"""Availability end to end: a doctor and a hospital setting what is happening, everyone reading it."""

from datetime import UTC, datetime, timedelta

DOC, HOSP, CLINIC = "JO-PRC-000001", "JO-AM-HSP-00001", "JO-AM-CLN-00001"
ROLE_HOSP, ROLE_CLINIC = "JO-ROL-0000001", "JO-ROL-0000002"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def places(client, at=None):
    r = client.get(f"/v1/practitioners/{DOC}/availability", params={"at": at} if at else None)
    assert r.status_code == 200, r.text
    return {p["facility"]["id"]: p["availability"] for p in r.json()["places"]}


def test_doctor_off_everywhere_then_back_at_own_clinic(registry, actors):
    doctor = actors.owner("rami@example.com", "practitioner", DOC)
    now = datetime.now(UTC)
    week = registry.post(
        "/v1/availability",
        json={
            "subject_type": "practitioner",
            "subject_id": DOC,
            "state": "unavailable",
            "reason": "leave",
            "note": "Back on Sunday",
            "ends_at": iso(now + timedelta(days=5)),
        },
        headers=doctor,
    )
    assert week.status_code == 201, week.text
    state = places(registry)
    assert {s["state"] for s in state.values()} == {"unavailable"}
    assert state[HOSP]["note"] == "Back on Sunday" and state[HOSP]["set_on"] == "practitioner"

    clinic = registry.post(
        "/v1/availability",
        json={"subject_type": "role", "subject_id": ROLE_CLINIC, "state": "available"},
        headers=doctor,
    )
    assert clinic.status_code == 201
    state = places(registry)
    assert state[CLINIC]["state"] == "available" and state[HOSP]["state"] == "unavailable"

    # Every read reflects it: the doctor's own page and the hospital's roster.
    doc = registry.get(f"/v1/practitioners/{DOC}").json()
    assert {r["facility_id"]: r["availability"]["state"] for r in doc["roles"]} == {
        HOSP: "unavailable",
        CLINIC: "available",
    }
    roster = registry.get(f"/v1/facilities/{HOSP}/live").json()
    assert {p["practitioner_id"]: p["availability"]["state"] for p in roster["practitioners"]}[DOC] == "unavailable"

    # "I'm back": ending the leave early restores the schedule at the hospital.
    ended = registry.post(f"/v1/availability/{week.json()['id']}/end", json={}, headers=doctor)
    assert ended.status_code == 200 and ended.json()["ends_at"] is not None
    assert places(registry)[HOSP]["source"] == "schedule"


def test_hospital_closes_and_its_doctors_follow(registry, actors):
    admin = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    r = registry.post(
        "/v1/availability",
        json={
            "subject_type": "facility",
            "subject_id": HOSP,
            "state": "closed",
            "reason": "construction",
            "note": "Renovation; ER at Irbid",
            "details": {"er": "closed"},
        },
        headers=admin,
    )
    assert r.status_code == 201, r.text
    fac = registry.get(f"/v1/facilities/{HOSP}").json()
    assert fac["live"]["state"] == "closed" and fac["live"]["er"] == "closed" and fac["open_now"] is False
    # 24/7 by its hours, closed in fact: open_now search must not list it.
    ids = [
        f["id"] for f in registry.get("/v1/facilities", params={"open_now": "true", "type": "hospital"}).json()["data"]
    ]
    assert HOSP not in ids
    assert places(registry)[HOSP]["reason"] == "facility_closed"

    # Withdrawn: back to its schedule.
    registry.post(f"/v1/availability/{r.json()['id']}/cancel", headers=admin)
    assert registry.get(f"/v1/facilities/{HOSP}").json()["live"]["source"] == "schedule"


