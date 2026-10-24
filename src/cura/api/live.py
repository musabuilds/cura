"""Loading availability events and resolving live state for many records at once.

The rules live in `cura.availability`; this module only fetches what they need, in as few queries
as possible, so a facility page with forty doctors costs three queries, not forty.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cura import availability
from cura.api.db import AvailabilityEvent, FacilityRow

E = AvailabilityEvent


def utc(dt: datetime) -> datetime:
    """Every datetime is bound to the database in UTC. SQLite compares them as text."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=availability.AMMAN)  # a bare local time from a person or a model
    return dt.astimezone(UTC)


def now() -> datetime:
    return datetime.now(UTC)


def events_for(
    db: Session,
    *,
    practitioner_ids: Iterable[str] = (),
    role_ids: Iterable[str] = (),
    facility_ids: Iterable[str] = (),
    start: datetime,
    end: datetime | None = None,
    include_cancelled: bool = False,
) -> list[availability.Event]:
    """Events on any of these subjects that overlap [start, end] (a single instant when `end` is None)."""
    start = utc(start)
    end = utc(end) if end else start
    subjects = [
        and_(E.subject_type == kind, E.subject_id.in_(ids))
        for kind, ids in (
            ("practitioner", set(practitioner_ids)),
            ("role", set(role_ids)),
            ("facility", set(facility_ids)),
        )
        if ids
    ]
    if not subjects:
        return []
    stmt = select(E).where(or_(*subjects), E.starts_at <= end, or_(E.ends_at.is_(None), E.ends_at > start))
    if not include_cancelled:
        stmt = stmt.where(or_(E.cancelled_at.is_(None), E.cancelled_at > start))
    return [availability.Event.from_row(r) for r in db.scalars(stmt)]


def facility_live(db: Session, docs: list[dict], at: datetime) -> dict[str, dict]:
    events = events_for(db, facility_ids=[d["id"] for d in docs], start=at)
    return {d["id"]: availability.resolve_facility(d, events, at) for d in docs}


def role_availability(db: Session, roles: list[dict], at: datetime) -> dict[str, dict]:
    if not roles:
        return {}
    facility_ids = {r["facility_id"] for r in roles}
    facilities = {r.id: r.doc for r in db.scalars(select(FacilityRow).where(FacilityRow.id.in_(facility_ids)))}
    events = events_for(
        db,
        practitioner_ids={r["practitioner_id"] for r in roles},
        role_ids={r["id"] for r in roles},
        facility_ids=facility_ids,
        start=at,
    )
    return {r["id"]: availability.resolve_role(r, facilities.get(r["facility_id"]), events, at) for r in roles}


def event_out(e: AvailabilityEvent) -> dict:
    return {
        "id": e.id,
        "subject_type": e.subject_type,
        "subject_id": e.subject_id,
        "practitioner_id": e.practitioner_id,
        "facility_id": e.facility_id,
        "state": e.state,
        "reason": e.reason,
        "note": e.note,
        "details": e.details or {},
        "starts_at": availability.aware(e.starts_at),
        "ends_at": availability.aware(e.ends_at),
        "created_at": availability.aware(e.created_at),
        "updated_at": availability.aware(e.updated_at),
        "cancelled_at": availability.aware(e.cancelled_at),
        "client_id": e.client_id,
    }
