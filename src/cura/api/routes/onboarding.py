"""Onboarding from a phone: find yourself, claim it or create it, and prove you run a facility.

    GET  /v1/onboarding/practitioner/candidates   "which of these is me?" — phone match first, then names
    POST /v1/claims                               claim a record: instant on a phone match, else a moderator
    POST /v1/practitioners · POST /v1/facilities  "none of these": created at once, owned, `unverified`
    GET  /v1/onboarding/facility/candidates       facilities to claim, each with the proofs it allows
    POST /v1/facilities/{id}/claims               start a proof (code, email, token, documents)
    POST /v1/facility-claims/{id}/verify          finish it
    GET  /v1/merge-cases · POST /v1/merge-cases/{id}/dismiss    moderators: possible duplicates

**Never blocked, never a silent duplicate.** A doctor or facility the matcher thinks already exists is
shown the candidates first (409). If they insist (`confirm_new`), the record is created, owned by them
at level `unverified`, and a merge case with the candidates goes to a moderator. This applies to
accounts with a verified phone (which is the accountability anchor); email/password accounts keep the
older moderated path (ADR 0004 → ADR 0005).

**Proving a facility.** Each method relies on something only the real facility controls and that the
registry recorded before this account existed: its listed phone (code by SMS or voice call), a mailbox
on its own website's domain, a file or DNS record on that website, or MoH licence + commercial
registration checked by a moderator. The target always comes from the record, never from the caller.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from rapidfuzz import fuzz
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cura import normalize as nz
from cura.api import contact, guard, matching, messaging, netguard, store, verification, writes
from cura.api.db import Claim, MergeCase, OtpChallenge, get_session, utcnow
from cura.api.deps import Principal, owned_subjects, require_moderator, require_user
from cura.api.errors import Problem
from cura.api.routes import contrib
from cura.api.routes.account import check_code

router = APIRouter(prefix="/v1")
FACILITY_CODE_TTL = timedelta(minutes=10)
WEBSITE_TOKEN_TTL = timedelta(days=7)
WELL_KNOWN = "/.well-known/cura-verification.txt"

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "yandex.com", "yandex.ru",
    "mail.com", "gmx.com", "gmx.net", "zoho.com", "mail.ru", "inbox.com", "hushmail.com", "tutanota.com",
    "fastmail.com", "maktoob.com", "yahoo.co.uk", "hotmail.co.uk", "outlook.sa", "windowslive.com",
}  # fmt: skip
# Hosts anyone can publish on: a page there proves nothing about who runs the facility.
SHARED_HOSTS = (
    "facebook.com", "fb.com", "instagram.com", "linktr.ee", "google.com", "goo.gl", "business.site",
    "wixsite.com", "blogspot.com", "wordpress.com", "sites.google.com", "linkedin.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "wa.me", "whatsapp.com", "t.me", "github.io", "netlify.app", "vercel.app",
    "weebly.com", "squarespace.com", "webflow.io", "carrd.co",
)  # fmt: skip


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceIn(Body):
    license_number: Annotated[str | None, Field(max_length=80)] = None
    jmc_number: Annotated[str | None, Field(max_length=80)] = None
    note: Annotated[str | None, Field(max_length=2000)] = None
    work_email: EmailStr | None = None
    document_url: Annotated[str | None, Field(max_length=2000, pattern=r"^https://")] = None


class ClaimIn(Body):
    subject_type: Literal["practitioner", "facility"]
    subject_id: Annotated[str, Field(max_length=20)]
    evidence: EvidenceIn = Field(default_factory=EvidenceIn)
    dispute: bool = Field(False, description="The record is owned by someone else and it is yours: a moderator decides")


class DocumentsIn(Body):
    moh_license: Annotated[str, Field(min_length=3, max_length=80)]
    commercial_registration: Annotated[str, Field(min_length=3, max_length=80)]
    photo_url: Annotated[str | None, Field(max_length=2000, pattern=r"^https://")] = None


Method = Literal["registered_phone", "phone_match", "work_email", "website_token", "documents"]


class FacilityClaimIn(Body):
    method: Method
    email: EmailStr | None = None
    documents: DocumentsIn | None = None
    dispute: bool = False


class VerifyIn(Body):
    code: Annotated[str | None, Field(pattern=r"^\d{6}$")] = None


# --- shared --------------------------------------------------------------------------------------


def _record(db: Session, kind: str, rid: str):
    row = store.get(db, kind, rid)
    if row is None:
        raise Problem(404, f"{kind.capitalize()} not found", rid)
    if row.doc.get("status") == "merged":
        raise Problem(
            409,
            f"This {kind} was merged",
            f"Use {row.doc.get('replaced_by')} instead.",
            replaced_by=row.doc.get("replaced_by"),
        )
    return row


def claim_state(db: Session, kind: str, rid: str, user_id: str) -> str:
    owner = verification.owner_of(db, kind, rid)
    return "unclaimed" if owner is None else "claimed_by_you" if owner == user_id else "claimed"


def _owned_by_other(db: Session, kind: str, rid: str, user_id: str) -> bool:
    return (
        db.scalar(
            select(Claim.id).where(
                Claim.subject_type == kind,
                Claim.subject_id == rid,
                Claim.status == "approved",
                Claim.user_id != user_id,
            )
        )
        is not None
    )


def _approve(claim: Claim, level: str) -> None:
    now = utcnow()
    claim.status, claim.level, claim.reviewed_at = "approved", level, now
    if level in ("verified", "reviewed"):
        claim.verified_at = now


def _supersede_created(db: Session, user_id: str, kind: str, rid: str, keep: str) -> None:
    """Proving a record you created replaces the `created` claim (one approved claim per owner)."""
    db.execute(
        update(Claim)
        .where(
            Claim.user_id == user_id,
            Claim.subject_type == kind,
            Claim.subject_id == rid,
            Claim.status == "approved",
            Claim.id != keep,
        )
        .values(status="superseded", note="replaced by a verified claim")
    )


def _require_phone_user(p: Principal) -> None:
    if not p.user.phone:
        raise Problem(403, "Verified phone required", "Sign in with your phone number (POST /v1/auth/phone/start).")


# --- doctors -------------------------------------------------------------------------------------


@router.get("/onboarding/practitioner/candidates", tags=["claims"])
def practitioner_candidates(
    name: Annotated[str | None, Query(max_length=200)] = None,
    specialty: Annotated[str | None, Query(max_length=64)] = None,
    p: Principal = Depends(require_user("profile")),
    db: Session = Depends(get_session),
) -> dict:
    """Records that may be the caller: exact matches on their verified phone first (claiming one of those
    completes at once), then fuzzy name matches with Arabic folding. Only public summaries."""
    guard.throttle(f"onb:cand:{p.user.id}", 60, 60, "searches")
    found: dict[str, dict] = {}
    if p.user.phone:
        hits = verification.practitioners_with_phone(db, p.user.phone)
        for pid in hits:
            found[pid] = {"score": 0.99, "reasons": ["phone_match"], "instant": len(hits) == 1}
    names = [name or p.user.display_name]
    ambiguous = False
    if any(names):
        workplace = sorted(owned_subjects(db, p.user)["facility"])
        for m in matching.practitioner_matches(
            db, names=names, specialty=specialty, facility_id=workplace[0] if workplace else None, limit=15
        ):
            ambiguous = ambiguous or m["ambiguous"]
            entry = found.setdefault(m["id"], {"score": 0.0, "reasons": [], "instant": False})
            reasons = ["name_match"]
            if "same specialty" in m["reasons"]:
                reasons.append("specialty_match")
            if "works at the same facility" in m["reasons"]:
                reasons.append("shared_workplace")
            entry["reasons"] += [r for r in reasons if r not in entry["reasons"]]
            entry["score"] = max(entry["score"], m["score"])
    docs = {d["id"]: d for d in store.docs(db, "practitioner", found)}
    summaries = {s["id"]: s for s in verification.practitioner_summaries(db, list(docs.values()))}
    data = []
    for pid, entry in found.items():
        if pid not in summaries or docs[pid].get("status") == "merged":
            continue
        state = claim_state(db, "practitioner", pid, p.user.id)
        data.append(
            {
                "practitioner": summaries[pid],
                "score": round(entry["score"], 2),
                "reasons": entry["reasons"],
                "claim_state": state,
                "instant": entry["instant"] and state == "unclaimed",
            }
        )
    data.sort(key=lambda c: ("phone_match" not in c["reasons"], -c["score"]))
    return {"data": data[:20], "ambiguous": ambiguous}


@router.post("/claims", status_code=201, tags=["claims"])
def create_claim(
    body: ClaimIn,
    request: Request,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Claim a doctor profile (yours) or a facility. Approved at once when your verified phone is on the
    record and on no other (`phone_match`); otherwise pending with your evidence, for a moderator.
    409 when someone else owns it: resend with `dispute: true` to have a moderator decide."""
    guard.throttle(f"claims:{p.user.id}", 10, 600, "claims")
    _record(db, body.subject_type, body.subject_id)
    evidence = body.evidence.model_dump(exclude_none=True, mode="json")
    mine = db.scalar(
        select(Claim).where(
            Claim.user_id == p.user.id,
            Claim.subject_type == body.subject_type,
            Claim.subject_id == body.subject_id,
            Claim.status.in_(["pending", "approved"]),
        )
    )
    if mine:
        raise Problem(
            409, "Claim already exists", f"Claim {mine.id} is {mine.status}.", claim=verification.claim_out(mine)
        )
    taken = _owned_by_other(db, body.subject_type, body.subject_id, p.user.id)
    if taken and not body.dispute:
        raise Problem(
            409,
            "Already claimed",
            "Someone else manages this record. If it is yours, open a dispute "
            "(resend with dispute: true and your evidence); a moderator will decide.",
            can_dispute=True,
        )
    if body.subject_type == "practitioner" and not body.dispute and owned_subjects(db, p.user)["practitioner"]:
        raise Problem(409, "You already own a profile", "One account manages one doctor profile.")
    claim = Claim(user_id=p.user.id, subject_type=body.subject_type, subject_id=body.subject_id, evidence=evidence)
    if body.dispute:
        if not evidence:
            raise Problem(422, "Evidence required", "A dispute needs evidence: license_number, jmc_number or a note.")
        claim.method, claim.level = "dispute", "pending"
    elif verification.unique_phone_match(db, body.subject_type, body.subject_id, p.user):
        claim.method = "phone_match"
        _approve(claim, "verified")
    else:
        if not evidence:
            raise Problem(
                422,
                "Evidence required",
                "Your phone is not on this record: add license_number, jmc_number (or work_email/document_url) for a "
                "moderator to check.",
                errors=[{"field": "evidence", "message": "required"}],
            )
        claim.method, claim.level = "evidence", "pending"
    db.add(claim)
    db.flush()
    guard.audit(
        db,
        "claim." + ("approved" if claim.status == "approved" else "requested"),
        actor_id=p.user.id,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        request=request,
        method=claim.method,
    )
    db.commit()
    return verification.claim_out(claim) | {"can_dispute": False}


