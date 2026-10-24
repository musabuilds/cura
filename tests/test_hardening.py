"""What a judge with curl tries: headers, oversized bodies, unexpected fields, stack traces, and every
write against someone else's facility or profile (IDOR)."""

import re

import pytest

from cura.api import store
from cura.api.settings import Settings
from helpers import phone_user

HOSP, CLINIC, DOC, PEDS = "JO-AM-HSP-00001", "JO-AM-CLN-00001", "JO-PRC-000001", "JO-PRC-000002"
ROLE_HOSP, ROLE_CLINIC = "JO-ROL-0000001", "JO-ROL-0000002"


def test_security_headers(registry):
    r = registry.get(f"/v1/facilities/{HOSP}")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer" and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in h
    tls = registry.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert tls.headers["Strict-Transport-Security"].startswith("max-age=")


def test_request_size_limit(registry):
    big = {"phone": "0" * 2_000_000}
    assert registry.post("/v1/auth/phone/start", json=big).status_code == 413


def test_errors_never_carry_stack_traces(registry, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db password is hunter2")

    monkeypatch.setattr(store, "get", boom)
    r = registry.get(f"/v1/facilities/{HOSP}")
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/problem+json")
    assert "hunter2" not in r.text and "Traceback" not in r.text


def test_production_cors_is_not_a_wildcard():
    s = Settings(environment="production", jwt_secret="x" * 64, cors_origins=["*", "https://crest.jo"])
    assert s.effective_cors_origins == ["https://crest.jo"]
    assert Settings(environment="production", jwt_secret="x" * 64, demo_phones="0790000001").demo_numbers == set()


@pytest.fixture()
def two_owners(registry, actors):
    """A owns the hospital and Dr. Haddad; B owns the clinic. B attacks A's things."""
    a = actors.owner("a-hosp@example.com", "facility", HOSP)
    a_doc = actors.owner("a-doc@example.com", "practitioner", DOC)
    b = actors.owner("b@example.com", "facility", CLINIC)
    return a, a_doc, b


def test_idor_matrix(registry, two_owners, monkeypatch):
    from cura.api import netguard

    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    a, _a_doc, b = two_owners
    hook = registry.post(f"/v1/facilities/{HOSP}/webhooks", json={"url": "https://a.example.com/h"}, headers=a).json()
    key = registry.post(f"/v1/facilities/{HOSP}/api-keys", json={"name": "site"}, headers=a).json()
    before = registry.get(f"/v1/facilities/{HOSP}").json()
    attacks = [
        ("post", f"/v1/facilities/{HOSP}/webhooks", {"url": "https://b.example.com/h"}, 403),
        ("get", f"/v1/facilities/{HOSP}/webhooks", None, 403),
        ("delete", f"/v1/webhooks/{hook['id']}", None, 404),
        ("get", f"/v1/webhooks/{hook['id']}/deliveries", None, 404),
        ("post", f"/v1/facilities/{HOSP}/api-keys", {"name": "x"}, 403),
        ("get", f"/v1/facilities/{HOSP}/api-keys", None, 403),
        ("post", f"/v1/api-keys/{key['id']}/revoke", None, 404),
        ("get", f"/v1/facilities/{HOSP}/activity", None, 403),
        ("post", f"/v1/facilities/{HOSP}/practitioners", {"practitioner_id": PEDS}, 403),
        ("post", f"/v1/facilities/{HOSP}/invitations", {"practitioner_id": PEDS}, 403),
        ("post", "/v1/availability", {"subject_type": "facility", "subject_id": HOSP, "state": "closed"}, 403),
        ("post", "/v1/availability", {"subject_type": "practitioner", "subject_id": DOC, "state": "unavailable"}, 403),
        ("post", f"/v1/facilities/{HOSP}/load", {"level": 0.9}, 403),
        ("post", f"/v1/roles/{ROLE_HOSP}/end", {}, 403),
        ("patch", f"/v1/practitioners/{DOC}", {"patch": {"contact": {"email": "b@evil.example"}}}, 403),
    ]
    for method, url, body, expected in attacks:
        kwargs = {"headers": b} | ({"json": body} if body is not None else {})
        r = getattr(registry, method)(url, **kwargs)
        assert r.status_code == expected, f"{method.upper()} {url}: {r.status_code} {r.text}"
    # Editing A's record as B is only ever a proposal for a moderator; nothing changes.
    r = registry.patch(f"/v1/facilities/{HOSP}", json={"patch": {"beds": 1}}, headers=b)
    assert r.status_code == 202
    after = registry.get(f"/v1/facilities/{HOSP}").json()
    assert after["beds"] == before.get("beds") and after["version"] == before["version"]
    # And A still has their webhook and key.
    assert registry.get(f"/v1/facilities/{HOSP}/webhooks", headers=a).json()["data"][0]["active"]
    assert registry.get(f"/v1/facilities/{HOSP}/api-keys", headers=a).json()["data"][0]["revoked_at"] is None


def test_system_fields_cannot_be_written(registry, actors):
    owner = actors.owner("own@example.com", "facility", HOSP)
    for field in ("demo", "id", "rating", "license_verified", "quality"):
        r = registry.patch(f"/v1/facilities/{HOSP}", json={"patch": {field: True}}, headers=owner)
        assert r.status_code == 422, field


def test_new_bodies_reject_unknown_fields(registry):
    user = phone_user(registry, "0791114400")
    cases = [
        ("/v1/auth/phone/verify", {"challenge_id": "pc_x", "code": "123456", "admin": True}),
        ("/v1/claims", {"subject_type": "practitioner", "subject_id": DOC, "evidence": {"is_admin": True}}),
        (f"/v1/facilities/{HOSP}/claims", {"method": "documents", "level": "verified"}),
        (f"/v1/facilities/{HOSP}/webhooks", {"url": "https://a.example.com", "secret": "mine"}),
    ]
    for url, body in cases:
        assert registry.post(url, json=body, headers=user).status_code == 422, url


def test_auth_endpoints_are_rate_limited(registry):
    statuses = [
        registry.post("/v1/auth/login", json={"email": "x@example.com", "password": "wrong password"}).status_code
        for _ in range(12)
    ]
    assert statuses[:10] == [401] * 10 and 429 in statuses


def test_demo_site_is_a_real_page_with_a_strict_csp(registry):
    r = registry.get(f"/demo/site/{HOSP}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in r.text and f'<style nonce="{nonce}">' in r.text
    assert "unsafe-inline" not in csp and "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "Powered by Cura API" in r.text and "/v1/stream" in r.text
    assert registry.get(f"/demo/site/{HOSP}").headers["Content-Security-Policy"] != csp  # fresh nonce
    assert registry.get("/demo/site/<script>").status_code == 404
    assert registry.get("/demo/site/JO-AM-HSP-99999").status_code == 404
