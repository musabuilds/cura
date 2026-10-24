"""Live state: availability events, resolved availability, facility load.

Doctors and facility admins set these themselves and they apply at once — there is no moderator in
the loop, because "I'm off today" is only useful today. Only the people who own a record may write
its events; everything is readable by everyone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cura import availability
from cura.api import live, store
from cura.api.db import AvailabilityEvent, LoadSample, get_session, utcnow
from cura.api.deps import Principal, owned_subjects, principal, require_user
from cura.api.errors import Problem

router = APIRouter(prefix="/v1")
E = AvailabilityEvent
MAX_BACKDATE = timedelta(days=7)
MAX_LENGTH = timedelta(days=400)


class EventDetails(BaseModel):
    er: availability.ErState | None = Field(None, description="Emergency department, when the facility has one")
    departments: Annotated[
        list[Annotated[str, Field(max_length=80)]] | None,
        Field(max_length=50, description="Departments this event affects (closed, or the only ones open)"),
    ] = None


class EventIn(BaseModel):
    """One change to what is happening. Times without an offset are read as Amman local time."""

    subject_type: Literal["practitioner", "role", "facility"] = Field(
        description="practitioner = everywhere the doctor works; role = one doctor at one place; facility"
    )
    subject_id: str
    state: str = Field(
        description="practitioner/role: available | busy | unavailable · facility: open | limited | closed"
    )
    reason: str | None = Field(None, description="GET /v1/reference/availability lists the codes")
    note: Annotated[str | None, Field(max_length=160, description="Public, shown as written")] = None
    starts_at: datetime | None = Field(None, description="Default: now")
    ends_at: datetime | None = Field(None, description="Default: until changed")
    details: EventDetails | None = Field(None, description="Facilities only")

    @model_validator(mode="after")
    def _check(self) -> EventIn:
        if self.state not in availability.STATES[self.subject_type]:
            raise ValueError(f"state must be one of {sorted(availability.STATES[self.subject_type])}")
        if self.reason is not None and self.reason not in availability.REASONS[self.subject_type]:
            raise ValueError(f"reason must be one of {sorted(availability.REASONS[self.subject_type])}")
        if self.details is not None and self.subject_type != "facility":
            raise ValueError("details apply to facilities only")
        return self


class EndIn(BaseModel):
    at: datetime | None = Field(None, description="Default: now")


class LoadIn(BaseModel):
    level: Annotated[float, Field(ge=0, le=1, description="0 = empty, 1 = at capacity")]
    at: datetime | None = None


# --- authority -----------------------------------------------------------------------------------


def _subject(db: Session, subject_type: str, subject_id: str) -> tuple[dict, str | None, str | None]:
    """The subject's document plus the practitioner and facility it touches."""
    row = store.get(db, subject_type, subject_id)
    if row is None:
        raise Problem(404, f"{subject_type} not found", subject_id)
    doc = row.doc
    if doc.get("status") == "merged":
        raise Problem(409, f"{subject_type} was merged", f"Use {doc.get('replaced_by')} instead.")
    if subject_type == "practitioner":
        return doc, doc["id"], None
    if subject_type == "facility":
        return doc, None, doc["id"]
    return doc, doc["practitioner_id"], doc["facility_id"]


def _require_manager(db: Session, p: Principal, subject_type: str, practitioner_id, facility_id) -> None:
    """Doctors manage their own events and their roles'; facility admins their facility's and its roles'."""
    if p.is_moderator:
        return
    owned = owned_subjects(db, p.user)
    if subject_type == "practitioner" and practitioner_id in owned["practitioner"]:
        return
    if subject_type == "facility" and facility_id in owned["facility"]:
        return
    if subject_type == "role" and (practitioner_id in owned["practitioner"] or facility_id in owned["facility"]):
        return
    raise Problem(
        403,
        "Not your record",
        "Only the verified doctor or the facility's verified admin can set availability. Claim the record first.",
    )


# --- events --------------------------------------------------------------------------------------