def _contact_split(record: dict) -> tuple[dict, dict | None]:
    """Takes `contact` out of a practitioner record: the private part is stored separately."""
    if "contact" not in record:
        return record, None
    rest = {k: v for k, v in record.items() if k != "contact"}
    return rest, contact.prepare(None, record["contact"])


PRACTITIONER_CREATE = {
    "name",
    "specialty",
    "title",
    "gender",
    "grade",
    "languages",
    "subspecialties",
    "qualifications",
    "bio",
    "photo_url",
    "contact",
}


def _practitioner_candidates(db: Session, user_id: str, matches: list[dict]) -> list[dict]:
    docs = store.docs(db, "practitioner", [m["id"] for m in matches])
    summaries = {s["id"]: s for s in verification.practitioner_summaries(db, docs)}
    return [
        {
            "practitioner": summaries[m["id"]],
            "score": m["score"],
            "reasons": m["reasons"],
            "claim_state": claim_state(db, "practitioner", m["id"], user_id),
        }
        for m in matches
        if m["id"] in summaries
    ]


@router.post("/practitioners", status_code=201, tags=["claims"])
def create_practitioner(
    body: contrib.CreateIn,
    request: Request,
    response: Response,
    p: Principal = Depends(require_user("proposals:write")),
    db: Session = Depends(get_session),
) -> dict:
    """ "None of these is me." With a verified phone: 409 with candidates when similar records exist; with
    `confirm_new: true` the profile is created at once, owned by you, `unverified`, and the candidates go
    to a moderator as a merge case. Without a phone account: the moderated path (see POST /v1/{collection})."""
    if not p.user.phone:
        response.status_code = 202
        return contrib.create("practitioners", body, response, p, db)
    guard.throttle(f"create:{p.user.id}", 5, 3600, "new records")
    record = dict(body.record)
    unknown = sorted(set(record) - PRACTITIONER_CREATE)
    if unknown:
        raise Problem(422, "Unknown fields", f"Not accepted when creating a doctor: {unknown}")
    if owned_subjects(db, p.user)["practitioner"]:
        raise Problem(409, "You already own a profile", "One account manages one doctor profile.")
    record, private = _contact_split(record)
    if private is not None:
        record["contact"] = contact.project(private)
    writes.check_create("practitioner", record)
    matches = matching.practitioner_matches(
        db, names=[(record.get("name") or {}).get(k) for k in ("en", "ar")], specialty=record.get("specialty")
    )
    if matches and not body.confirm_new:
        raise Problem(
            409,
            "Possible duplicate",
            "These records look like you. Claim one, or resend with confirm_new: true if none is you.",
            candidates=_practitioner_candidates(db, p.user.id, matches),
        )
    change = writes.record_change(
        db,
        p,
        "practitioner",
        None,
        "create",
        record | ({"_candidates": [m["id"] for m in matches]} if matches else {}),
        reason=body.reason,
    )
    doc = writes.apply_create(db, "practitioner", record, change.id)
    change.subject_id = doc["id"]
    writes.mark_applied(change)
    db.flush()
    if private is not None:
        contact.save(db, doc["id"], private, p.user.id)
    claim = Claim(
        user_id=p.user.id,
        subject_type="practitioner",
        subject_id=doc["id"],
        method="created",
        evidence={},
        note="created by its owner",
    )
    _approve(claim, "unverified")
    db.add(claim)
    if matches:
        db.add(
            MergeCase(
                kind="practitioner", record_id=doc["id"], candidates=[m["id"] for m in matches], created_by=p.user.id
            )
        )
    db.flush()
    guard.audit(
        db,
        "record.created",
        actor_id=p.user.id,
        subject_type="practitioner",
        subject_id=doc["id"],
        request=request,
        duplicates=len(matches) or None,
    )
    db.commit()
    from cura.api.routes.registry import practitioner_view

    return {
        "practitioner": practitioner_view(db, store.get(db, "practitioner", doc["id"]), p),
        "claim": verification.claim_out(claim),
        "possible_duplicates": [c["practitioner"] for c in _practitioner_candidates(db, p.user.id, matches)],
    }


