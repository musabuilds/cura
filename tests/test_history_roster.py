"""History is never lost; rosters change without deleting anyone; doctors are onboarded by invitation."""

from datetime import UTC, datetime, timedelta

from cura import patterns

DOC, PEDS, HOSP, CLINIC = "JO-PRC-000001", "JO-PRC-000002", "JO-AM-HSP-00001", "JO-AM-CLN-00001"
ROLE_HOSP, ROLE_PEDS = "JO-ROL-0000001", "JO-ROL-0000003"


def test_every_edit_is_a_version_and_the_past_stays_readable(registry, actors):
    doctor = actors.owner("rami@example.com", "practitioner", DOC)
    before = datetime.now(UTC)
    for hours in ("Su-Th 09:00-17:00", "Su-Th 08:00-14:00"):
        r = registry.patch(
            f"/v1/roles/{ROLE_HOSP}",
            json={"patch": {"hours": {"opening_hours": hours}}, "reason": "summer hours"},
            headers=doctor,
        )
        assert r.status_code == 200, r.text
    hist = registry.get(f"/v1/roles/{ROLE_HOSP}/history").json()
    assert [v["version"] for v in hist["data"]] == [3, 2, 1]
    assert hist["data"][0]["changed"] == ["hours.opening_hours", "hours.weekly"]  # weekly derived from the text
    assert hist["data"][1]["changed"] == ["hours.always_open", "hours.opening_hours", "hours.weekly"]
    assert hist["data"][0]["change"]["reason"] == "summer hours"
    assert hist["data"][2]["origin"] == "seed" and hist["data"][2]["valid_to"] is not None

    old = registry.get(f"/v1/roles/{ROLE_HOSP}/history", params={"as_of": before.isoformat()}).json()["data"][0]
    assert old["version"] == 1 and old["doc"]["hours"]["weekly"][0]["opens"] == "08:00"
    now = registry.get(f"/v1/roles/{ROLE_HOSP}").json()["hours"]
    assert now["opening_hours"] == "Su-Th 08:00-14:00" and now["weekly"][0]["closes"] == "14:00"


def test_schedule_patterns_by_season():
    t = lambda y, m, d: datetime(y, m, d, tzinfo=UTC)  # noqa: E731
    week = lambda o, c: {"hours": {"weekly": [{"day": d, "opens": o, "closes": c} for d in ("su", "mo", "tu")]}}  # noqa: E731
    out = patterns.schedules(
        [
            (t(2025, 1, 1), t(2025, 6, 1), week("09:00", "17:00")),
            (t(2025, 6, 1), t(2025, 9, 1), week("08:00", "13:00")),  # summer: early finish
            (t(2025, 9, 1), None, week("09:00", "17:00")),
        ],
        now=t(2026, 1, 1),
    )
    assert out["changes"] == 2
    assert out["by_season"]["summer"]["typical_end"] == "13:00"
    assert out["by_season"]["winter"]["typical_end"] == "17:00"
    assert out["periods"][1]["hours_per_week"] == 15.0


def test_absence_patterns_from_the_api(registry, actors):
    doctor = actors.owner("rami@example.com", "practitioner", DOC)
    start = datetime.now(UTC) - timedelta(days=3)
    registry.post(
        "/v1/availability",
        json={
            "subject_type": "practitioner",
            "subject_id": DOC,
            "state": "unavailable",
            "reason": "leave",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(days=2)).isoformat(),
        },
        headers=doctor,
    )
    out = registry.get(f"/v1/practitioners/{DOC}/patterns", params={"days": 30}).json()
    assert out["absence"]["days"] in (2, 3)  # two 24 h spans touch two or three calendar days
    assert out["absence"]["events_by_reason"] == {"leave": 1}
    assert {r["role_id"] for r in out["roles"]} == {ROLE_HOSP, "JO-ROL-0000002"}


