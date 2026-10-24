"""End-to-end API flows against a throwaway SQLite database."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from cura.api import db, store
from cura.api.settings import settings

NOW = "2026-10-24T00:00:00Z"
FACILITY = {
    "id": "JO-AM-HSP-00001",
    "type": "hospital",
    "name": {"en": "Alpha Hospital", "ar": "مستشفى ألفا", "aliases": []},
    "address": {"governorate": "JO-AM"},
    "location": {"lat": 31.9539, "lon": 35.9106, "precision": "exact"},
    "hours": {"opening_hours": "24/7", "always_open": True},
    "specialties": ["cardiology"],
    "insurance": [{"insurer": "mednet"}],
    "quality": {"completeness": 0.5, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
CLINIC = {
    "id": "JO-IR-CLN-00001",
    "type": "clinic",
    "name": {"en": "Irbid Clinic"},
    "address": {"governorate": "JO-IR"},
    "location": {"lat": 32.5556, "lon": 35.85},
    "quality": {"completeness": 0.2, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
DOCTOR = {
    "id": "JO-PRC-000001",
    "name": {"en": "Dr. Rami Haddad"},
    "specialty": "cardiology",
    "quality": {"completeness": 0.3, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
ROLE = {"id": "JO-ROL-0000001", "practitioner_id": "JO-PRC-000001", "facility_id": "JO-AM-HSP-00001", "updated_at": NOW}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CURA_DATABASE_URL", f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    settings.cache_clear()
    db.reset_engine()
    db.init()
    with next(db.get_session()) as s:
        for kind, doc in (("facility", FACILITY), ("facility", CLINIC), ("practitioner", DOCTOR), ("role", ROLE)):
            store.put(s, kind, doc, dirty=False)
        s.commit()
    from cura.api.main import app

    with TestClient(app) as c:
        yield c
    db.reset_engine()
    settings.cache_clear()


def signup(client, email, password="correct horse battery"):
    assert client.post("/v1/auth/register", json={"email": email, "password": password}).status_code == 201
    tok = client.post("/v1/auth/login", json={"email": email, "password": password}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}, tok


def moderator(client):
    from cura.api import admin

    admin.create_admin("mod@cura.jo", "moderator password!")
    tok = client.post("/v1/auth/login", json={"email": "mod@cura.jo", "password": "moderator password!"}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


def test_public_reads_search_and_geo(client):
    r = client.get("/v1/facilities", params={"q": "alpha"})
    assert r.status_code == 200 and [f["id"] for f in r.json()["data"]] == ["JO-AM-HSP-00001"]
    assert client.get("/v1/facilities", params={"q": "ألفا"}).json()["count"] == 1  # Arabic search
    assert client.get("/v1/facilities", params={"governorate": "IR"}).json()["data"][0]["id"] == "JO-IR-CLN-00001"
    assert client.get("/v1/facilities", params={"specialty": "cardiology", "insurer": "mednet"}).json()["count"] == 1
    near = client.get("/v1/facilities", params={"near": "31.95,35.91", "radius_km": 3}).json()["data"]
    assert [f["id"] for f in near] == ["JO-AM-HSP-00001"] and near[0]["distance_m"] < 1000
    assert client.get("/v1/facilities", params={"open_now": True}).json()["count"] == 1  # only the 24/7 one
    one = client.get("/v1/facilities/JO-AM-HSP-00001")
    assert one.headers["ETag"] == '"1"' and one.json()["open_now"] is True
    doc = client.get("/v1/practitioners/JO-PRC-000001").json()
    assert doc["roles"][0]["facility"]["id"] == "JO-AM-HSP-00001"
    assert client.get("/v1/facilities/JO-XX-HSP-99999").headers["content-type"] == "application/problem+json"
    assert "X-RateLimit-Remaining" in one.headers


def test_writes_require_a_user(client):
    r = client.patch("/v1/facilities/JO-AM-HSP-00001", json={"patch": {"beds": 10}})
    assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith("Bearer")


def test_stranger_edit_becomes_proposal_and_moderator_applies_it(client):
    user, _ = signup(client, "a@example.com")
    r = client.patch(
        "/v1/facilities/JO-AM-HSP-00001", json={"patch": {"beds": 120}, "reason": "hospital website"}, headers=user
    )
    assert r.status_code == 202 and r.json()["status"] == "proposed" and r.json()["proposal_id"]
    assert client.get("/v1/facilities/JO-AM-HSP-00001").json()["beds"] is None
    mod = moderator(client)
    queue = client.get("/v1/proposals", headers=mod).json()["data"]
    assert len(queue) == 1
    applied = client.post(f"/v1/proposals/{queue[0]['id']}/approve", json={"note": "checked"}, headers=mod)
    assert applied.status_code == 200
    after = client.get("/v1/facilities/JO-AM-HSP-00001")
    assert after.json()["beds"] == 120 and after.headers["ETag"] == '"2"'
    feed = client.get("/v1/changes", params={"since": "2000-01-01T00:00:00Z"}).json()
    assert any(c["id"] == "JO-AM-HSP-00001" and c["version"] == 2 for c in feed["data"])


def test_system_fields_are_read_only(client):
    user, _ = signup(client, "b@example.com")
    r = client.patch("/v1/facilities/JO-AM-HSP-00001", json={"patch": {"id": "JO-AM-HSP-00009"}}, headers=user)
    assert r.status_code == 422
    r = client.patch(
        "/v1/facilities/JO-AM-HSP-00001", json={"patch": {"location": {"lat": 50, "lon": 10}}}, headers=user
    )
    assert r.status_code == 422  # outside Jordan: invalid even as a proposal


def test_verified_doctor_edits_own_schedule_directly(client):
    doc_user, _ = signup(client, "rami@example.com")
    claim = client.post(
        "/v1/claims",
        json={
            "subject_type": "practitioner",
            "subject_id": "JO-PRC-000001",
            "evidence": {"license_number": "JMA-12345"},
        },
        headers=doc_user,
    )
    assert claim.status_code == 201 and claim.json()["status"] == "pending"
    # Before approval: still only a proposal.
    r = client.patch(
        "/v1/roles/JO-ROL-0000001",
        json={"patch": {"consultation_fee": {"amount": 25, "currency": "JOD"}}},
        headers=doc_user,
    )
    assert r.status_code == 202
    mod = moderator(client)
    assert client.post(f"/v1/claims/{claim.json()['id']}/approve", json={}, headers=mod).json()["status"] == "approved"
    assert client.get("/v1/practitioners/JO-PRC-000001").json()["license_verified"] is True
    r = client.patch(
        "/v1/roles/JO-ROL-0000001",
        headers={**doc_user, "If-Match": '"1"'},
        json={
            "patch": {
                "hours": {"opening_hours": "Su-Th 10:00-14:00"},
                "consultation_fee": {"amount": 25, "currency": "JOD"},
            }
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "applied"
    stale = client.patch(
        "/v1/roles/JO-ROL-0000001", headers={**doc_user, "If-Match": '"1"'}, json={"patch": {"active": False}}
    )
    assert stale.status_code == 412
    # Identity fields still need review, even for the owner.
    assert (
        client.patch(
            "/v1/practitioners/JO-PRC-000001", json={"patch": {"specialty": "neurology"}}, headers=doc_user
        ).status_code
        == 202
    )


def test_reviews_aggregate_and_hide(client):
    u1, _ = signup(client, "r1@example.com")
    u2, _ = signup(client, "r2@example.com")
    client.post(
        "/v1/facilities/JO-AM-HSP-00001/reviews", json={"ratings": {"overall": 5, "cleanliness": 4}}, headers=u1
    )
    r = client.post(
        "/v1/facilities/JO-AM-HSP-00001/reviews", json={"ratings": {"overall": 2, "cleanliness": 2}}, headers=u2
    )
    assert r.json()["rating"] == {"overall": 3.5, "count": 2, "dimensions": {"cleanliness": 3.0}}
    # Re-posting replaces, it does not stack.
    r = client.post("/v1/facilities/JO-AM-HSP-00001/reviews", json={"ratings": {"overall": 4}}, headers=u2)
    assert r.json()["rating"]["count"] == 2
    bad = client.post("/v1/facilities/JO-AM-HSP-00001/reviews", json={"ratings": {"vibes": 5}}, headers=u1)
    assert bad.status_code == 422
    listed = client.get("/v1/facilities/JO-AM-HSP-00001/reviews").json()["data"]
    assert "user_id" not in listed[0]


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def test_oauth_pkce_flow_refresh_rotation_and_reuse_detection(client):
    dev, _ = signup(client, "dev@example.com")
    app = client.post(
        "/v1/apps", json={"name": "Clinic Finder", "redirect_uris": ["myapp://callback"]}, headers=dev
    ).json()
    assert app["api_key"].startswith("cura_") and app["client_secret"] is None
    signup(client, "patient@example.com", "patient password 1")
    verifier, challenge = _pkce()
    q = {
        "client_id": app["client_id"],
        "redirect_uri": "myapp://callback",
        "response_type": "code",
        "scope": "profile reviews:write",
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    page = client.get("/oauth/authorize", params=q)
    assert page.status_code == 200 and "Clinic Finder" in page.text and page.headers["X-Frame-Options"] == "DENY"
    redirect = client.post(
        "/oauth/authorize",
        data=q | {"email": "patient@example.com", "password": "patient password 1", "decision": "allow"},
        follow_redirects=False,
    )
    assert redirect.status_code == 303
    params = parse_qs(urlsplit(redirect.headers["location"]).query)
    assert params["state"] == ["xyz"]
    code = params["code"][0]
    # Wrong verifier fails — and burns the code.
    bad = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": app["client_id"],
            "code": code,
            "redirect_uri": "myapp://callback",
            "code_verifier": "x" * 43,
        },
    )
    assert bad.status_code == 400
    verifier, challenge = _pkce()
    q["code_challenge"] = challenge
    redirect = client.post(
        "/oauth/authorize",
        data=q | {"email": "patient@example.com", "password": "patient password 1", "decision": "allow"},
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(redirect.headers["location"]).query)["code"][0]
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": app["client_id"],
            "code": code,
            "redirect_uri": "myapp://callback",
            "code_verifier": verifier,
        },
    ).json()
    assert tok["scope"] == "profile reviews:write"
    headers = {"Authorization": f"Bearer {tok['access_token']}", "X-API-Key": app["api_key"]}
    me = client.get("/v1/me", headers=headers).json()
    assert me["email"] == "patient@example.com" and me["client_id"] == app["client_id"]
    # Token scoped to reviews cannot propose edits.
    assert (
        client.post(
            "/v1/facilities", json={"record": {"type": "clinic", "name": {"en": "New"}}}, headers=headers
        ).status_code
        == 403
    )
    rotated = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": app["client_id"], "refresh_token": tok["refresh_token"]},
    ).json()
    assert rotated["refresh_token"] != tok["refresh_token"]
    replay = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": app["client_id"], "refresh_token": tok["refresh_token"]},
    )
    assert replay.status_code == 400
    # Family revoked: the rotated token is dead too.
    dead = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": app["client_id"], "refresh_token": rotated["refresh_token"]},
    )
    assert dead.status_code == 400


def test_new_facility_proposal_gets_reserved_id(client):
    user, _ = signup(client, "c@example.com")
    r = client.post(
        "/v1/facilities",
        json={"record": {"type": "clinic", "name": {"ar": "عيادة جديدة"}, "address": {"governorate": "JO-AM"}}},
        headers=user,
    )
    assert r.status_code == 202
    mod = moderator(client)
    change_id = r.json()["change_id"]
    rec = client.post(f"/v1/proposals/{change_id}/approve", json={}, headers=mod).json()["record"]
    assert rec["id"] == "JO-AM-CLN-90000"