# --- facilities ----------------------------------------------------------------------------------


def website_host(doc: dict) -> str | None:
    site = (doc.get("contact") or {}).get("website")
    if not site:
        return None
    if "://" not in site:
        site = "https://" + site
    host = netguard.normalise_host(urlsplit(site).hostname)
    if not host or "." not in host:
        return None
    return host


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def shared_host(host: str) -> bool:
    bare = _bare(host)
    return any(bare == h or bare.endswith("." + h) for h in SHARED_HOSTS) or bare in FREE_MAIL


def registered_phone(doc: dict) -> str | None:
    """The number the code goes to: the first listed mobile (SMS), else the first listed number (voice)."""
    phones = [e for e in (guard.normalise_phone(x) for x in (doc.get("contact") or {}).get("phones") or []) if e]
    mobiles = [x for x in phones if guard.is_mobile(x)]
    return (mobiles or phones or [None])[0]


def offers(db: Session, doc: dict, p: Principal) -> list[dict]:
    phone = registered_phone(doc)
    host = website_host(doc)
    usable_host = host if host and not shared_host(host) else None
    match = verification.unique_phone_match(db, "facility", doc["id"], p.user)
    return [
        {
            "method": "registered_phone",
            "available": phone is not None,
            "target_masked": guard.mask_phone(phone) if phone else None,
            "channel": ("sms" if guard.is_mobile(phone) else "voice") if phone else None,
            **({} if phone else {"unavailable_reason": "no phone on record"}),
        },
        {
            "method": "phone_match",
            "available": match,
            "target_masked": guard.mask_phone(p.user.phone) if p.user.phone else None,
            "channel": None,
            **({} if match else {"unavailable_reason": "your verified phone is not this facility's registered phone"}),
        },
        {
            "method": "work_email",
            "available": usable_host is not None,
            "target_masked": f"•••@{_bare(usable_host)}" if usable_host else None,
            "channel": "email",
            **({} if usable_host else {"unavailable_reason": "no own website domain on record"}),
        },
        {
            "method": "website_token",
            "available": usable_host is not None,
            "target_masked": usable_host,
            "channel": None,
            **({} if usable_host else {"unavailable_reason": "no own website on record"}),
        },
        {"method": "documents", "available": True, "target_masked": None, "channel": None},
    ]


