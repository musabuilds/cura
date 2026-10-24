"""Identity: finding the existing record before creating one, and merging two records that are one.

    GET  /v1/practitioners/match     "which of these is me?" — a doctor after signing in
    GET  /v1/facilities/match        "is our hospital already here?" — an admin before creating one
    POST /v1/{collection}/{id}/merge a moderator folds a duplicate into the record that survives

A merge never deletes. The duplicate becomes a tombstone (`status: merged`, `replaced_by`), its roles,
claims, reviews and ongoing availability move to the survivor, and the decision is exported to
`sources/curation/merges.jsonl` so the next pipeline build makes the same merge.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from cura.api import live, matching, ratings, store, writes
from cura.api.db import AvailabilityEvent, Claim, Embedding, Invitation, Review, RoleRow, get_session, utcnow
from cura.api.deps import Principal, principal, require_moderator
from cura.api.errors import Problem

router = APIRouter(prefix="/v1")
COLLECTION = {"facilities": "facility", "practitioners": "practitioner"}


class MergeIn(BaseModel):
    into: str = Field(description="The record that survives")
    reason: Annotated[str, Field(min_length=3, max_length=2000)]


@router.get("/practitioners/match", tags=["identity"])
def match_practitioner(
    name: Annotated[str, Query(min_length=2, description="Full name, Arabic or English, titles allowed")],
    name_alt: Annotated[str | None, Query(description="The name in the other language")] = None,
    specialty: str | None = None,
    facility_id: Annotated[str | None, Query(description="A place they work: separates namesakes")] = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Candidates for "this is me", best first, with the reasons. `ambiguous` marks two-part names,
    which many people share: ask for a workplace or specialty before trusting the top result."""
    data = matching.practitioner_matches(db, names=[name, name_alt], specialty=specialty, facility_id=facility_id)
    return {"data": data, "count": len(data)}


