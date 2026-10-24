"""Rating aggregates. A record's `rating` is always recomputed from the visible reviews, never edited."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import store, writes
from cura.api.db import Review


def recompute(db: Session, kind: str, rid: str) -> dict:
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
    store.put(db, kind, row.doc | {"rating": summary, "updated_at": writes.now_iso()})
    return summary