@router.post("/availability", status_code=201, tags=["availability"])
def create_event(
    body: EventIn,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=64, description="Retries with the same key return the first event"),
    ] = None,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Set what is happening: "off everywhere until Sunday", "closed today, power outage",
    "available at my clinic, not at the hospital". Applies immediately; overrides the schedule."""
    if idempotency_key:
        prior = db.scalar(select(E).where(E.author_id == p.user.id, E.idempotency_key == idempotency_key))
        if prior is not None:
            response.status_code = 200
            return live.event_out(prior)
    _, practitioner_id, facility_id = _subject(db, body.subject_type, body.subject_id)
    _require_manager(db, p, body.subject_type, practitioner_id, facility_id)
    now = live.now()
    starts = live.utc(body.starts_at) if body.starts_at else now
    ends = live.utc(body.ends_at) if body.ends_at else None
    if starts < now - MAX_BACKDATE:
        raise Problem(422, "Too far in the past", "Events can start at most 7 days ago.")
    if ends is not None and ends <= starts:
        raise Problem(422, "Invalid period", "ends_at must be after starts_at.")
    if ends is not None and ends - starts > MAX_LENGTH:
        raise Problem(422, "Period too long", "Leave ends_at empty for an open-ended event instead.")
    event = E(
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        practitioner_id=practitioner_id,
        facility_id=facility_id,
        state=body.state,
        reason=body.reason,
        note=body.note,
        details=body.details.model_dump(exclude_none=True) if body.details else {},
        starts_at=starts,
        ends_at=ends,
        author_id=p.user.id,
        client_id=p.client_id,
        idempotency_key=idempotency_key,
        updated_at=utcnow(),
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:  # a concurrent retry with the same key won the race
        db.rollback()
        prior = db.scalar(select(E).where(E.author_id == p.user.id, E.idempotency_key == idempotency_key))
        if prior is None:
            raise
        response.status_code = 200
        return live.event_out(prior)
    return live.event_out(event)


def _owned_event(db: Session, p: Principal, event_id: str) -> AvailabilityEvent:
    event = db.get(E, event_id)
    if event is None:
        raise Problem(404, "Event not found", event_id)
    _require_manager(db, p, event.subject_type, event.practitioner_id, event.facility_id)
    if event.cancelled_at is not None:
        raise Problem(409, "Event was cancelled")
    return event


@router.post("/availability/{event_id}/end", tags=["availability"])
def end_event(
    event_id: str,
    body: EndIn,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """End an event early ("I'm back"). An event that has not started yet is cancelled instead."""
    event = _owned_event(db, p, event_id)
    at = live.utc(body.at) if body.at else live.now()
    starts, ends = availability.aware(event.starts_at), availability.aware(event.ends_at)
    if ends is not None and ends <= at:
        raise Problem(409, "Event already ended", f"It ended at {ends.isoformat()}.")
    if starts >= at:
        event.cancelled_at, event.cancelled_by = at, p.user.id
    else:
        event.ends_at = at
    event.updated_at = utcnow()
    db.commit()
    return live.event_out(event)


@router.post("/availability/{event_id}/cancel", tags=["availability"])
def cancel_event(
    event_id: str, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    """Withdraw an event set by mistake. It stays in the history, marked cancelled."""
    event = _owned_event(db, p, event_id)
    event.cancelled_at, event.cancelled_by, event.updated_at = live.now(), p.user.id, utcnow()
    db.commit()
    return live.event_out(event)


@router.get("/availability", tags=["availability"])
def list_events(
    practitioner_id: str | None = None,
    facility_id: str | None = None,
    role_id: str | None = None,
    start: Annotated[datetime | None, Query(alias="from", description="Default: 30 days ago")] = None,
    end: Annotated[datetime | None, Query(alias="to", description="Default: 90 days ahead")] = None,
    include_cancelled: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Events touching a doctor (everywhere they work), a facility (and every doctor there) or one role,
    overlapping [from, to]. Past events are kept: this is the record a model learns patterns from."""
    if not (practitioner_id or facility_id or role_id):
        raise Problem(422, "Choose a subject", "Pass practitioner_id, facility_id or role_id.")
    start = live.utc(start) if start else live.now() - timedelta(days=30)
    end = live.utc(end) if end else live.now() + timedelta(days=90)
    subjects = []
    if practitioner_id:
        subjects.append(E.practitioner_id == practitioner_id)
    if facility_id:
        subjects.append(E.facility_id == facility_id)
    if role_id:
        subjects.append(and_(E.subject_type == "role", E.subject_id == role_id))
    stmt = select(E).where(or_(*subjects), E.starts_at <= end, or_(E.ends_at.is_(None), E.ends_at > start))
    if not include_cancelled:
        stmt = stmt.where(E.cancelled_at.is_(None))
    rows = db.scalars(stmt.order_by(E.starts_at, E.id).limit(limit)).all()
    return {"data": [live.event_out(e) for e in rows], "count": len(rows)}


