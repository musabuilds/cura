"""How sure Cura is that an account is who it says, and the summaries apps draw.

A claim's `method` is how it was proven; its `level` is how strongly:

* `verified`: proven instantly by controlling something the registry lists
  (phone_match, registered_phone, work_email, website_token, facility_invite);
* `reviewed`: a moderator checked documents or evidence (evidence, documents, with_profile);
* `pending`: proof submitted, waiting for a moderator (or, for code methods, for the code);
* `unverified`: the account created the record and has proven nothing yet (created).

A record's verification is the strongest approved claim on it (reviewed > verified > unverified), or
`pending` when the only claims are in flight. It is computed on read, never stored on the record.
"""

from __future__ import annotations

import re
from functools import cache

from sqlalchemy import Text, cast, select
from sqlalchemy.orm import Session

from cura.api import guard, live, store
from cura.api.db import Claim, FacilityRow, PractitionerRow, RoleRow, User
from cura.reference import read_csv

INSTANT = {"phone_match", "registered_phone", "work_email", "website_token", "facility_invite"}
REVIEWED = {"evidence", "documents", "with_profile"}
RANK = {"unverified": 1, "verified": 2, "reviewed": 3}


def claim_level(c: Claim) -> str:
    if c.status == "pending":
        return "pending"
    if c.level:
        return c.level
    if c.method in INSTANT:
        return "verified"
    if c.method == "created":
        return "unverified"
    return "reviewed"


def aware(dt):
    return live.availability.aware(dt) if dt is not None else None


def claim_out(c: Claim) -> dict:
    return {
        "id": c.id,
        "subject_type": c.subject_type,
        "subject_id": c.subject_id,
        "status": "approved"
        if c.status == "approved"
        else "rejected"
        if c.status in ("rejected", "superseded")
        else "pending",
        "method": c.method,
        "level": claim_level(c),
        "created_at": aware(c.created_at),
        "decided_at": aware(c.reviewed_at),
    }


def verification_of(claims: list[Claim]) -> dict | None:
    approved = [c for c in claims if c.status == "approved"]
    if approved:
        best = max(
            approved,
            key=lambda c: (RANK.get(claim_level(c), 0), aware(c.verified_at or c.reviewed_at) or aware(c.created_at)),
        )
        level = claim_level(best)
        return {
            "level": level,
            "method": best.method,
            "verified_at": aware(best.verified_at or best.reviewed_at) if level in ("verified", "reviewed") else None,
        }
    if any(
        c.status == "pending" and c.method not in ("registered_phone", "work_email", "website_token") for c in claims
    ):
        return {"level": "pending", "method": None, "verified_at": None}
    return None


def verifications(db: Session, kind: str, ids) -> dict[str, dict | None]:
    ids = set(ids)
    if not ids:
        return {}
    claims: dict[str, list[Claim]] = {}
    for c in db.scalars(
        select(Claim).where(
            Claim.subject_type == kind, Claim.subject_id.in_(ids), Claim.status.in_(["approved", "pending"])
        )
    ):
        claims.setdefault(c.subject_id, []).append(c)
    return {i: verification_of(claims.get(i, [])) for i in ids}


UNVERIFIED = {"level": "unverified", "method": None, "verified_at": None}


def owner_of(db: Session, kind: str, rid: str) -> str | None:
    return db.scalar(
        select(Claim.user_id).where(Claim.subject_type == kind, Claim.subject_id == rid, Claim.status == "approved")
    )


# --- summaries -----------------------------------------------------------------------------------


@cache
def specialty_labels() -> dict[str, dict]:
    return {r["code"]: {"en": r["name_en"], "ar": r["name_ar"]} for r in read_csv("specialties.csv")}


def _first_phone(doc: dict) -> str | None:
    phones = (doc.get("contact") or {}).get("phones") or []
    return phones[0] if phones else None


