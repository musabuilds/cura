"""`cura demo seed` / `cura demo reset`: the pitch's cast, idempotent, and gone without a trace."""

from sqlalchemy import func, select

from cura.api import db, demo_data
from cura.api.db import Claim, FacilityRow, PractitionerRow, RoleRow, User

HOSP = "JO-AM-HSP-00001"  # the real registry hospital that invites Musab


def login(client, phone):
    from cura.api import guard

    guard.windows.reset()
    ch = client.post("/v1/auth/phone/start", json={"phone": phone}).json()
    r = client.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": "246810"})
    assert r.status_code == 200, r.text
    return r.json(), {"Authorization": f"Bearer {r.json()['access_token']}"}


def counts():
    with db.new_session() as s:
        return tuple(
            s.scalar(select(func.count()).select_from(t)) for t in (FacilityRow, PractitionerRow, RoleRow, User, Claim)
        )


def test_seed_is_idempotent_and_reset_is_clean(registry):
    before = counts()
    ids = demo_data.seed()
    seeded = counts()
    assert demo_data.seed() == ids and counts() == seeded
    assert ids["pending_invitation"]["facility_id"] == HOSP
    crest = registry.get(f"/v1/facilities/{demo_data.HOSPITAL_ID}", params={"expand": "practitioners"}).json()
    assert crest["demo"] is True and crest["verification"]["level"] == "verified" and crest["emergency"] is True
    assert len(crest["departments"]) == 8 and len(crest["practitioners"]) == 6
    assert all(p["practitioner"]["name"]["ar"] for p in crest["practitioners"])

    # Musab signs in with the demo code and owns his profile and his clinic.
    tokens, musab = login(registry, "0790000001")
    user = tokens["user"]
    assert (
        user["practitioner"]["id"] == demo_data.MUSAB_ID and user["practitioner"]["verification"]["level"] == "verified"
    )
    assert [f["facility"]["id"] for f in user["facilities"]] == [demo_data.CLINIC_ID]
    roles = registry.get("/v1/me/roles", headers=musab).json()["data"]
    states = {r["facility"]["id"]: r["role"]["confirmation"] for r in roles}
    assert states == {
        demo_data.CLINIC_ID: "confirmed",
        demo_data.HOSPITAL_ID: "confirmed",
        HOSP: "pending_practitioner",
    }
    # The confirm-workplace flow works on the real hospital's invitation.
    invite = next(r["role"]["id"] for r in roles if r["facility"]["id"] == HOSP)
    assert registry.post(f"/v1/roles/{invite}/confirmation", json={"accept": True}, headers=musab).status_code == 200
    # His private number stays private; his public one is on the record.
    public = registry.get(f"/v1/practitioners/{demo_data.MUSAB_ID}").text
    assert "+962790000001" in public and "790000009" not in public
    # The new user's duplicate check finds both spellings.
    _, new = login(registry, "0790000003")
    r = registry.post(
        "/v1/practitioners",
        json={"record": {"name": {"en": "Musab Al-Jabiri", "ar": "مصعب الجابري"}, "specialty": "sports_medicine"}},
        headers=new,
    )
    assert r.status_code == 409
    assert {demo_data.MUSAB_ID, demo_data.DUPLICATE_ID} <= {c["practitioner"]["id"] for c in r.json()["candidates"]}
    cands = registry.get(
        "/v1/onboarding/practitioner/candidates", params={"name": "Musab Al-Jabiri"}, headers=musab
    ).json()["data"]
    assert cands[0]["practitioner"]["id"] == demo_data.MUSAB_ID and cands[0]["reasons"][0] == "phone_match"
    assert demo_data.DUPLICATE_ID in {c["practitioner"]["id"] for c in cands}
    # The new user creates a profile anyway; reset removes it with everything else.
    made = registry.post(
        "/v1/practitioners",
        json={"record": {"name": {"en": "Musab Al-Jabiri"}, "specialty": "sports_medicine"}, "confirm_new": True},
        headers=new,
    )
    assert made.status_code == 201
    removed = demo_data.reset()
    assert removed["users"] == 3 and removed["facilities"] == 2
    assert counts() == before
    assert registry.get(f"/v1/facilities/{HOSP}").status_code == 200  # the real hospital is untouched
    assert demo_data.reset() == {}  # nothing left to remove