def test_facility_admin_ends_a_role_and_it_is_kept(registry, actors):
    admin = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    r = registry.post(f"/v1/roles/{ROLE_PEDS}/end", json={"end_date": "2026-10-20", "reason": "left"}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["record"]["active"] is False and r.json()["record"]["period"]["end"] == "2026-10-20"
    listed = registry.get(f"/v1/facilities/{HOSP}/practitioners").json()["data"]
    assert PEDS not in {x["practitioner"]["id"] for x in listed}
    everyone = registry.get(f"/v1/facilities/{HOSP}/practitioners", params={"include": "all"}).json()["data"]
    assert PEDS in {x["practitioner"]["id"] for x in everyone}
    # The doctor's own page keeps where they worked.
    assert registry.get(f"/v1/practitioners/{PEDS}").json()["roles"][0]["active"] is False
    assert registry.post(f"/v1/roles/{ROLE_PEDS}/end", json={}, headers=admin).status_code == 409

    # Coming back reactivates the same role rather than creating a second one.
    back = registry.post(f"/v1/facilities/{HOSP}/practitioners", json={"practitioner_id": PEDS}, headers=admin)
    assert back.status_code == 200 and back.json()["role"]["id"] == ROLE_PEDS and back.json()["role"]["active"]


def test_facility_adds_a_new_doctor_after_a_duplicate_check(registry, actors):
    admin = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    dup = registry.post(
        f"/v1/facilities/{HOSP}/practitioners",
        json={"practitioner": {"name": {"en": "Rami Khalil Haddad"}, "specialty": "cardiology"}},
        headers=admin,
    )
    assert dup.status_code == 409 and dup.json()["candidates"][0]["id"] == DOC
    new = registry.post(
        f"/v1/facilities/{HOSP}/practitioners",
        json={
            "practitioner": {
                "name": {"en": "Dr. Sara Nabil Masri", "ar": "سارة نبيل المصري"},
                "specialty": "neurology",
            },
            "role": {"hours": {"opening_hours": "Mo 10:00-14:00"}},
        },
        headers=admin,
    )
    assert new.status_code == 201, new.text
    pid = new.json()["practitioner_created"]["id"]
    assert pid == "JO-PRC-900000" and new.json()["role"]["confirmation"] == "confirmed"
    stranger = actors.signup("x@example.com")
    body = {"practitioner_id": PEDS}
    assert registry.post(f"/v1/facilities/{HOSP}/practitioners", json=body, headers=stranger).status_code == 403


def test_two_sided_confirmation(registry, actors):
    admin = actors.owner("admin@irbid.jo", "facility", "JO-IR-HSP-00001")
    doctor = actors.owner("rami@example.com", "practitioner", DOC)
    r = registry.post(
        "/v1/roles", json={"record": {"practitioner_id": DOC, "facility_id": "JO-IR-HSP-00001"}}, headers=doctor
    )
    assert r.status_code == 201, r.text
    role = r.json()["record"]
    assert role["confirmation"] == "pending_facility"
    roster = registry.get("/v1/facilities/JO-IR-HSP-00001/practitioners").json()["data"]
    assert roster == []  # not listed until the hospital agrees
    assert (
        registry.post(f"/v1/roles/{role['id']}/confirmation", json={"accept": True}, headers=doctor).status_code == 403
    )
    ok = registry.post(f"/v1/roles/{role['id']}/confirmation", json={"accept": True}, headers=admin)
    assert ok.json()["record"]["confirmation"] == "confirmed"
    assert len(registry.get("/v1/facilities/JO-IR-HSP-00001/practitioners").json()["data"]) == 1


def test_invitation_onboarding(registry, actors):
    admin = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    inv = registry.post(
        f"/v1/facilities/{HOSP}/invitations", json={"practitioner_id": PEDS, "email": "lina@example.com"}, headers=admin
    )
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]
    assert inv.json()["url"].endswith(token)
    lina = actors.signup("lina@example.com")
    accepted = registry.post("/v1/invitations/accept", json={"token": token}, headers=lina)
    assert accepted.status_code == 200, accepted.text
    me = registry.get("/v1/me", headers=lina).json()
    assert me["owns"]["practitioner"] == [PEDS]
    claims = registry.get("/v1/claims", headers=lina).json()["data"]
    assert claims[0]["method"] == "facility_invite"
    # One use only; and the profile is now owned.
    assert registry.post("/v1/invitations/accept", json={"token": token}, headers=lina).status_code == 404
    again = registry.post(f"/v1/facilities/{HOSP}/invitations", json={"practitioner_id": PEDS}, headers=admin)
    assert again.status_code == 409
    listed = registry.get(f"/v1/facilities/{HOSP}/invitations", headers=admin).json()["data"]
    assert [i["status"] for i in listed] == ["accepted"]
    # The doctor now edits their own role directly.
    r = registry.patch(f"/v1/roles/{ROLE_PEDS}", json={"patch": {"phones": ["+96265000009"]}}, headers=lina)
    assert r.status_code == 200
    # Only doctors on the roster can be invited: once Rami has left, the hospital cannot vouch for him.
    registry.post(f"/v1/roles/{ROLE_HOSP}/end", json={}, headers=admin)
    gone = registry.post(f"/v1/facilities/{HOSP}/invitations", json={"practitioner_id": DOC}, headers=admin)
    assert gone.status_code == 422
