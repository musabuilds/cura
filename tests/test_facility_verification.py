"""Proving you run a facility: every method in the contract, plus create-new and IDOR on claims."""

import httpx
import pytest

from cura.api import messaging, netguard
from cura.api.routes import onboarding
from helpers import phone_user, set_doc

HOSP, CLINIC, IRBID = "JO-AM-HSP-00001", "JO-AM-CLN-00001", "JO-IR-HSP-00001"


@pytest.fixture()
def public_dns(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])


def offer(client, headers, fid, method):
    data = client.get("/v1/onboarding/facility/candidates", params={"q": "amman hospital"}, headers=headers).json()[
        "data"
    ]
    fac = next(c for c in data if c["facility"]["id"] == fid)
    return next(o for o in fac["verification_methods"] if o["method"] == method), fac


def test_registered_landline_gets_a_voice_code(registry):
    admin = phone_user(registry, "0791113300")
    o, fac = offer(registry, admin, HOSP, "registered_phone")
    assert o["available"] and o["channel"] == "voice" and o["target_masked"] == "+962 6 5•• ••01"
    assert fac["claim_state"] == "unclaimed" and fac["facility"]["phone_masked"] == "+962 6 5•• ••01"
    r = registry.post(f"/v1/facilities/{HOSP}/claims", json={"method": "registered_phone"}, headers=admin)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["claim"]["status"] == "pending" and body["challenge"]["target_masked"] == "+962 6 5•• ••01"
    call = messaging.console().last("voice", "+96265000001")
    assert call is not None and call.code not in r.text
    claim_id = body["claim"]["id"]
    wrong = "000000" if call.code != "000000" else "111111"
    bad = registry.post(f"/v1/facility-claims/{claim_id}/verify", json={"code": wrong}, headers=admin)
    assert bad.status_code == 422 and bad.json()["attempts_left"] == 4
    ok = registry.post(f"/v1/facility-claims/{claim_id}/verify", json={"code": call.code}, headers=admin)
    assert ok.status_code == 200 and ok.json()["status"] == "approved" and ok.json()["method"] == "registered_phone"
    fac = registry.get(f"/v1/facilities/{HOSP}").json()
    assert fac["verification"]["level"] == "verified" and fac["verification"]["method"] == "registered_phone"
    me = registry.get("/v1/me", headers=admin).json()
    assert me["facilities"][0]["facility"]["id"] == HOSP and me["facilities"][0]["role"] == "admin"


def test_phone_match_is_instant(registry):
    set_doc("facility", CLINIC, contact={"phones": ["+962791113311"]})
    owner = phone_user(registry, "0791113311")
    data = registry.get("/v1/onboarding/facility/candidates", headers=owner).json()["data"]
    assert data[0]["facility"]["id"] == CLINIC and data[0]["reasons"][0] == "phone_match"
    assert next(o for o in data[0]["verification_methods"] if o["method"] == "phone_match")["available"]
    r = registry.post(f"/v1/facilities/{CLINIC}/claims", json={"method": "phone_match"}, headers=owner)
    assert r.status_code == 201 and r.json()["claim"]["status"] == "approved"
    other = phone_user(registry, "0791113322")
    r = registry.post(f"/v1/facilities/{CLINIC}/claims", json={"method": "registered_phone"}, headers=other)
    assert r.status_code == 409 and r.json()["can_dispute"]


def test_work_email_must_be_on_the_records_own_domain(registry):
    admin = phone_user(registry, "0791113333")
    url = f"/v1/facilities/{HOSP}/claims"
    assert (
        registry.post(url, json={"method": "work_email", "email": "boss@gmail.com"}, headers=admin).status_code == 422
    )
    assert (
        registry.post(
            url, json={"method": "work_email", "email": "it@ammanhospital.jo.evil.com"}, headers=admin
        ).status_code
        == 422
    )
    assert registry.post(url, json={"method": "work_email", "email": "it@other.jo"}, headers=admin).status_code == 422
    r = registry.post(url, json={"method": "work_email", "email": "IT@ammanhospital.jo"}, headers=admin)
    assert r.status_code == 201, r.text
    assert r.json()["challenge"]["target_masked"] == "i•@ammanhospital.jo"
    mail = messaging.console().last("email", "it@ammanhospital.jo")
    ok = registry.post(f"/v1/facility-claims/{r.json()['claim']['id']}/verify", json={"code": mail.code}, headers=admin)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


def test_shared_hosts_cannot_prove_anything(registry):
    set_doc("facility", IRBID, contact={"website": "https://www.facebook.com/irbidkids"})
    user = phone_user(registry, "0791113344")
    r = registry.post(f"/v1/facilities/{IRBID}/claims", json={"method": "website_token"}, headers=user)
    assert r.status_code == 422
    r = registry.post(
        f"/v1/facilities/{IRBID}/claims", json={"method": "work_email", "email": "a@facebook.com"}, headers=user
    )
    assert r.status_code == 422