def test_facility_admin_can_mark_its_doctor_but_not_others(registry, actors):
    admin = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    ok = registry.post(
        "/v1/availability",
        json={"subject_type": "role", "subject_id": ROLE_HOSP, "state": "busy", "reason": "procedure"},
        headers=admin,
    )
    assert ok.status_code == 201
    for body in (
        {"subject_type": "role", "subject_id": ROLE_CLINIC, "state": "unavailable"},
        {"subject_type": "practitioner", "subject_id": DOC, "state": "unavailable"},
        {"subject_type": "facility", "subject_id": CLINIC, "state": "closed"},
    ):
        assert registry.post("/v1/availability", json=body, headers=admin).status_code == 403
    stranger = actors.signup("stranger@example.com")
    body = {"subject_type": "facility", "subject_id": HOSP, "state": "closed"}
    assert registry.post("/v1/availability", json=body, headers=stranger).status_code == 403


def test_idempotent_retries_and_validation(registry, actors):
    doctor = actors.owner("rami@example.com", "practitioner", DOC)
    body = {"subject_type": "practitioner", "subject_id": DOC, "state": "unavailable", "reason": "conference"}
    first = registry.post("/v1/availability", json=body, headers=doctor | {"Idempotency-Key": "abc-1"})
    again = registry.post("/v1/availability", json=body, headers=doctor | {"Idempotency-Key": "abc-1"})
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]

    bad = [
        body | {"state": "closed"},  # a facility state on a doctor
        body | {"reason": "sick"},  # medical reasons are not a vocabulary
        body | {"details": {"er": "closed"}},
        body | {"starts_at": iso(datetime.now(UTC) - timedelta(days=30))},
        body | {"starts_at": iso(datetime.now(UTC)), "ends_at": iso(datetime.now(UTC) - timedelta(hours=1))},
    ]
    for b in bad:
        assert registry.post("/v1/availability", json=b, headers=doctor).status_code == 422, b


def test_future_event_is_scheduled_and_feed_and_timeline(registry, actors):
    doctor = actors.owner("rami@example.com", "practitioner", DOC)
    start = datetime.now(UTC) + timedelta(days=10)
    r = registry.post(
        "/v1/availability",
        json={
            "subject_type": "practitioner",
            "subject_id": DOC,
            "state": "unavailable",
            "reason": "conference",
            "starts_at": iso(start),
            "ends_at": iso(start + timedelta(days=3)),
        },
        headers=doctor,
    )
    assert r.status_code == 201
    assert places(registry)[CLINIC]["source"] == "schedule"  # not yet
    during = places(registry, at=iso(start + timedelta(days=1)))
    assert during[CLINIC]["reason"] == "conference"

    timeline = registry.get("/v1/availability", params={"facility_id": HOSP, "to": iso(start + timedelta(days=30))})
    assert [e["id"] for e in timeline.json()["data"]] == []  # a doctor-wide event has no facility
    mine = registry.get("/v1/availability", params={"practitioner_id": DOC, "to": iso(start + timedelta(days=30))})
    assert [e["id"] for e in mine.json()["data"]] == [r.json()["id"]]

    feed = registry.get("/v1/availability/changes", params={"since": "2020-01-01T00:00:00Z"}).json()
    assert feed["count"] == 1 and feed["data"][0]["state"] == "unavailable"
    # Ending an event that has not started cancels it; it stays in the history.
    registry.post(f"/v1/availability/{r.json()['id']}/end", json={}, headers=doctor)
    kept = registry.get(
        "/v1/availability",
        params={"practitioner_id": DOC, "to": iso(start + timedelta(days=30)), "include_cancelled": "true"},
    ).json()["data"]
    assert kept[0]["cancelled_at"] is not None


def test_load_samples_and_vocabulary(registry, actors):
    admin = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    assert registry.post(f"/v1/facilities/{HOSP}/load", json={"level": 0.8}, headers=admin).status_code == 201
    assert registry.post(f"/v1/facilities/{HOSP}/load", json={"level": 1.5}, headers=admin).status_code == 422
    vocab = registry.get("/v1/reference/availability").json()
    assert "construction" in vocab["reasons"]["facility"] and "busy" in vocab["states"]["role"]