@router.get("/availability/changes", tags=["sync"])
def event_feed(
    since: datetime,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Every event created, ended or cancelled after `since`. Mirrors poll this beside `/v1/changes`."""
    rows = db.scalars(select(E).where(E.updated_at > live.utc(since)).order_by(E.updated_at, E.id).limit(limit)).all()
    out = [live.event_out(e) for e in rows]
    return {"data": out, "count": len(out), "next_since": out[-1]["updated_at"] if out else since}


# --- resolved state ------------------------------------------------------------------------------


@router.get("/practitioners/{practitioner_id}/availability", tags=["availability"])
def practitioner_availability(
    practitioner_id: str,
    at: datetime | None = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Is this doctor available, and where — now, or at `at`."""
    if store.get(db, "practitioner", practitioner_id) is None:
        raise Problem(404, "Practitioner not found", practitioner_id)
    at = live.utc(at) if at else live.now()
    roles = [r for r in store.roles_for(db, practitioner_id=practitioner_id) if r.get("confirmation") != "declined"]
    states = live.role_availability(db, roles, at)
    facilities = {d["id"]: d for d in store.docs(db, "facility", {r["facility_id"] for r in roles})}
    places = [
        {
            "role_id": r["id"],
            "facility": store.summary(facilities.get(r["facility_id"])),
            "active": r.get("active"),
            "availability": states[r["id"]],
        }
        for r in roles
    ]
    available_at = [x["facility"]["id"] for x in places if x["availability"]["state"] == "available" and x["facility"]]
    return {"practitioner_id": practitioner_id, "at": at, "available_at": available_at, "places": places}


@router.get("/facilities/{facility_id}/live", tags=["availability"])
def facility_live(
    facility_id: str,
    at: datetime | None = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """The facility's state and every confirmed doctor's availability there — now, or at `at`."""
    row = store.get(db, "facility", facility_id)
    if row is None:
        raise Problem(404, "Facility not found", facility_id)
    at = live.utc(at) if at else live.now()
    roles = [r for r in store.roles_for(db, facility_id=facility_id) if store.is_listed(r)]
    states = live.role_availability(db, roles, at)
    people = {d["id"]: d for d in store.docs(db, "practitioner", {r["practitioner_id"] for r in roles})}
    return {
        "facility_id": facility_id,
        "at": at,
        "live": live.facility_live(db, [row.doc], at)[facility_id],
        "practitioners": [
            {
                "role_id": r["id"],
                "practitioner_id": r["practitioner_id"],
                "name": (people.get(r["practitioner_id"]) or {}).get("name"),
                "specialty": r.get("specialty") or (people.get(r["practitioner_id"]) or {}).get("specialty"),
                "availability": states[r["id"]],
            }
            for r in roles
        ],
    }


@router.post("/facilities/{facility_id}/load", status_code=201, tags=["availability"])
def report_load(
    facility_id: str,
    body: LoadIn,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Staff report how busy the place is right now. Samples build the facility's busy-hours profile."""
    _require_manager(db, p, "facility", None, facility_id)
    if store.get(db, "facility", facility_id) is None:
        raise Problem(404, "Facility not found", facility_id)
    sample = LoadSample(
        facility_id=facility_id, at=live.utc(body.at) if body.at else live.now(), level=body.level, author_id=p.user.id
    )
    db.add(sample)
    db.commit()
    return {"id": sample.id, "facility_id": facility_id, "at": sample.at, "level": sample.level}


@router.get("/reference/availability", tags=["reference"])
def availability_vocabulary(_: Principal = Depends(principal)) -> dict:
    return {
        "states": {k: sorted(v) for k, v in availability.STATES.items()},
        "reasons": {k: sorted(v) for k, v in availability.REASONS.items()},
        "er": [s.value for s in availability.ErState],
    }