def test_website_token_via_well_known_file(registry, public_dns, monkeypatch):
    admin = phone_user(registry, "0791113355")
    r = registry.post(f"/v1/facilities/{HOSP}/claims", json={"method": "website_token"}, headers=admin)
    assert r.status_code == 201, r.text
    token = r.json()["website_token"]
    assert token.startswith("cura-verify=") and r.json()["instructions"]["file"] == (
        "https://ammanhospital.jo/.well-known/cura-verification.txt"
    )
    published = {"body": b"nothing here"}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, content=published["body"])

    monkeypatch.setattr(netguard, "_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(onboarding, "dns_txt", lambda name: [])
    claim_id = r.json()["claim"]["id"]
    assert registry.post(f"/v1/facility-claims/{claim_id}/verify", headers=admin).status_code == 422
    published["body"] = f"# Cura\n{token}\n".encode()
    ok = registry.post(f"/v1/facility-claims/{claim_id}/verify", headers=admin)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    # The request went to the pinned, checked address, with the record's host for TLS and Host.
    req = seen[-1]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "ammanhospital.jo"
    assert req.extensions["sni_hostname"] == "ammanhospital.jo"


def test_website_token_via_dns_txt(registry, public_dns, monkeypatch):
    admin = phone_user(registry, "0791113366")
    r = registry.post(f"/v1/facilities/{HOSP}/claims", json={"method": "website_token"}, headers=admin).json()
    monkeypatch.setattr(netguard, "_transport", lambda: httpx.MockTransport(lambda req: httpx.Response(404)))
    monkeypatch.setattr(onboarding, "dns_txt", lambda name: [r["website_token"]] if name.startswith("_cura") else [])
    ok = registry.post(f"/v1/facility-claims/{r['claim']['id']}/verify", headers=admin)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


def test_documents_go_to_a_moderator(registry, actors):
    admin = phone_user(registry, "0791113377")
    r = registry.post(
        f"/v1/facilities/{IRBID}/claims",
        json={"method": "documents", "documents": {"moh_license": "MOH-123", "commercial_registration": "CR-9"}},
        headers=admin,
    )
    assert r.status_code == 201 and r.json()["claim"]["status"] == "pending"
    assert registry.get(f"/v1/facilities/{IRBID}").json()["verification"]["level"] == "pending"
    registry.post(f"/v1/claims/{r.json()['claim']['id']}/approve", json={}, headers=actors.moderator)
    v = registry.get(f"/v1/facilities/{IRBID}").json()["verification"]
    assert v["level"] == "reviewed" and v["method"] == "documents"


def test_create_facility_then_prove_it(registry):
    user = phone_user(registry, "0791113388")
    record = {
        "type": "hospital",
        "name": {"en": "Amman Hospital"},
        "address": {"governorate": "JO-AM"},
        "location": {"lat": 31.9541, "lon": 35.9109},
    }
    r = registry.post("/v1/facilities", json={"record": record}, headers=user)
    assert r.status_code == 409 and r.json()["candidates"][0]["id"] == HOSP
    fresh = {
        "type": "clinic",
        "name": {"en": "Shmeisani Family Clinic"},
        "address": {"governorate": "JO-AM"},
        "contact": {"phones": ["+962791113388"]},
    }
    r = registry.post("/v1/facilities", json={"record": fresh, "confirm_new": True}, headers=user)
    assert r.status_code == 201, r.text
    fid = r.json()["facility"]["id"]
    assert r.json()["facility"]["verification"]["level"] == "unverified"
    me = registry.get("/v1/me", headers=user).json()
    assert me["facilities"][0]["role"] == "owner" and me["facilities"][0]["verification"]["level"] == "unverified"
    up = registry.post(f"/v1/facilities/{fid}/claims", json={"method": "phone_match"}, headers=user)
    assert up.status_code == 201 and up.json()["claim"]["status"] == "approved"
    assert registry.get(f"/v1/facilities/{fid}").json()["verification"]["level"] == "verified"


def test_claims_are_private_to_their_author(registry):
    alice = phone_user(registry, "0791113399")
    r = registry.post(f"/v1/facilities/{HOSP}/claims", json={"method": "registered_phone"}, headers=alice).json()
    code = messaging.console().last("voice", "+96265000001").code
    mallory = phone_user(registry, "0791113400")
    stolen = registry.post(f"/v1/facility-claims/{r['claim']['id']}/verify", json={"code": code}, headers=mallory)
    assert stolen.status_code == 404
    assert registry.get(f"/v1/facilities/{HOSP}").json()["verification"] is None


# --- the SSRF guard ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "::1",
        "::ffff:10.0.0.1",
        "fd00::1",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
def test_private_and_reserved_addresses_are_refused(monkeypatch, address):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34", address])
    monkeypatch.setattr(netguard, "_transport", lambda: pytest.fail("must not connect"))
    with pytest.raises(netguard.Blocked):
        netguard.fetch("https://example.jo/.well-known/cura-verification.txt", allowed_host="example.jo")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.jo/x",  # not https
        "https://example.jo:8443/x",  # not the default port
        "https://user:pw@example.jo/x",  # credentials
        "https://93.184.216.34/x",  # IP literal
        "https://localhost/x",
        "https://evil.example/x",  # not the record's host
        "https://example.jo.evil.example/x",
    ],
)
def test_url_rules(monkeypatch, url):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(netguard, "_transport", lambda: pytest.fail("must not connect"))
    with pytest.raises(netguard.Blocked):
        netguard.fetch(url, allowed_host="example.jo")


def test_redirects_to_another_host_are_not_followed(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    hops = []

    def handler(request):
        hops.append(request.headers["host"])
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, content=b"never")

    monkeypatch.setattr(netguard, "_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(netguard.Blocked, match="another host"):
        netguard.fetch("https://example.jo/start", allowed_host="example.jo")
    assert hops == ["example.jo", "example.jo"]  # the same-host hop was followed and re-checked


def test_body_is_capped(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        netguard, "_transport", lambda: httpx.MockTransport(lambda r: httpx.Response(200, content=b"a" * 100_000))
    )
    got = netguard.fetch("https://example.jo/big", allowed_host="example.jo", max_bytes=4096)
    assert len(got.body) == 4096
