"""Record history: every version of a facility, practitioner or role, and the record as it was at a moment.

Nothing in the registry is overwritten. A doctor changing their hours closes the old version and
opens a new one; both stay readable here, so apps can show "hours changed on 3 Sep" and a model can
learn that a clinic shortens its hours every summer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura import availability, patterns
from cura.api import live, store
from cura.api.db import Change, LoadSample, get_session
from cura.api.deps import Principal, principal
from cura.api.errors import Problem

router = APIRouter(prefix="/v1")
COLLECTION = {"facilities": "facility", "practitioners": "practitioner", "roles": "role"}


def _kind(collection: str) -> str:
    if collection not in COLLECTION:
        raise Problem(404, "Unknown collection", f"One of: {', '.join(COLLECTION)}")
    return COLLECTION[collection]


@router.get("/{collection}/{rid}/history", tags=["history"])
def record_history(
    collection: str,
    rid: str,
    as_of: Annotated[datetime | None, Query(description="Return only the version current at this instant")] = None,
    before_version: Annotated[int | None, Query(ge=1, description="Page: versions older than this one")] = None,
    include_docs: Annotated[bool, Query(description="Include each version's full document")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Newest first. Each version lists the fields that changed, when, and through which change."""
    kind = _kind(collection)
    if store.get(db, kind, rid) is None:
        raise Problem(404, "Not found", rid)
    if as_of is not None:
        version = store.as_of(db, kind, rid, live.utc(as_of))
        if version is None:
            raise Problem(404, "No version at that time", f"{rid} has no recorded version at {as_of.isoformat()}.")
        return {"data": [_out(version, None, include_docs=True, db=db)], "count": 1}
    versions = store.history(db, kind, rid, limit=limit + 1, before=before_version)
    page, more = versions[:limit], len(versions) > limit
    out = [
        _out(v, versions[i + 1] if i + 1 < len(versions) else None, include_docs=include_docs, db=db)
        for i, v in enumerate(page)
    ]
    return {"data": out, "count": len(out), "next_before_version": page[-1].version if more else None}


def _out(v, previous, *, include_docs: bool, db: Session) -> dict:
    change = db.get(Change, v.change_id) if v.change_id else None
    out = {
        "version": v.version,
        "valid_from": v.valid_from,
        "valid_to": v.valid_to,
        "origin": v.origin,
        "changed": store.diff_paths(previous.doc, v.doc) if previous is not None else None,
        "change": {"id": change.id, "op": change.op, "reason": change.reason, "client_id": change.client_id}
        if change
        else None,
    }
    if include_docs:
        out["doc"] = v.doc
    return out


# --- patterns ------------------------------------------------------------------------------------


Window = Annotated[int, Query(ge=7, le=3650, description="How many days of history to read")]


def _schedule(db: Session, kind: str, rid: str, now) -> dict:
    versions = store.history(db, kind, rid, limit=10_000)
    return patterns.schedules(
        [(availability.aware(v.valid_from), availability.aware(v.valid_to), v.doc) for v in versions], now=now
    )


@router.get("/practitioners/{practitioner_id}/patterns", tags=["history"])
def practitioner_patterns(
    practitioner_id: str, days: Window = 365, _: Principal = Depends(principal), db: Session = Depends(get_session)
) -> dict:
    """How this doctor's availability and hours behave over time: absence by month and weekday,
    schedule changes, typical start and end per season, per workplace. Features, not forecasts."""
    if store.get(db, "practitioner", practitioner_id) is None:
        raise Problem(404, "Practitioner not found", practitioner_id)
    now = live.now()
    start = now - timedelta(days=days)
    roles = store.roles_for(db, practitioner_id=practitioner_id)
    events = live.events_for(
        db, practitioner_ids=[practitioner_id], role_ids=[r["id"] for r in roles], start=start, end=now
    )
    everywhere = [e for e in events if e.subject_type == "practitioner"]
    return {
        "practitioner_id": practitioner_id,
        "window": {"from": start, "to": now},
        "absence": patterns.absences(everywhere, states={"unavailable"}, start=start, end=now),
        "roles": [
            {
                "role_id": r["id"],
                "facility_id": r["facility_id"],
                "active": r.get("active"),
                "absence": patterns.absences(
                    everywhere + [e for e in events if e.subject_id == r["id"]],
                    states={"unavailable"},
                    start=start,
                    end=now,
                ),
                "schedule": _schedule(db, "role", r["id"], now),
            }
            for r in roles
        ],
    }


@router.get("/facilities/{facility_id}/patterns", tags=["history"])
def facility_patterns(
    facility_id: str, days: Window = 365, _: Principal = Depends(principal), db: Session = Depends(get_session)
) -> dict:
    """Closures and reduced service by month and weekday, opening-hours history by season, and the busy
    profile staff have reported (mean load per weekday and hour)."""
    if store.get(db, "facility", facility_id) is None:
        raise Problem(404, "Facility not found", facility_id)
    now = live.now()
    start = now - timedelta(days=days)
    events = live.events_for(db, facility_ids=[facility_id], start=start, end=now)
    samples = db.execute(
        select(LoadSample.at, LoadSample.level).where(LoadSample.facility_id == facility_id, LoadSample.at >= start)
    ).all()
    return {
        "facility_id": facility_id,
        "window": {"from": start, "to": now},
        "closed": patterns.absences(events, states={"closed"}, start=start, end=now),
        "limited": patterns.absences(events, states={"limited"}, start=start, end=now),
        "schedule": _schedule(db, "facility", facility_id, now),
        "load": patterns.load_profile([(availability.aware(at), level) for at, level in samples]),
    }
