"""Write API: edits, proposals, ownership claims, reviews and moderation.

Every write is recorded as a `Change`. Whether it applies immediately is decided by `policy`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import policy, store
from cura.api.db import Change, Claim, Review, get_session, utcnow
from cura.api.deps import Principal, owned_subjects, require_moderator, require_user
from cura.api.errors import Problem
from cura.reference import read_csv

router = APIRouter(prefix="/v1")
# Routes with a `{collection}` placeholder are mounted last so they never shadow /v1/claims etc.
collection_router = APIRouter(prefix="/v1")
COLLECTION = {"facilities": "facility", "practitioners": "practitioner", "roles": "role"}
DIMENSIONS = {r["code"] for r in read_csv("review_dimensions.csv")}


class EditIn(BaseModel):
    patch: dict[str, Any] = Field(description="RFC 7396 JSON Merge Patch against the record")
    reason: Annotated[str | None, Field(max_length=2000)] = None
    evidence_url: Annotated[str | None, Field(max_length=2000)] = None


class CreateIn(BaseModel):
    record: dict[str, Any] = Field(description="The new record, without id/sources/quality/timestamps")
    reason: Annotated[str | None, Field(max_length=2000)] = None
    evidence_url: Annotated[str | None, Field(max_length=2000)] = None
    claim: dict[str, Any] | None = Field(None, description="Practitioners: evidence to own the new profile on approval")


class ClaimIn(BaseModel):
    subject_type: Literal["practitioner", "facility"]
    subject_id: str
    evidence: dict[str, Any] = Field(description="e.g. {license_number, work_email, document_url, note}")


class ReviewIn(BaseModel):
    ratings: dict[str, Annotated[int, Field(ge=1, le=5)]]
    text: Annotated[str | None, Field(max_length=4000)] = None
    visit_month: Annotated[str | None, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None


class DecisionIn(BaseModel):
    note: Annotated[str | None, Field(max_length=2000)] = None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kind(collection: str) -> str:
    if collection not in COLLECTION:
        raise Problem(404, "Unknown collection", f"One of: {', '.join(COLLECTION)}")
    return COLLECTION[collection]


def _owns(db: Session, p: Principal, kind: str, doc: dict) -> bool:
    if "practitioner:manage" not in p.scopes:
        return False
    owned = owned_subjects(db, p.user)
    if kind == "role":
        return doc["practitioner_id"] in owned["practitioner"] or doc["facility_id"] in owned["facility"]
    return doc["id"] in owned[kind]


def _change_out(c: Change) -> dict:
    return {
        "id": c.id,
        "subject_type": c.subject_type,
        "subject_id": c.subject_id,
        "op": c.op,
        "patch": c.patch,
        "status": c.status,
        "auto_applied": c.auto_applied,
        "reason": c.reason,
        "evidence_url": c.evidence_url,
        "created_at": c.created_at,
        "applied_at": c.applied_at,
        "review_note": c.review_note,
    }


def _apply_update(db: Session, kind: str, row, patch: dict) -> dict:
    new = policy.merge_patch(row.doc, patch)
    new["updated_at"] = _now_iso()
    try:
        row = store.put(db, kind, new)
    except ValidationError as exc:
        raise Problem(
            422,
            "Patch produces an invalid record",
            str(exc.errors()[0]["msg"]),
            errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
    return row.doc


def _blank(kind: str, record: dict, rid: str) -> dict:
    now = _now_iso()
    base: dict[str, Any] = {"id": rid, "updated_at": now}
    if kind in ("facility", "practitioner"):
        base |= {
            "created_at": now,
            "sources": [{"source": "cura_api", "ref": rid, "retrieved_at": now}],
            "quality": {"completeness": 0, "confidence": 0.45, "last_verified_at": now},
            "identifiers": {"cura_api": rid},
        }
    else:
        base |= {"sources": [{"source": "cura_api", "ref": rid}]}
    return {k: v for k, v in record.items() if k not in policy.SYSTEM - {"practitioner_id", "facility_id"}} | base


def _apply_create(db: Session, kind: str, record: dict) -> dict:
    if kind == "facility":
        gov = (record.get("address") or {}).get("governorate") or "JO-XX"
        from cura.pipeline.merge import TYPE_SEGMENT

        prefix = f"JO-{gov[3:]}-{TYPE_SEGMENT.get(record.get('type', 'other'), 'OTH')}"
    else:
        prefix = {"practitioner": "JO-PRC", "role": "JO-ROL"}[kind]
    rid = store.next_reserved_id(db, kind, prefix)
    try:
        return store.put(db, kind, _blank(kind, record, rid)).doc
    except ValidationError as exc:
        raise Problem(
            422,
            "Invalid record",
            str(exc.errors()[0]["msg"]),
            errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc


# --- edits ---------------------------------------------------------------------------------------


@collection_router.patch("/{collection}/{rid}", tags=["contribute"])
def edit(
    collection: str,
    rid: str,
    body: EditIn,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    p: Principal = Depends(require_user()),
    db: Session = Depends(get_session),
) -> dict:
    """Change a record. Applies now if you are a verified owner editing owner fields (or a moderator);
    otherwise it is queued as a proposal (202). Send `If-Match: "<version>"` to avoid lost updates."""
    kind = _kind(collection)
    row = store.get(db, kind, rid)
    if row is None:
        raise Problem(404, "Not found", rid)
    if if_match and if_match.strip('"') != str(row.version):
        raise Problem(412, "Version mismatch", f"Current version is {row.version}; re-read and retry.")
    owns = _owns(db, p, kind, row.doc)
    decision = policy.decide(kind, body.patch, is_moderator=p.is_moderator, owns=owns)
    if decision == "propose" and "proposals:write" not in p.scopes:
        raise Problem(403, "Insufficient scope", "Proposing a change needs the proposals:write scope.")
    # Validate now even for proposals so reviewers never see a patch that cannot apply.
    try:
        store.validate(kind, policy.merge_patch(row.doc, body.patch) | {"updated_at": _now_iso()})
    except ValidationError as exc:
        raise Problem(
            422,
            "Patch produces an invalid record",
            str(exc.errors()[0]["msg"]),
            errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
    change = Change(
        subject_type=kind,
        subject_id=rid,
        op="update",
        patch=body.patch,
        reason=body.reason,
        evidence_url=body.evidence_url,
        author_id=p.user.id,
        client_id=p.client_id,
        base_version=row.version,
    )
    db.add(change)
    if decision == "apply":
        doc = _apply_update(db, kind, row, body.patch)
        change.status, change.auto_applied, change.applied_at = "applied", True, utcnow()
        db.commit()
        response.headers["ETag"] = f'"{row.version}"'
        return {"status": "applied", "change_id": change.id, "version": row.version, "record": doc}
    db.commit()
    response.status_code = 202
    return {"status": "pending", "change_id": change.id, "detail": "Queued for moderator review."}


@collection_router.post("/{collection}", status_code=202, tags=["contribute"])
def create(
    collection: str,
    body: CreateIn,
    response: Response,
    p: Principal = Depends(require_user("proposals:write")),
    db: Session = Depends(get_session),
) -> dict:
    """Add a facility, practitioner or role (a doctor working somewhere). New records are reviewed,
    except a role linking a doctor you own to a facility you own."""
    kind = _kind(collection)
    rec = body.record
    try:
        store.validate(
            kind,
            _blank(
                kind,
                rec,
                {"facility": "JO-XX-OTH-99999", "practitioner": "JO-PRC-999999", "role": "JO-ROL-9999999"}[kind],
            ),
        )
    except ValidationError as exc:
        raise Problem(
            422,
            "Invalid record",
            str(exc.errors()[0]["msg"]),
            errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
    if kind == "role":
        for ref_kind, key in (("practitioner", "practitioner_id"), ("facility", "facility_id")):
            if store.get(db, ref_kind, rec[key]) is None:
                raise Problem(422, f"Unknown {ref_kind}", rec[key])
    change = Change(
        subject_type=kind,
        subject_id=None,
        op="create",
        patch=rec | ({"_claim": body.claim} if body.claim else {}),
        reason=body.reason,
        evidence_url=body.evidence_url,
        author_id=p.user.id,
        client_id=p.client_id,
    )
    db.add(change)
    owned = (
        owned_subjects(db, p.user) if "practitioner:manage" in p.scopes else {"practitioner": set(), "facility": set()}
    )
    auto = p.is_moderator or (
        kind == "role" and rec["practitioner_id"] in owned["practitioner"] and rec["facility_id"] in owned["facility"]
    )
    if auto:
        doc = _apply_create(db, kind, rec)
        change.subject_id, change.status, change.auto_applied, change.applied_at = doc["id"], "applied", True, utcnow()
        db.commit()
        response.status_code = 201
        return {"status": "applied", "change_id": change.id, "record": doc}
    db.commit()
    return {"status": "pending", "change_id": change.id, "detail": "Queued for moderator review."}


# --- proposals & moderation ----------------------------------------------------------------------


@router.get("/proposals", tags=["moderation"])
def list_proposals(
    status: str = "pending", p: Principal = Depends(require_user()), db: Session = Depends(get_session)
) -> dict:
    stmt = select(Change).where(Change.status == status).order_by(Change.id)
    if not p.is_moderator:
        stmt = stmt.where(Change.author_id == p.user.id)
    rows = db.scalars(stmt.limit(500)).all()
    return {"data": [_change_out(c) for c in rows], "count": len(rows)}


@router.post("/proposals/{change_id}/approve", tags=["moderation"])
def approve(
    change_id: int, body: DecisionIn, p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    change = db.get(Change, change_id)
    if change is None or change.status != "pending":
        raise Problem(404, "No pending proposal with that id")
    if change.op == "update":
        row = store.get(db, change.subject_type, change.subject_id)
        if row is None:
            raise Problem(409, "Subject no longer exists", change.subject_id)
        doc = _apply_update(db, change.subject_type, row, change.patch)
    else:
        record = {k: v for k, v in change.patch.items() if k != "_claim"}
        doc = _apply_create(db, change.subject_type, record)
        change.subject_id = doc["id"]
        if change.subject_type == "practitioner" and change.patch.get("_claim") is not None:
            db.add(
                Claim(
                    user_id=change.author_id,
                    subject_type="practitioner",
                    subject_id=doc["id"],
                    evidence=change.patch["_claim"],
                    status="approved",
                    reviewed_by=p.user.id,
                    reviewed_at=utcnow(),
                    note="approved with profile creation",
                )
            )
    change.status, change.reviewed_by, change.review_note, change.applied_at = "applied", p.user.id, body.note, utcnow()
    db.commit()
    return {"status": "applied", "change": _change_out(change), "record": doc}


@router.post("/proposals/{change_id}/reject", tags=["moderation"])
def reject(
    change_id: int, body: DecisionIn, p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    change = db.get(Change, change_id)
    if change is None or change.status != "pending":
        raise Problem(404, "No pending proposal with that id")
    change.status, change.reviewed_by, change.review_note = "rejected", p.user.id, body.note
    db.commit()
    return {"status": "rejected", "change": _change_out(change)}


# --- claims ----------------------------------------------------------------------------------------


@router.post("/claims", status_code=202, tags=["claims"])
def create_claim(
    body: ClaimIn, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    """Ask to manage a doctor profile (yours) or a facility (you administer it). A moderator checks the
    evidence — e.g. Jordan Medical Association membership / MoH licence number, or a work email on the
    facility's domain — before granting edit rights."""
    if store.get(db, body.subject_type, body.subject_id) is None:
        raise Problem(404, f"{body.subject_type} not found", body.subject_id)
    if not body.evidence:
        raise Problem(422, "Evidence required", "Provide e.g. license_number, work_email or document_url.")
    dup = db.scalar(
        select(Claim).where(
            Claim.user_id == p.user.id, Claim.subject_id == body.subject_id, Claim.status.in_(["pending", "approved"])
        )
    )
    if dup:
        raise Problem(409, "Claim already exists", f"Claim {dup.id} is {dup.status}.")
    claim = Claim(user_id=p.user.id, subject_type=body.subject_type, subject_id=body.subject_id, evidence=body.evidence)
    db.add(claim)
    db.commit()
    return {"id": claim.id, "status": claim.status}


