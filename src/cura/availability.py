"""Availability: what is actually happening, on top of what the schedule says should happen.

Two layers, kept apart on purpose:

* **Schedule** — `hours` on a facility or a role. Slow-changing, part of the canonical record, and
  versioned with it (`record_versions`), so "this doctor used to start at 9 in winter" stays readable.
* **Events** — time-bounded overrides (`availability_events`): a doctor on leave for a week, a clinic
  closed for a storm until 8 am, a hospital under construction until further notice. Append-only.

Resolution answers "is this doctor available at this place at this moment?" by precedence:

1. the role has ended                         → unavailable (ended)
2. the facility is closed (status or event)   → unavailable (facility_closed)
3. the newest active event on the doctor or on this role → its state
   (a doctor-wide "off everywhere" and a per-place "but I'm at my clinic" are both events; the one
   set last wins, which is what a person toggling a switch expects)
4. the role's weekly hours                    → available / unavailable (outside_hours)
5. nothing known                              → unknown — never guessed as available

All functions here are pure: they take documents and events and a moment, and return a state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

AMMAN = ZoneInfo("Asia/Amman")
DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"]


class PractitionerState(StrEnum):
    available = "available"
    busy = "busy"  # at the facility but not taking new patients now (in theatre, running late)
    unavailable = "unavailable"


class FacilityState(StrEnum):
    open = "open"
    limited = "limited"  # open with reduced services: see `details.departments`, `details.er`
    closed = "closed"


class PractitionerReason(StrEnum):
    # Deliberately no medical reasons: an absence is published, its cause is the doctor's business.
    leave = "leave"
    conference = "conference"
    training = "training"
    procedure = "procedure"
    on_call = "on_call"
    personal = "personal"
    other = "other"


class FacilityReason(StrEnum):
    holiday = "holiday"
    maintenance = "maintenance"
    construction = "construction"
    power_outage = "power_outage"
    weather = "weather"
    staffing = "staffing"
    capacity = "capacity"
    emergency = "emergency"
    other = "other"


class ErState(StrEnum):
    accepting = "accepting"
    diverting = "diverting"
    closed = "closed"


STATES = {
    "practitioner": {s.value for s in PractitionerState},
    "role": {s.value for s in PractitionerState},
    "facility": {s.value for s in FacilityState},
}
REASONS = {
    "practitioner": {r.value for r in PractitionerReason},
    "role": {r.value for r in PractitionerReason},
    "facility": {r.value for r in FacilityReason},
}


@dataclass(frozen=True)
class Event:
    """The resolver's view of an `availability_events` row."""

    id: str
    subject_type: str
    subject_id: str
    state: str
    starts_at: datetime
    ends_at: datetime | None
    created_at: datetime
    reason: str | None = None
    note: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    cancelled_at: datetime | None = None

    def active_at(self, at: datetime) -> bool:
        if self.cancelled_at is not None and self.cancelled_at <= at:
            return False
        return self.starts_at <= at and (self.ends_at is None or at < self.ends_at)

    @classmethod
    def from_row(cls, row) -> Event:
        return cls(
            id=row.id,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            state=row.state,
            starts_at=aware(row.starts_at),
            ends_at=aware(row.ends_at),
            created_at=aware(row.created_at),
            reason=row.reason,
            note=row.note,
            details=row.details or {},
            cancelled_at=aware(row.cancelled_at),
        )


def aware(dt: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; everything Cura stores is UTC.
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def _newest(events: Iterable[Event], at: datetime) -> Event | None:
    live = [e for e in events if e.active_at(at)]
    return max(live, key=lambda e: (e.created_at, e.id)) if live else None


def within_hours(hours: dict | None, at: datetime) -> bool | None:
    """True/False from published weekly hours; None when the hours are unknown."""
    hours = hours or {}
    if hours.get("always_open"):
        return True
    weekly = hours.get("weekly")
    if not weekly:
        return None
    now = at.astimezone(AMMAN)
    day, hhmm = DAYS[now.weekday()], now.strftime("%H:%M")
    prev = DAYS[(now.weekday() - 1) % 7]
    for span in weekly:
        o, c = span["opens"], span["closes"]
        if span["day"] == day and (o <= hhmm < c or (c <= o and hhmm >= o)):
            return True
        if span["day"] == prev and c <= o and hhmm < c:  # a past-midnight span from yesterday
            return True
    return False


def _out(state: str, source: str, *, reason=None, note=None, until=None, event: Event | None = None, **extra):
    out = {"state": state, "source": source, "reason": reason, "note": note, "until": until}
    if event is not None:
        out |= {
            "reason": event.reason,
            "note": event.note,
            "until": event.ends_at,
            "event_id": event.id,
            "set_on": event.subject_type,
        }
    return out | extra


def resolve_facility(doc: dict, events: Iterable[Event], at: datetime) -> dict:
    """The facility's operating state at `at`. `events` may include other subjects; they are ignored."""
    status = doc.get("status")
    if status in ("permanently_closed", "merged"):
        return _out("closed", "status", reason=status)
    if status == "temporarily_closed":
        return _out("closed", "status", reason="temporarily_closed")
    ev = _newest((e for e in events if e.subject_type == "facility" and e.subject_id == doc["id"]), at)
    if ev is not None:
        return _out(ev.state, "event", event=ev, er=ev.details.get("er"), departments=ev.details.get("departments"))
    inside = within_hours(doc.get("hours"), at)
    if inside is None:
        return _out("unknown", "schedule")
    return _out("open", "schedule") if inside else _out("closed", "schedule", reason="outside_hours")


def resolve_role(role: dict, facility: dict | None, events: Iterable[Event], at: datetime) -> dict:
    """Whether the practitioner is available at this role's facility at `at`."""
    events = list(events)
    if role.get("active") is False:
        return _out("unavailable", "role", reason="ended")
    if facility is not None:
        place = resolve_facility(facility, events, at)
        if place["state"] == "closed" and place["source"] != "schedule":
            return _out("unavailable", "facility", reason="facility_closed", until=place["until"])
    mine = (
        e
        for e in events
        if (e.subject_type == "practitioner" and e.subject_id == role["practitioner_id"])
        or (e.subject_type == "role" and e.subject_id == role["id"])
    )
    ev = _newest(mine, at)
    if ev is not None:
        return _out(ev.state, "event", event=ev)
    inside = within_hours(role.get("hours"), at)
    if inside is None:
        return _out("unknown", "schedule")
    return _out("available", "schedule") if inside else _out("unavailable", "schedule", reason="outside_hours")
