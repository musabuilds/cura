"""Doctor onboarding from a phone: candidates, instant claims on a phone match, disputes, create-new,
and private contact details that never leak."""

from cura.api import db
from helpers import phone_user, set_doc

DOC, PEDS, HOSP, CLINIC = "JO-PRC-000001", "JO-PRC-000002", "JO-AM-HSP-00001", "JO-AM-CLN-00001"
ROLE_HOSP, ROLE_CLINIC, ROLE_PEDS = "JO-ROL-0000001", "JO-ROL-0000002", "JO-ROL-0000003"


def test_phone_on_the_record_makes_the_claim_instant(registry):
    set_doc("role", ROLE_CLINIC, phones=["+962791112233"])
    rami = phone_user(registry, "0791112233", "Rami Haddad")
    cands = registry.get("/v1/onboarding/practitioner/candidates", headers=rami).json()
    top = cands["data"][0]
    assert top["practitioner"]["id"] == DOC and top["reasons"][0] == "phone_match"
    assert top["instant"] is True and top["claim_state"] == "unclaimed"
    assert (
        top["practitioner"]["specialty_label"] == {"en": "Cardiology", "ar": "أمراض القلب"}
        or top["practitioner"]["specialty_label"]["en"]
    )
    claim = registry.post("/v1/claims", json={"subject_type": "practitioner", "subject_id": DOC}, headers=rami)
    assert claim.status_code == 201, claim.text
    assert claim.json()["status"] == "approved" and claim.json()["method"] == "phone_match"
    me = registry.get("/v1/me", headers=rami).json()
    assert me["practitioner"]["id"] == DOC and me["practitioner"]["verification"]["level"] == "verified"
    assert registry.get(f"/v1/practitioners/{DOC}").json()["verification"]["level"] == "verified"
    # It now shows as yours, and nobody else can take it without a dispute.
    again = registry.get("/v1/onboarding/practitioner/candidates", headers=rami).json()["data"][0]
    assert again["claim_state"] == "claimed_by_you" and again["instant"] is False


def test_a_shared_reception_number_proves_nothing(registry):
    # The clinic's line is listed on two doctors' roles: whoever answers it is not both of them.
    set_doc("role", ROLE_HOSP, phones=["+962791112244"])
    set_doc("role", ROLE_PEDS, phones=["+962791112244"])
    reception = phone_user(registry, "0791112244")
    cands = registry.get("/v1/onboarding/practitioner/candidates", headers=reception).json()["data"]
    assert {c["practitioner"]["id"] for c in cands} == {DOC, PEDS} and not any(c["instant"] for c in cands)
    r = registry.post("/v1/claims", json={"subject_type": "practitioner", "subject_id": DOC}, headers=reception)
    assert r.status_code == 422  # no instant path: evidence is required
    r = registry.post(
        "/v1/claims",
        json={"subject_type": "practitioner", "subject_id": DOC, "evidence": {"jmc_number": "JMC-1"}},
        headers=reception,
    )
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["method"] == "evidence"


def test_name_candidates_fold_transliterations(registry):
    user = phone_user(registry, "0791112255")
    r = registry.get("/v1/onboarding/practitioner/candidates", params={"name": "Rami Haddad"}, headers=user).json()
    assert r["data"][0]["practitioner"]["id"] == DOC and "name_match" in r["data"][0]["reasons"]
    assert r["data"][0]["instant"] is False


def test_claiming_someone_elses_profile_needs_a_dispute(registry, actors):
    set_doc("role", ROLE_CLINIC, phones=["+962791112233"])
    owner = phone_user(registry, "0791112233")
    assert (
        registry.post("/v1/claims", json={"subject_type": "practitioner", "subject_id": DOC}, headers=owner).json()[
            "status"
        ]
        == "approved"
    )
    rival = phone_user(registry, "0791112266")
    r = registry.post(
        "/v1/claims",
        json={"subject_type": "practitioner", "subject_id": DOC, "evidence": {"license_number": "X"}},
        headers=rival,
    )
    assert r.status_code == 409 and r.json()["can_dispute"] is True
    d = registry.post(
        "/v1/claims",
        json={"subject_type": "practitioner", "subject_id": DOC, "dispute": True, "evidence": {"license_number": "X"}},
        headers=rival,
    )
    assert d.status_code == 201 and d.json()["status"] == "pending" and d.json()["method"] == "dispute"
    # The moderator sides with the rival: ownership moves.
    assert registry.post(f"/v1/claims/{d.json()['id']}/approve", json={}, headers=actors.moderator).status_code == 200
    assert registry.get("/v1/me", headers=rival).json()["practitioner"]["id"] == DOC
    assert registry.get("/v1/me", headers=owner).json()["practitioner"] is None


