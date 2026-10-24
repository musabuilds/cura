"""The resolution rules, without a database."""

from datetime import UTC, datetime, timedelta

from conftest import CLINIC, HOSPITAL, ROLE_CLINIC, ROLE_HOSPITAL
from cura.availability import Event, resolve_facility, resolve_role, within_hours

# Tuesday 2026-10-27 10:00 in Amman (UTC+3)
TUE_10 = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
TUE_20 = datetime(2026, 10, 27, 17, 0, tzinfo=UTC)


def ev(subject_type, subject_id, state, *, start=TUE_10 - timedelta(hours=1), end=None, created=0, **kw):
    return Event(
        id=f"e{created}",
        subject_type=subject_type,
        subject_id=subject_id,
        state=state,
        starts_at=start,
        ends_at=end,
        created_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=created),
        **kw,
    )


def test_schedule_decides_when_nothing_is_happening():
    assert resolve_role(ROLE_CLINIC, CLINIC, [], TUE_10)["state"] == "available"
    off = resolve_role(ROLE_CLINIC, CLINIC, [], TUE_20)
    assert (off["state"], off["reason"], off["source"]) == ("unavailable", "outside_hours", "schedule")


def test_unknown_hours_are_never_guessed_available():
    role = ROLE_CLINIC | {"hours": {}}
    assert resolve_role(role, CLINIC, [], TUE_10)["state"] == "unknown"
    assert within_hours({}, TUE_10) is None


def test_leave_everywhere_applies_to_every_role():
    leave = ev("practitioner", "JO-PRC-000001", "unavailable", reason="leave", end=TUE_10 + timedelta(days=3))
    for role, place in ((ROLE_HOSPITAL, HOSPITAL), (ROLE_CLINIC, CLINIC)):
        out = resolve_role(role, place, [leave], TUE_10)
        assert (out["state"], out["reason"], out["set_on"]) == ("unavailable", "leave", "practitioner")
        assert out["until"] == TUE_10 + timedelta(days=3)


def test_newest_event_wins_away_from_hospital_but_at_own_clinic():
    away = ev("practitioner", "JO-PRC-000001", "unavailable", reason="leave", created=1)
    clinic = ev("role", ROLE_CLINIC["id"], "available", created=2)
    assert resolve_role(ROLE_HOSPITAL, HOSPITAL, [away, clinic], TUE_10)["state"] == "unavailable"
    assert resolve_role(ROLE_CLINIC, CLINIC, [away, clinic], TUE_10)["state"] == "available"


def test_events_outside_their_window_do_not_apply():
    later = ev("practitioner", "JO-PRC-000001", "unavailable", start=TUE_10 + timedelta(days=1))
    over = ev("practitioner", "JO-PRC-000001", "unavailable", start=TUE_10 - timedelta(days=2), end=TUE_10)
    cancelled = ev("practitioner", "JO-PRC-000001", "unavailable", cancelled_at=TUE_10 - timedelta(minutes=1))
    assert resolve_role(ROLE_CLINIC, CLINIC, [later, over, cancelled], TUE_10)["source"] == "schedule"


def test_facility_closure_overrides_the_doctor_and_its_24h_hours():
    closed = ev("facility", HOSPITAL["id"], "closed", reason="construction", note="East wing works")
    here = ev("role", ROLE_HOSPITAL["id"], "available", created=5)
    place = resolve_facility(HOSPITAL, [closed], TUE_10)
    assert (place["state"], place["reason"], place["note"]) == ("closed", "construction", "East wing works")
    doc = resolve_role(ROLE_HOSPITAL, HOSPITAL, [closed, here], TUE_10)
    assert (doc["state"], doc["reason"]) == ("unavailable", "facility_closed")


def test_facility_after_hours_does_not_close_a_doctor_with_their_own_hours():
    evening_role = ROLE_CLINIC | {"hours": {"weekly": [{"day": "tu", "opens": "18:00", "closes": "22:00"}]}}
    assert resolve_facility(CLINIC, [], TUE_20)["state"] == "closed"
    assert resolve_role(evening_role, CLINIC, [], TUE_20)["state"] == "available"


def test_limited_service_carries_er_and_departments():
    limited = ev("facility", HOSPITAL["id"], "limited", details={"er": "diverting", "departments": ["Maternity"]})
    out = resolve_facility(HOSPITAL, [limited], TUE_10)
    assert (out["state"], out["er"], out["departments"]) == ("limited", "diverting", ["Maternity"])


def test_status_and_ended_roles():
    assert resolve_facility(HOSPITAL | {"status": "permanently_closed"}, [], TUE_10)["state"] == "closed"
    ended = resolve_role(ROLE_CLINIC | {"active": False}, CLINIC, [], TUE_10)
    assert (ended["state"], ended["reason"]) == ("unavailable", "ended")


def test_overnight_spans():
    hours = {"weekly": [{"day": "mo", "opens": "20:00", "closes": "02:00"}]}
    # Tuesday 01:00 Amman is inside Monday's span.
    assert within_hours(hours, datetime(2026, 10, 26, 22, 0, tzinfo=UTC)) is True
    assert within_hours(hours, datetime(2026, 10, 27, 0, 30, tzinfo=UTC)) is False