@router.get("/claims", tags=["claims"])
def list_claims(
    status: str | None = None, p: Principal = Depends(require_user()), db: Session = Depends(get_session)
) -> dict:
    stmt = select(Claim).order_by(Claim.created_at)
    if not p.is_moderator:
        stmt = stmt.where(Claim.user_id == p.user.id)
    if status:
        stmt = stmt.where(Claim.status == status)
    rows = db.scalars(stmt).all()
    return {
        "data": [
            {
                "id": c.id,
                "user_id": c.user_id,
                "subject_type": c.subject_type,
                "subject_id": c.subject_id,
                "evidence": c.evidence if p.is_moderator or c.user_id == p.user.id else None,
                "status": c.status,
                "note": c.note,
                "created_at": c.created_at,
            }
            for c in rows
        ]
    }


@router.post("/claims/{claim_id}/{decision}", tags=["claims"])
def decide_claim(
    claim_id: str,
    decision: Literal["approve", "reject"],
    body: DecisionIn,
    p: Principal = Depends(require_moderator),
    db: Session = Depends(get_session),
) -> dict:
    claim = db.get(Claim, claim_id)
    if claim is None or claim.status != "pending":
        raise Problem(404, "No pending claim with that id")
    claim.status = "approved" if decision == "approve" else "rejected"
    claim.reviewed_by, claim.reviewed_at, claim.note = p.user.id, utcnow(), body.note
    if claim.status == "approved" and claim.subject_type == "practitioner":
        row = store.get(db, "practitioner", claim.subject_id)
        store.put(db, "practitioner", row.doc | {"license_verified": True, "updated_at": _now_iso()})
    db.commit()
    return {"id": claim.id, "status": claim.status}