@router.get("/onboarding/facility/candidates", tags=["claims"])
def facility_candidates(
    q: Annotated[str | None, Query(max_length=200)] = None,
    near: Annotated[str | None, Query(description="lat,lon")] = None,
    p: Principal = Depends(require_user("profile")),
    db: Session = Depends(get_session),
) -> dict:
    """Facilities the caller may run: those whose registered phone is the caller's verified phone first,
    then name / nearby matches. Each lists the proofs this record allows (`verification_methods`)."""
    guard.throttle(f"onb:fcand:{p.user.id}", 60, 60, "searches")
    point = store.parse_near(near)
    if near and point is None:
        raise Problem(422, "Invalid near", "Use near=<lat>,<lon>")
    found: dict[str, dict] = {}
    if p.user.phone:
        for fid in verification.facilities_with_phone(db, p.user.phone):
            found[fid] = {"score": 0.99, "reasons": ["phone_match"]}
    if q or point:
        docs, _, dist = store.search_facilities(db, q=q, near=point, radius_km=10.0, limit=20)
        key = nz.match_key(q or "", drop=set())
        for d in docs:
            entry = found.setdefault(d["id"], {"score": 0.0, "reasons": []})
            if q:
                names = [nz.match_key(n, drop=set()) for n in (d["name"].get("en"), d["name"].get("ar")) if n]
                sim = max((fuzz.token_set_ratio(key, n) for n in names), default=0) / 100
                entry["score"] = max(entry["score"], round(sim, 2))
                if "name_match" not in entry["reasons"]:
                    entry["reasons"].append("name_match")
            if d["id"] in dist:
                entry["reasons"].append(f"nearby ({dist[d['id']]} m)")
    docs = {d["id"]: d for d in store.docs(db, "facility", found)}
    summaries = {s["id"]: s for s in verification.facility_summaries(db, list(docs.values()))}
    data = [
        {
            "facility": summaries[fid],
            "score": entry["score"],
            "reasons": entry["reasons"],
            "claim_state": claim_state(db, "facility", fid, p.user.id),
            "verification_methods": offers(db, docs[fid], p),
        }
        for fid, entry in found.items()
        if fid in summaries and docs[fid].get("status") != "merged"
    ]
    data.sort(key=lambda c: ("phone_match" not in c["reasons"], -c["score"]))
    return {"data": data[:20]}