def facility_summaries(db: Session, docs: list[dict], *, with_live: bool = True) -> list[dict]:
    docs = [d for d in docs if d]
    ver = verifications(db, "facility", [d["id"] for d in docs])
    states = live.facility_live(db, docs, live.now()) if with_live and docs else {}
    out = []
    for d in docs:
        gov = (d.get("address") or {}).get("governorate")
        state = states.get(d["id"])
        phone = _first_phone(d)
        out.append(
            {
                "id": d["id"],
                "name": {"en": d["name"].get("en"), "ar": d["name"].get("ar")},
                "type": d["type"],
                "governorate": gov[3:] if gov else None,
                "address": d.get("address") or {},
                "location": d.get("location"),
                "phone_masked": guard.mask_phone(phone) if phone else None,
                "website": (d.get("contact") or {}).get("website"),
                "verification": ver.get(d["id"]),
                "open_now": None if not state or state["state"] == "unknown" else state["state"] in ("open", "limited"),
                "demo": bool(d.get("demo")),
            }
        )
    return out


def facility_summary(db: Session, doc: dict | None) -> dict | None:
    return facility_summaries(db, [doc])[0] if doc else None


def practitioner_summaries(db: Session, docs: list[dict]) -> list[dict]:
    docs = [d for d in docs if d]
    ids = [d["id"] for d in docs]
    ver = verifications(db, "practitioner", ids)
    counts: dict[str, int] = {}
    if ids:
        for pid, doc in db.execute(
            select(RoleRow.practitioner_id, RoleRow.doc).where(RoleRow.practitioner_id.in_(ids))
        ):
            if store.is_listed(doc):
                counts[pid] = counts.get(pid, 0) + 1
    labels = specialty_labels()
    return [
        {
            "id": d["id"],
            "name": {"en": d["name"].get("en"), "ar": d["name"].get("ar")},
            "title": d.get("title"),
            "specialty": d.get("specialty"),
            "specialty_label": labels.get(d.get("specialty") or "", None),
            "photo_url": d.get("photo_url"),
            "facilities_count": counts.get(d["id"], 0),
            "verification": ver.get(d["id"]) or UNVERIFIED,
            "demo": bool(d.get("demo")),
        }
        for d in docs
    ]


def practitioner_summary(db: Session, doc: dict | None) -> dict | None:
    return practitioner_summaries(db, [doc])[0] if doc else None


# --- phone evidence ------------------------------------------------------------------------------


def _digits(e164: str) -> str:
    return re.sub(r"\D", "", e164)[-9:]  # national significant number: matches "+9627…" and "07…" spellings


def _phones_in(values) -> set[str]:
    return {e for e in (guard.normalise_phone(v) for v in values or [] if isinstance(v, str)) if e}


def facilities_with_phone(db: Session, e164: str) -> list[str]:
    """Facilities whose registered phones include this number (exact, after normalisation)."""
    rows = db.scalars(select(FacilityRow).where(cast(FacilityRow.doc, Text).like(f"%{_digits(e164)}%"))).all()
    return [
        r.id
        for r in rows
        if r.doc.get("status") != "merged" and e164 in _phones_in((r.doc.get("contact") or {}).get("phones"))
    ]


def practitioners_with_phone(db: Session, e164: str) -> list[str]:
    """Practitioners whose record or roles list this number. Role phones are often a clinic's reception
    line, shared by every doctor there, so callers treat a number on more than one doctor as no proof."""
    found: set[str] = set()
    for r in db.scalars(select(RoleRow).where(cast(RoleRow.doc, Text).like(f"%{_digits(e164)}%"))):
        if e164 in _phones_in(r.doc.get("phones")):
            found.add(r.practitioner_id)
    for r in db.scalars(select(PractitionerRow).where(cast(PractitionerRow.doc, Text).like(f"%{_digits(e164)}%"))):
        phones = [p.get("number") for p in ((r.doc.get("contact") or {}).get("phones") or [])]
        if e164 in _phones_in(phones) and r.doc.get("status") != "merged":
            found.add(r.id)
    return sorted(found)


def unique_phone_match(db: Session, kind: str, rid: str, user: User | None) -> bool:
    """The caller's verified phone is on this record and on no other record of its kind."""
    if user is None or not user.phone:
        return False
    hits = facilities_with_phone(db, user.phone) if kind == "facility" else practitioners_with_phone(db, user.phone)
    return hits == [rid]
