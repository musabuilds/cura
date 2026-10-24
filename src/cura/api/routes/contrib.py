"""Write API: edits, proposals, ownership claims, reviews and moderation.

Every write is recorded as a `Change`. Whether it applies immediately is decided by `policy`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import contact, guard, matching, policy, ratings, store, writes
from cura.api.db import Change, Claim, Review, ReviewReport, RoleRow, get_session, utcnow
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
    confirm_new: bool = Field(
        False,
        description="Facilities and practitioners are checked for duplicates first. If the 409 lists none of "
        "yours, resend with true: it then goes to a moderator with the candidates attached.",
    )


class ReviewIn(BaseModel):
    ratings: dict[str, Annotated[int, Field(ge=1, le=5)]]
    text: Annotated[str | None, Field(max_length=4000)] = None
    visit_month: Annotated[str | None, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None


class DecisionIn(BaseModel):
    note: Annotated[str | None, Field(max_length=2000)] = None


class ReportIn(BaseModel):
    reason: Literal["spam", "abusive", "conflict_of_interest", "personal_data", "off_topic"]
    note: Annotated[str | None, Field(max_length=2000)] = None


def _kind(collection: str) -> str:
    if collection not in COLLECTION:
        raise Problem(404, "Unknown collection", f"One of: {', '.join(COLLECTION)}")
    return COLLECTION[collection]


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
    if row.doc.get("status") == "merged":
        raise Problem(409, f"{kind} was merged", f"Edit {row.doc.get('replaced_by')} instead.")
    owns = writes.owns(db, p, kind, row.doc)
    patch = dict(body.patch)
    if kind == "practitioner" and "contact" in patch:
        # Private numbers never reach the Change log or the record: store them aside, keep the public part.
        if not (owns or p.is_moderator):
            raise Problem(403, "Not your profile", "Only the doctor can change their contact details.")
        patch["contact"] = contact.apply(db, rid, patch["contact"], p.user.id)
    body = body.model_copy(update={"patch": patch})
    decision = policy.decide(kind, body.patch, is_moderator=p.is_moderator, owns=owns)
    if decision == "propose" and "proposals:write" not in p.scopes:
        raise Problem(403, "Insufficient scope", "Proposing a change needs the proposals:write scope.")
    writes.check_update(kind, row.doc, body.patch)
    change = writes.record_change(
        db,
        p,
        kind,
        rid,
        "update",
        body.patch,
        reason=body.reason,
        evidence_url=body.evidence_url,
        base_version=row.version,
    )
    if decision == "apply":
        doc = writes.apply_update(db, kind, row, body.patch, change.id)
        writes.mark_applied(change)
        db.commit()
        response.headers["ETag"] = f'"{row.version}"'
        return {"status": "applied", "change_id": change.id, "version": row.version, "record": doc}
    db.commit()
    response.status_code = 202
    return {
        "status": "proposed",
        "proposal_id": change.id,
        "change_id": change.id,
        "detail": "Queued for moderator review.",
    }


@collection_router.post("/{collection}", status_code=202, tags=["contribute"])
def create(
    collection: str,
    body: CreateIn,
    response: Response,
    p: Principal = Depends(require_user("proposals:write")),
    db: Session = Depends(get_session),
) -> dict:
    """Add a facility, practitioner or role (a doctor working somewhere).

    * **Facilities and practitioners** are checked against the registry first; a likely duplicate
      returns **409** with the candidates, so the caller can claim or edit the existing record instead
      of creating a second one. New ones are reviewed by a moderator.
    * **Roles** apply at once when you own either side: a doctor adding a workplace, or a facility
      adding a doctor. When the other side has a verified owner too, the role waits for their
      confirmation (`POST /v1/roles/{id}/confirmation`) and is hidden from the public roster until then.
    """
    kind = _kind(collection)
    rec = body.record
    writes.check_create(kind, rec)
    duplicates: list[dict] = []
    system: dict = {}
    auto = p.is_moderator
    if kind == "role":
        for ref_kind, key in (("practitioner", "practitioner_id"), ("facility", "facility_id")):
            if store.get(db, ref_kind, rec[key]) is None:
                raise Problem(422, f"Unknown {ref_kind}", rec[key])
        existing = db.scalar(
            select(RoleRow).where(
                RoleRow.practitioner_id == rec["practitioner_id"], RoleRow.facility_id == rec["facility_id"]
            )
        )
        if existing is not None:
            raise Problem(
                409,
                "Role already exists",
                f"{existing.id} links this doctor and facility. PATCH it (e.g. active: true) to rejoin.",
                existing_id=existing.id,
            )
        owned = (
            owned_subjects(db, p.user)
            if "practitioner:manage" in p.scopes
            else {"practitioner": set(), "facility": set()}
        )
        owns_p, owns_f = rec["practitioner_id"] in owned["practitioner"], rec["facility_id"] in owned["facility"]
        auto = auto or owns_p or owns_f
        system["confirmation"] = role_confirmation(db, p, rec, owns_p=owns_p, owns_f=owns_f)
    elif kind == "facility":
        duplicates = matching.facility_matches(db, rec)
    else:
        duplicates = matching.practitioner_matches(
            db, names=[(rec.get("name") or {}).get(k) for k in ("en", "ar")], specialty=rec.get("specialty")
        )
    if duplicates and not body.confirm_new:
        raise Problem(
            409,
            "Possible duplicate",
            "Records like this one already exist. Claim or edit one of them, set it as `parent` for a branch, "
            "or resend with confirm_new: true if none is the same.",
            candidates=duplicates,
        )
    if duplicates:
        auto = False  # a person said "not a duplicate" against the matcher: a moderator looks
    patch = rec | ({"_claim": body.claim} if body.claim else {})
    if duplicates:
        patch["_candidates"] = [c["id"] for c in duplicates]
    change = writes.record_change(
        db, p, kind, None, "create", patch, reason=body.reason, evidence_url=body.evidence_url
    )
    if auto:
        doc = writes.apply_create(db, kind, rec, change.id, system=system)
        change.subject_id = doc["id"]
        writes.mark_applied(change)
        db.commit()
        response.status_code = 201
        return {"status": "applied", "change_id": change.id, "record": doc}
    db.commit()
    return {"status": "pending", "change_id": change.id, "detail": "Queued for moderator review."}


def role_confirmation(db: Session, p: Principal, rec: dict, *, owns_p: bool, owns_f: bool) -> str:
    """Who still has to agree. A side with no verified owner cannot object, so it does not hold the role."""
    if p.is_moderator or (owns_p and owns_f):
        return "confirmed"
    if owns_p and (
        writes.has_owner(db, "facility", rec["facility_id"])
        or writes.unverified_owner(db, p, "practitioner", rec["practitioner_id"])
    ):
        return "pending_facility"
    if owns_f and (
        writes.has_owner(db, "practitioner", rec["practitioner_id"])
        or writes.unverified_owner(db, p, "facility", rec["facility_id"])
    ):
        return "pending_practitioner"
    return "confirmed"


# --- proposals & moderation ----------------------------------------------------------------------


@router.get("/proposals", tags=["moderation"])
def list_proposals(
    status: str = "pending", p: Principal = Depends(require_user()), db: Session = Depends(get_session)
) -> dict:
    stmt = select(Change).where(Change.status == status).order_by(Change.id)
    if not p.is_moderator:
        stmt = stmt.where(Change.author_id == p.user.id)
    rows = db.scalars(stmt.limit(500)).all()
    return {"data": [writes.change_out(c) for c in rows], "count": len(rows)}


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
        doc = writes.apply_update(db, change.subject_type, row, change.patch, change.id)
    else:
        record = {k: v for k, v in change.patch.items() if not k.startswith("_")}
        doc = writes.apply_create(db, change.subject_type, record, change.id)
        change.subject_id = doc["id"]
        if change.subject_type == "practitioner" and change.patch.get("_claim") is not None:
            db.add(
                Claim(
                    user_id=change.author_id,
                    subject_type="practitioner",
                    subject_id=doc["id"],
                    evidence=change.patch["_claim"],
                    status="approved",
                    method="with_profile",
                    reviewed_by=p.user.id,
                    reviewed_at=utcnow(),
                    note="approved with profile creation",
                )
            )
    change.status, change.reviewed_by, change.review_note, change.applied_at = "applied", p.user.id, body.note, utcnow()
    db.commit()
    return {"status": "applied", "change": writes.change_out(change), "record": doc}


@router.post("/proposals/{change_id}/reject", tags=["moderation"])
def reject(
    change_id: int, body: DecisionIn, p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    change = db.get(Change, change_id)
    if change is None or change.status != "pending":
        raise Problem(404, "No pending proposal with that id")
    change.status, change.reviewed_by, change.review_note = "rejected", p.user.id, body.note
    db.commit()
    return {"status": "rejected", "change": writes.change_out(change)}


# --- claims ----------------------------------------------------------------------------------------
# POST /v1/claims lives in routes/onboarding.py (phone match, evidence, disputes).


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
                "method": c.method,
                "level": c.level,
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
    if claim.status == "approved":
        claim.level, claim.verified_at = "reviewed", utcnow()
        # The moderator's decision is the ownership: a won dispute takes the record from its previous owner,
        # and a reviewed claim replaces the claimant's own `created` claim.
        others = select(Claim).where(
            Claim.subject_type == claim.subject_type,
            Claim.subject_id == claim.subject_id,
            Claim.status == "approved",
            Claim.id != claim.id,
        )
        for other in db.scalars(others):
            if other.user_id == claim.user_id or claim.method == "dispute":
                other.status, other.note = "superseded", f"superseded by claim {claim.id}"
    if claim.status == "approved" and claim.subject_type == "practitioner":
        row = store.get(db, "practitioner", claim.subject_id)
        store.put(db, "practitioner", row.doc | {"license_verified": True, "updated_at": writes.now_iso()})
    guard.audit(
        db,
        f"claim.{claim.status}",
        actor_id=p.user.id,
        subject_type=claim.subject_type,
        subject_id=claim.subject_id,
        method=claim.method,
        claimant=claim.user_id,
    )
    db.commit()
    return {"id": claim.id, "status": claim.status}


# --- reviews -------------------------------------------------------------------------------------


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
    summary = ratings.recompute(db, kind, rid)
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


@router.post("/reviews/{review_id}/reports", status_code=201, tags=["reviews"])
def report_review(
    review_id: str, body: ReportIn, p: Principal = Depends(require_user()), db: Session = Depends(get_session)
) -> dict:
    """Flag a review for a moderator. Reviews belong to the patients who wrote them: a facility or a
    doctor cannot edit or delete one, only report it (spam, abuse, a competitor, personal data)."""
    review = db.get(Review, review_id)
    if review is None or review.status != "visible":
        raise Problem(404, "Review not found")
    if review.user_id == p.user.id:
        raise Problem(422, "Your own review", "Post again to replace it instead.")
    report = db.scalar(
        select(ReviewReport).where(ReviewReport.review_id == review_id, ReviewReport.reporter_id == p.user.id)
    )
    if report is not None:
        raise Problem(409, "Already reported", f"Report {report.id} is {report.status}.")
    report = ReviewReport(review_id=review_id, reporter_id=p.user.id, reason=body.reason, note=body.note)
    db.add(report)
    db.commit()
    return {"id": report.id, "status": report.status}


@router.get("/review-reports", tags=["moderation"])
def list_reports(
    status: str = "open", p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    rows = db.scalars(select(ReviewReport).where(ReviewReport.status == status).order_by(ReviewReport.created_at)).all()
    reviews = {r.id: r for r in db.scalars(select(Review).where(Review.id.in_({x.review_id for x in rows})))}
    return {
        "data": [
            {
                "id": r.id,
                "reason": r.reason,
                "note": r.note,
                "status": r.status,
                "created_at": r.created_at,
                "review": {
                    "id": r.review_id,
                    "subject_type": reviews[r.review_id].subject_type,
                    "subject_id": reviews[r.review_id].subject_id,
                    "ratings": reviews[r.review_id].ratings,
                    "text": reviews[r.review_id].text,
                    "status": reviews[r.review_id].status,
                },
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.post("/review-reports/{report_id}/{decision}", tags=["moderation"])
def resolve_report(
    report_id: str,
    decision: Literal["uphold", "dismiss"],
    body: DecisionIn,
    p: Principal = Depends(require_moderator),
    db: Session = Depends(get_session),
) -> dict:
    """Upholding hides the review (and every other open report on it is upheld with it)."""
    report = db.get(ReviewReport, report_id)
    if report is None or report.status != "open":
        raise Problem(404, "No open report with that id")
    siblings = [report]
    if decision == "uphold":
        siblings = db.scalars(
            select(ReviewReport).where(ReviewReport.review_id == report.review_id, ReviewReport.status == "open")
        ).all()
        review = db.get(Review, report.review_id)
        review.status = "hidden"
        db.flush()
        ratings.recompute(db, review.subject_type, review.subject_id)
    for r in siblings:
        r.status = "upheld" if decision == "uphold" else "dismissed"
        r.resolved_by, r.resolved_at, r.note = p.user.id, utcnow(), body.note or r.note
    db.commit()
    return {"id": report.id, "status": report.status}


@router.post("/reviews/{review_id}/hide", tags=["moderation"])
def hide_review(
    review_id: str, body: DecisionIn, p: Principal = Depends(require_moderator), db: Session = Depends(get_session)
) -> dict:
    review = db.get(Review, review_id)
    if review is None:
        raise Problem(404, "Review not found")
    review.status = "hidden"
    db.flush()
    ratings.recompute(db, review.subject_type, review.subject_id)
    db.commit()
    return {"id": review.id, "status": "hidden"}