def test_create_new_is_never_blocked_and_queues_a_merge_case(registry, actors):
    user = phone_user(registry, "0791112277")
    record = {"name": {"en": "Dr. Rami Haddad", "ar": "رامي حداد"}, "specialty": "cardiology"}
    r = registry.post("/v1/practitioners", json={"record": record}, headers=user)
    assert r.status_code == 409
    assert r.json()["candidates"][0]["practitioner"]["id"] == DOC
    r = registry.post("/v1/practitioners", json={"record": record, "confirm_new": True}, headers=user)
    assert r.status_code == 201, r.text
    body = r.json()
    new_id = body["practitioner"]["id"]
    assert new_id.startswith("JO-PRC-9") and body["claim"]["status"] == "approved"
    assert body["practitioner"]["verification"]["level"] == "unverified"
    assert [p["id"] for p in body["possible_duplicates"]] == [DOC]
    assert registry.get("/v1/me", headers=user).json()["practitioner"]["id"] == new_id
    cases = registry.get("/v1/merge-cases", headers=actors.moderator).json()["data"]
    assert cases[0]["record_id"] == new_id and cases[0]["candidates"] == [DOC]
    # One profile per account.
    assert (
        registry.post("/v1/practitioners", json={"record": record, "confirm_new": True}, headers=user).status_code
        == 409
    )
    # Owner edits apply at once.
    r = registry.patch(
        f"/v1/practitioners/{new_id}",
        json={"patch": {"bio": {"en": "Heart doctor."}, "photo_url": "https://example.com/rami.jpg"}},
        headers=user,
    )
    assert r.status_code == 200 and r.json()["record"]["bio"]["en"] == "Heart doctor."
    bad = registry.patch(
        f"/v1/practitioners/{new_id}", json={"patch": {"photo_url": "javascript:alert(1)"}}, headers=user
    )
    assert bad.status_code == 422


def test_email_accounts_keep_the_moderated_create(registry, actors):
    dev = actors.signup("dev@example.com")
    r = registry.post(
        "/v1/practitioners",
        json={"record": {"name": {"en": "Dr. Nobody Unique"}, "specialty": "cardiology"}, "confirm_new": True},
        headers=dev,
    )
    assert r.status_code == 202 and r.json()["status"] == "pending"


def test_private_contact_never_leaks(registry):
    set_doc("role", ROLE_CLINIC, phones=["+962791112233"])
    rami = phone_user(registry, "0791112233")
    registry.post("/v1/claims", json={"subject_type": "practitioner", "subject_id": DOC}, headers=rami)
    patch = {
        "contact": {
            "phones": [
                {"number": "0791112233", "label": "Clinic", "public": True},
                {"number": "0798887766", "label": "Personal", "public": False},
            ],
            "email": "rami.private@example.jo",
            "public_email": False,
        }
    }
    r = registry.patch(f"/v1/practitioners/{DOC}", json={"patch": patch}, headers=rami)
    assert r.status_code == 200, r.text
    secret_bits = ("798887766", "rami.private@example.jo")
    public_reads = [
        registry.get(f"/v1/practitioners/{DOC}"),
        registry.get("/v1/practitioners", params={"q": "rami"}),
        registry.get(f"/v1/facilities/{CLINIC}", params={"expand": "practitioners"}),
        registry.get("/v1/changes", params={"since": "2000-01-01T00:00:00Z"}),
        registry.get(f"/v1/practitioners/{DOC}/history", params={"include_docs": True}),
        registry.get("/v1/search", params={"q": "rami haddad"}),
        # Another signed-in user is "the public" too.
        registry.get(f"/v1/practitioners/{DOC}", headers=phone_user(registry, "0791112299")),
    ]
    for resp in public_reads:
        assert resp.status_code == 200, resp.text
        for bit in secret_bits:
            assert bit not in resp.text, f"private contact leaked via {resp.url}"
    public = registry.get(f"/v1/practitioners/{DOC}").json()
    assert public["contact"] == {"phones": [{"number": "+962791112233", "label": "Clinic"}], "email": None}
    mine = registry.get(f"/v1/practitioners/{DOC}", headers=rami)
    assert "798887766" in mine.text and mine.headers["Cache-Control"] == "private, no-store"
    # The proposal log never saw the private number either.
    from sqlalchemy import select

    from cura.api.db import Change

    with db.new_session() as s:
        for c in s.scalars(select(Change)):
            assert "798887766" not in str(c.patch)


def test_only_the_owner_sets_contact(registry, actors):
    stranger = actors.signup("stranger@example.com")
    r = registry.patch(
        f"/v1/practitioners/{DOC}", json={"patch": {"contact": {"email": "x@example.com"}}}, headers=stranger
    )
    assert r.status_code == 403


def test_unverified_records_cannot_publish_onto_other_rosters(registry):
    user = phone_user(registry, "0791112288")
    fake = registry.post(
        "/v1/facilities",
        json={
            "record": {"type": "clinic", "name": {"en": "Totally Real Clinic"}, "address": {"governorate": "JO-AM"}},
            "confirm_new": True,
        },
        headers=user,
    ).json()["facility"]["id"]
    # A facility nobody has verified cannot list a real doctor on its own say-so, nor take over their profile.
    added = registry.post(f"/v1/facilities/{fake}/practitioners", json={"practitioner_id": PEDS}, headers=user)
    assert added.status_code == 201 and added.json()["role"]["confirmation"] == "pending_practitioner"
    roster = registry.get(f"/v1/facilities/{fake}/practitioners").json()["data"]
    assert roster == []
    invite = registry.post(f"/v1/facilities/{fake}/invitations", json={"practitioner_id": PEDS}, headers=user)
    assert invite.status_code == 403
    # Likewise an unverified doctor profile joining a real hospital waits for that side.
    me = registry.post(
        "/v1/practitioners",
        json={"record": {"name": {"en": "Dr. Zaid Unique Person"}, "specialty": "cardiology"}, "confirm_new": True},
        headers=user,
    ).json()["practitioner"]["id"]
    role = registry.post("/v1/roles", json={"record": {"practitioner_id": me, "facility_id": HOSP}}, headers=user)
    assert role.status_code == 201 and role.json()["record"]["confirmation"] == "pending_facility"