# --- reviews -------------------------------------------------------------------------------------


def _recompute_rating(db: Session, kind: str, rid: str) -> dict:
    reviews = db.scalars(
        select(Review).where(Review.subject_type == kind, Review.subject_id == rid, Review.status == "visible")
    ).all()
    dims: dict[str, list[int]] = {}
    for r in reviews:
        for k, v in r.ratings.items():
            dims.setdefault(k, []).append(v)
    summary = {
        "overall": round(sum(dims["overall"]) / len(dims["overall"]), 2) if dims.get("overall") else None,
        "count": len(reviews),
        "dimensions": {k: round(sum(v) / len(v), 2) for k, v in sorted(dims.items()) if k != "overall"},
    }
    row = store.get(db, kind, rid)
    store.put(db, kind, row.doc | {"rating": summary, "updated_at": _now_iso()})
    return summary


@collection_router.post("/{collection}/{rid}/reviews", status_code=201, tags=["reviews"])
def post_review(
    collection: str,
    rid: str,
    body: ReviewIn,
    p: Principal = Depends(require_user("reviews:write")),
    db: Session = Depends(get_session),
) -> dict:
    """One review per user per place/doctor; posting again replaces your previous review."""
    kind = _kind(collection)
    if kind == "role":
        raise Problem(404, "Reviews are for facilities and practitioners")
    if store.get(db, kind, rid) is None:
        raise Problem(404, "Not found", rid)
    unknown = set(body.ratings) - DIMENSIONS
    if unknown or "overall" not in body.ratings:
        raise Problem(422, "Invalid ratings", f"`overall` is required; allowed dimensions: {sorted(DIMENSIONS)}")
    review = db.scalar(
        select(Review).where(Review.subject_type == kind, Review.subject_id == rid, Review.user_id == p.user.id)
    )
    if review is None:
        review = Review(subject_type=kind, subject_id=rid, user_id=p.user.id)
        db.add(review)
    review.ratings, review.text, review.visit_month, review.client_id = (
        body.ratings,
        body.text,
        body.visit_month,
        p.client_id,
    )
    review.status, review.created_at = "visible", utcnow()
    db.flush()
    summary = _recompute_rating(db, kind, rid)
    db.commit()
    return {"id": review.id, "rating": summary}


@collection_router.get("/{collection}/{rid}/reviews", tags=["reviews"])
def list_reviews(collection: str, rid: str, db: Session = Depends(get_session)) -> dict:
    kind = _kind(collection)
    rows = db.scalars(
        select(Review)
        .where(Review.subject_type == kind, Review.subject_id == rid, Review.status == "visible")
        .order_by(Review.created_at.desc())
        .limit(200)
    ).all()
    # Reviewer identity is never published; apps get the content, not the person.
    return {
        "data": [
            {"id": r.id, "ratings": r.ratings, "text": r.text, "visit_month": r.visit_month, "created_at": r.created_at}
            for r in rows
        ],
        "count": len(rows),
    }


@router.post("/reviews/{review_id}/hide", tags=["moderation"])
def hide_review(
    review_id: str, body: DecisionIn, p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    review = db.get(Review, review_id)
    if review is None:
        raise Problem(404, "Review not found")
    review.status = "hidden"
    db.flush()
    _recompute_rating(db, review.subject_type, review.subject_id)
    db.commit()
    return {"id": review.id, "status": "hidden"}