@router.post("/facilities/{facility_id}/claims", status_code=201, tags=["claims"])
def start_facility_claim(
    facility_id: str,
    body: FacilityClaimIn,
    request: Request,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Start proving you run this facility. Codes go to targets taken from the registry record, never
    from the request (except `work_email`, which must sit on the record's own website domain)."""
    guard.throttle(f"fclaim:user:{p.user.id}", 6, 600, "verification attempts")
    guard.throttle(f"fclaim:facility:{facility_id}", 12, 3600, "verification attempts for this facility")
    row = _record(db, "facility", facility_id)
    doc = row.doc
    mine = db.scalars(
        select(Claim).where(
            Claim.user_id == p.user.id,
            Claim.subject_type == "facility",
            Claim.subject_id == facility_id,
            Claim.status.in_(["approved", "pending"]),
        )
    ).all()
    if any(c.status == "approved" and verification.claim_level(c) in ("verified", "reviewed") for c in mine):
        raise Problem(409, "Already verified", "You already manage this facility.")
    if _owned_by_other(db, "facility", facility_id, p.user.id) and not body.dispute:
        raise Problem(
            409,
            "Already claimed",
            "Someone else manages this facility. If you run it, resend with "
            "method: documents and dispute: true; a moderator will decide.",
            can_dispute=True,
        )
    # A new attempt replaces any earlier unfinished one by the same account.
    for c in mine:
        if c.status == "pending":
            c.status, c.note = "rejected", "replaced by a newer attempt"
    if body.dispute and body.method != "documents":
        raise Problem(422, "Disputes need documents", "Use method: documents with dispute: true.")
    claim = Claim(
        user_id=p.user.id,
        subject_type="facility",
        subject_id=facility_id,
        method=body.method,
        level="pending",
        evidence={},
    )
    out: dict[str, Any] = {}
    now = utcnow()
    if body.method == "phone_match":
        if not verification.unique_phone_match(db, "facility", facility_id, p.user):
            raise Problem(422, "Phone does not match", "Your verified phone is not this facility's registered phone.")
        _approve(claim, "verified")
    elif body.method == "registered_phone":
        phone = registered_phone(doc)
        if phone is None:
            raise Problem(422, "No phone on record", "Choose another method.")
        channel = "sms" if guard.is_mobile(phone) else "voice"
        out["challenge"] = _challenge(db, claim, phone, channel, FACILITY_CODE_TTL, request)
    elif body.method == "work_email":
        if body.email is None:
            raise Problem(422, "email required", errors=[{"field": "email", "message": "required"}])
        email = str(body.email).lower()
        domain = email.rsplit("@", 1)[1]
        host = website_host(doc)
        if domain in FREE_MAIL:
            raise Problem(422, "Use a work email", "Free email providers cannot prove who runs a facility.")
        if host is None or shared_host(host) or domain != _bare(host):
            raise Problem(
                422,
                "Email domain does not match",
                f"Use an address on the facility's own website domain ({_bare(host) if host else 'none on record'}).",
            )
        claim.evidence = {"work_email": email}
        out["challenge"] = _challenge(db, claim, email, "email", FACILITY_CODE_TTL, request)
    elif body.method == "website_token":
        host = website_host(doc)
        if host is None or shared_host(host):
            raise Problem(422, "No own website on record", "Choose another method.")
        token = "cura-verify=" + secrets.token_urlsafe(24)
        claim.evidence = {"host": host}
        db.add(claim)
        db.flush()
        challenge_id = "fc_" + secrets.token_urlsafe(18)
        db.add(
            OtpChallenge(
                id=challenge_id,
                purpose="facility_claim",
                target=host,
                channel="web",
                code_hash=guard.code_hash(challenge_id, token),
                claim_id=claim.id,
                max_attempts=50,
                expires_at=now + WEBSITE_TOKEN_TTL,
                ip=guard.client_ip(request),
            )
        )
        out["website_token"] = token
        out["instructions"] = {
            "file": f"https://{host}{WELL_KNOWN}",
            "dns_txt": f"_cura-verification.{_bare(host)}",
            "value": token,
            "expires_in": int(WEBSITE_TOKEN_TTL.total_seconds()),
        }
    else:  # documents
        if body.documents is None:
            raise Problem(422, "documents required", errors=[{"field": "documents", "message": "required"}])
        claim.evidence = body.documents.model_dump(exclude_none=True) | ({"dispute": True} if body.dispute else {})
        claim.method = "dispute" if body.dispute else "documents"
    if claim not in db:
        db.add(claim)
    db.flush()
    if claim.status == "approved":
        _supersede_created(db, p.user.id, "facility", facility_id, claim.id)
    guard.audit(
        db,
        "facility_claim.started" if claim.status != "approved" else "claim.approved",
        actor_id=p.user.id,
        subject_type="facility",
        subject_id=facility_id,
        request=request,
        method=body.method,
    )
    db.commit()
    return {"claim": verification.claim_out(claim), **out}


def _challenge(db: Session, claim: Claim, target: str, channel: str, ttl: timedelta, request: Request) -> dict:
    db.add(claim)
    db.flush()
    challenge_id = "fc_" + secrets.token_urlsafe(18)
    code = guard.new_code()
    db.add(
        OtpChallenge(
            id=challenge_id,
            purpose="facility_claim",
            target=target,
            channel=channel,
            code_hash=guard.code_hash(challenge_id, code),
            claim_id=claim.id,
            max_attempts=5,
            expires_at=utcnow() + ttl,
            ip=guard.client_ip(request),
        )
    )
    db.flush()
    if channel == "email":
        messaging.email().send_email(
            target,
            "Your Cura verification code",
            f"Your code to verify this facility on Cura is {code}. It expires in 10 minutes.",
            code=code,
        )
        masked = guard.mask_email(target)
    elif channel == "voice":
        messaging.sms().call_with_code(target, code)
        masked = guard.mask_phone(target)
    else:
        messaging.sms().send_sms(
            target,
            f"Cura: someone is verifying this facility. Code: {code}. Ignore this message if it was not your staff.",
            code=code,
        )
        masked = guard.mask_phone(target)
    return {
        "challenge_id": challenge_id,
        "expires_in": int(ttl.total_seconds()),
        "target_masked": masked,
        "channel": channel,
    }


def _token_published(host: str, challenge: OtpChallenge) -> bool:
    candidates: list[str] = []
    try:
        fetched = netguard.fetch(f"https://{host}{WELL_KNOWN}", allowed_host=host, max_bytes=4096)
        if fetched.status == 200:
            candidates += fetched.body.decode("utf-8", "replace").split()
    except netguard.Blocked:
        pass
    candidates += dns_txt(f"_cura-verification.{_bare(host)}") + dns_txt(_bare(host))
    return any(
        c.startswith("cura-verify=") and guard.code_matches(challenge.id, c.strip().strip('"'), challenge.code_hash)
        for c in candidates
    )


def dns_txt(name: str) -> list[str]:  # replaced in tests
    try:
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.lifetime = 5.0
        answer = resolver.resolve(name, "TXT")
        return [b"".join(r.strings).decode("utf-8", "replace") for r in answer][:20]
    except Exception:
        return []


@router.post("/facility-claims/{claim_id}/verify", tags=["claims"])
def verify_facility_claim(
    claim_id: str,
    request: Request,
    body: VerifyIn | None = None,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Finish a proof: the code that was sent, or (website_token) nothing — Cura fetches the token."""
    guard.throttle(f"fverify:user:{p.user.id}", 20, 600, "verification attempts")
    guard.throttle(f"fverify:ip:{guard.client_ip(request)}", 40, 600, "verification attempts")
    claim = db.get(Claim, claim_id)
    if claim is None or claim.user_id != p.user.id or claim.subject_type != "facility":
        raise Problem(404, "Claim not found")
    if claim.status == "approved":
        return verification.claim_out(claim)
    if claim.status != "pending" or claim.method not in ("registered_phone", "work_email", "website_token"):
        raise Problem(409, "Nothing to verify", "This claim is waiting for a moderator or was closed.")
    challenge = db.scalar(
        select(OtpChallenge).where(OtpChallenge.claim_id == claim.id).order_by(OtpChallenge.created_at.desc())
    )
    if challenge is None:
        raise Problem(410, "Challenge expired", "Start again.")
    if claim.method == "website_token":
        if challenge.consumed_at is not None or verification.aware(challenge.expires_at) <= utcnow():
            raise Problem(410, "Token expired", "Start again to get a new token.")
        guard.throttle(f"fverify:web:{claim.id}", 10, 600, "checks for this token")
        if not _token_published(challenge.target, challenge):
            raise Problem(
                422,
                "Token not found yet",
                f"Cura could not find the token at https://{challenge.target}"
                f"{WELL_KNOWN} or in DNS. Publish it and try again.",
            )
        challenge.consumed_at = utcnow()
    else:
        if body is None or body.code is None:
            raise Problem(422, "code required", errors=[{"field": "code", "message": "required"}])
        check_code(db, challenge, body.code, request, "facility_claim")
    if _owned_by_other(db, "facility", claim.subject_id, p.user.id):
        claim.status, claim.note = "rejected", "someone else verified first"
        db.commit()
        raise Problem(
            409, "Already claimed", "Someone else verified this facility first. Open a dispute.", can_dispute=True
        )
    _approve(claim, "verified")
    _supersede_created(db, p.user.id, "facility", claim.subject_id, claim.id)
    guard.audit(
        db,
        "claim.approved",
        actor_id=p.user.id,
        subject_type="facility",
        subject_id=claim.subject_id,
        request=request,
        method=claim.method,
    )
    db.commit()
    return verification.claim_out(claim)


FACILITY_CREATE_FORBIDDEN = {"rating", "accreditations", "status", "replaced_by", "demo"}


@router.post("/facilities", status_code=201, tags=["claims"])
def create_facility(
    body: contrib.CreateIn,
    request: Request,
    response: Response,
    p: Principal = Depends(require_user("proposals:write")),
    db: Session = Depends(get_session),
) -> dict:
    """A facility that is not in the registry. With a verified phone: 409 with candidates unless
    `confirm_new`; created at once, owned by you, `unverified` until proven by a method above."""
    if not p.user.phone:
        response.status_code = 202
        return contrib.create("facilities", body, response, p, db)
    guard.throttle(f"create:{p.user.id}", 5, 3600, "new records")
    record = dict(body.record)
    bad = sorted(set(record) & FACILITY_CREATE_FORBIDDEN)
    if bad:
        raise Problem(422, "Read-only fields", f"Not accepted when creating a facility: {bad}")
    writes.check_create("facility", record)
    matches = matching.facility_matches(db, record)
    if matches and not body.confirm_new:
        raise Problem(
            409,
            "Possible duplicate",
            "This facility may already be in Cura. Claim it, or resend with "
            "confirm_new: true if none is the same place.",
            candidates=matches,
        )
    change = writes.record_change(
        db,
        p,
        "facility",
        None,
        "create",
        record | ({"_candidates": [m["id"] for m in matches]} if matches else {}),
        reason=body.reason,
    )
    doc = writes.apply_create(db, "facility", record, change.id)
    change.subject_id = doc["id"]
    writes.mark_applied(change)
    claim = Claim(
        user_id=p.user.id,
        subject_type="facility",
        subject_id=doc["id"],
        method="created",
        evidence={},
        note="created by its owner",
    )
    _approve(claim, "unverified")
    db.add(claim)
    if matches:
        db.add(
            MergeCase(kind="facility", record_id=doc["id"], candidates=[m["id"] for m in matches], created_by=p.user.id)
        )
    db.flush()
    guard.audit(
        db,
        "record.created",
        actor_id=p.user.id,
        subject_type="facility",
        subject_id=doc["id"],
        request=request,
        duplicates=len(matches) or None,
    )
    db.commit()
    summaries = verification.facility_summaries(db, store.docs(db, "facility", [m["id"] for m in matches]))
    return {
        "facility": doc | {"verification": verification.verifications(db, "facility", [doc["id"]])[doc["id"]]},
        "claim": verification.claim_out(claim),
        "possible_duplicates": summaries,
        "verification_methods": offers(db, doc, p),
    }


# --- moderators: merge cases ---------------------------------------------------------------------


@router.get("/merge-cases", tags=["moderation"])
def list_merge_cases(
    status: Literal["open", "merged", "dismissed"] = "open",
    _: Principal = Depends(require_moderator),
    db: Session = Depends(get_session),
) -> dict:
    rows = db.scalars(select(MergeCase).where(MergeCase.status == status).order_by(MergeCase.created_at)).all()
    return {
        "data": [
            {
                "id": m.id,
                "kind": m.kind,
                "record_id": m.record_id,
                "candidates": m.candidates,
                "status": m.status,
                "created_at": verification.aware(m.created_at),
            }
            for m in rows
        ],
        "count": len(rows),
    }


@router.post("/merge-cases/{case_id}/dismiss", tags=["moderation"])
def dismiss_merge_case(
    case_id: str, p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    case = db.get(MergeCase, case_id)
    if case is None or case.status != "open":
        raise Problem(404, "No open merge case with that id")
    case.status, case.resolved_by, case.resolved_at = "dismissed", p.user.id, utcnow()
    db.commit()
    return {"id": case.id, "status": case.status}