@router.get("/facilities/match", tags=["identity"])
def match_facility(
    name: Annotated[str, Query(min_length=2)],
    type: str = "other",
    near: Annotated[str | None, Query(description="lat,lon")] = None,
    governorate: str | None = None,
    phone: str | None = None,
    website: str | None = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Existing facilities that may be this one. `same_place` is true when the registry's own
    entity-resolution rules would merge them; a branch of a chain is a separate place (`parent`)."""
    point = store.parse_near(near)
    if near and point is None:
        raise Problem(422, "Invalid near", "Use near=<lat>,<lon>")
    gov = None
    if governorate:
        gov = governorate if governorate.startswith("JO-") else f"JO-{governorate.upper()}"
    record = {
        "type": type,
        "name": {"ar" if any("؀" <= c <= "ۿ" for c in name) else "en": name},
        "address": {"governorate": gov},
        "location": {"lat": point[0], "lon": point[1]} if point else None,
        "contact": {"phones": [phone] if phone else None, "website": website},
    }
    data = matching.facility_matches(db, record)
    return {"data": data, "count": len(data)}


@router.post("/{collection}/{rid}/merge", tags=["identity"])
def merge(
    collection: str,
    rid: str,
    body: MergeIn,
    p: Principal = Depends(require_moderator),
    db: Session = Depends(get_session),
) -> dict:
    """Fold a duplicate into the record that survives (moderators)."""
    if collection not in COLLECTION:
        raise Problem(404, "Facilities and practitioners can be merged")
    kind = COLLECTION[collection]
    src, dst = store.get(db, kind, rid), store.get(db, kind, body.into)
    if src is None or dst is None:
        raise Problem(404, "Not found", rid if src is None else body.into)
    if rid == body.into:
        raise Problem(422, "A record cannot merge into itself")
    for row in (src, dst):
        if row.doc.get("status") == "merged":
            raise Problem(409, "Already merged", f"{row.id} was merged into {row.doc.get('replaced_by')}.")
    change = writes.record_change(db, p, kind, rid, "merge", {"into": body.into}, reason=body.reason)
    moved = merge_records(db, kind, src, dst, change.id)
    writes.mark_applied(change, auto=False, reviewer=p.user.id)
    db.commit()
    return {"status": "applied", "change_id": change.id, "merged": rid, "into": body.into, "moved": moved}


def merge_records(db: Session, kind: str, src, dst, change_id: int) -> dict:
    now = writes.now_iso()
    s, d = src.doc, dst.doc
    names = {n for n in (d["name"].get("en"), d["name"].get("ar")) if n}
    aliases = list(
        dict.fromkeys(
            [
                *d["name"].get("aliases", []),
                *(n for n in (s["name"].get("en"), s["name"].get("ar")) if n),
                *s["name"].get("aliases", []),
            ]
        )
    )
    survivor = d | {
        "name": d["name"] | {"aliases": [a for a in aliases if a not in names]},
        "identifiers": s.get("identifiers", {}) | d.get("identifiers", {}),
        "sources": d.get("sources", []) + [x for x in s.get("sources", []) if x not in d.get("sources", [])],
        "updated_at": now,
    }
    store.put(db, kind, survivor, change_id=change_id)
    store.put(db, kind, s | {"status": "merged", "replaced_by": dst.id, "updated_at": now}, change_id=change_id)
    moved = {"roles": 0, "roles_ended": 0, "claims": 0, "reviews": 0, "events": 0}

    # Roles: one per (practitioner, facility). A clash means both records already had it: end the copy.
    key, other = ("practitioner_id", "facility_id") if kind == "practitioner" else ("facility_id", "practitioner_id")
    column = getattr(RoleRow, key)
    for role in db.scalars(select(RoleRow).where(column == src.id)).all():
        clash = db.scalar(select(RoleRow).where(column == dst.id, getattr(RoleRow, other) == role.doc[other]))
        if clash is not None:
            store.put(db, "role", role.doc | {"active": False, "updated_at": now}, change_id=change_id)
            moved["roles_ended"] += 1
        else:
            store.put(db, "role", role.doc | {key: dst.id, "updated_at": now}, change_id=change_id)
            moved["roles"] += 1

    # Ownership: claims follow the record; an owner of both keeps one.
    for claim in db.scalars(select(Claim).where(Claim.subject_type == kind, Claim.subject_id == src.id)).all():
        twin = db.scalar(
            select(Claim).where(Claim.subject_id == dst.id, Claim.user_id == claim.user_id, Claim.id != claim.id)
        )
        if twin is not None:
            claim.status, claim.note = "superseded", f"merged into {dst.id}"
        else:
            claim.subject_id = dst.id
            moved["claims"] += 1
    for inv in db.scalars(select(Invitation).where(Invitation.practitioner_id == src.id)).all():
        inv.revoked_at = inv.revoked_at or utcnow()

    # Reviews belong to their authors and move. Someone who reviewed both keeps their newer words on
    # the survivor; the other review stays on the tombstone, marked merged.
    for review in db.scalars(select(Review).where(Review.subject_type == kind, Review.subject_id == src.id)).all():
        twin = db.scalar(
            select(Review).where(
                Review.subject_type == kind, Review.subject_id == dst.id, Review.user_id == review.user_id
            )
        )
        if twin is None:
            review.subject_id = dst.id
            moved["reviews"] += 1
            continue
        if review.created_at > twin.created_at:
            twin.ratings, twin.text, twin.visit_month = review.ratings, review.text, review.visit_month
            twin.created_at, twin.status = review.created_at, review.status
        review.status = "merged"
    db.flush()
    ratings.recompute(db, kind, dst.id)

    # Live state: past events stay on the tombstone (they are its history); ongoing ones move.
    ongoing = select(AvailabilityEvent).where(
        AvailabilityEvent.cancelled_at.is_(None),
        or_(AvailabilityEvent.ends_at.is_(None), AvailabilityEvent.ends_at > live.now()),
        getattr(AvailabilityEvent, f"{kind}_id") == src.id,
    )
    for event in db.scalars(ongoing).all():
        setattr(event, f"{kind}_id", dst.id)
        if event.subject_type == kind:
            event.subject_id = dst.id
        event.updated_at = utcnow()
        moved["events"] += 1

    embedding = db.get(Embedding, (kind, src.id))
    if embedding is not None:
        db.delete(embedding)  # derived data; the survivor is re-embedded on its next sync
    return moved
