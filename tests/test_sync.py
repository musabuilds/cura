"""One record, read everywhere: webhooks (signed, retried, SSRF-guarded), the SSE stream, API keys, activity."""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from cura.api import db, netguard, sync, webhooks
from cura.api.db import WebhookDelivery

DOC, HOSP, CLINIC = "JO-PRC-000001", "JO-AM-HSP-00001", "JO-AM-CLN-00001"
ROLE_HOSP = "JO-ROL-0000001"


@pytest.fixture()
def public_dns(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])


@pytest.fixture()
def hospital(actors):
    return actors.owner("admin@ammanhospital.jo", "facility", HOSP)


@pytest.fixture()
def doctor(actors):
    return actors.owner("rami@example.com", "practitioner", DOC)


def test_webhook_urls_are_ssrf_guarded(registry, hospital, monkeypatch):
    url = f"/v1/facilities/{HOSP}/webhooks"
    assert registry.post(url, json={"url": "http://hooks.example.com/cura"}, headers=hospital).status_code == 422
    assert registry.post(url, json={"url": "https://10.0.0.8/cura"}, headers=hospital).status_code == 422
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["127.0.0.1"])
    r = registry.post(url, json={"url": "https://rebind.example.com/cura"}, headers=hospital)
    assert r.status_code == 422 and "private" in r.json()["detail"]
    assert (
        registry.post(url, json={"url": "https://x.example.com", "events": ["nope"]}, headers=hospital).status_code
        == 422
    )


def test_signed_delivery_with_retries(registry, hospital, doctor, public_dns, monkeypatch):
    created = registry.post(
        f"/v1/facilities/{HOSP}/webhooks", json={"url": "https://hooks.example.com/cura"}, headers=hospital
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    assert secret.startswith("whsec_")
    listed = registry.get(f"/v1/facilities/{HOSP}/webhooks", headers=hospital).text
    assert secret not in listed  # shown once
    # The doctor changes their hours at the hospital: one write, announced to the hospital's endpoint.
    r = registry.patch(
        f"/v1/roles/{ROLE_HOSP}", json={"patch": {"hours": {"opening_hours": "Su-Th 10:00-14:00"}}}, headers=doctor
    )
    assert r.status_code == 200, r.text
    received = []
    status = {"code": 500}

    def handler(request: httpx.Request):
        received.append(request)
        return httpx.Response(status["code"])

    monkeypatch.setattr(netguard, "_transport", lambda: httpx.MockTransport(handler))
    with db.new_session() as s:
        assert webhooks.deliver_due(s) == 1
        d = s.scalars(select(WebhookDelivery)).one()
        assert d.status == "pending" and d.attempts == 1 and d.response_status == 500 and d.next_attempt_at
        d.next_attempt_at = d.created_at  # make the retry due now
        s.commit()
        status["code"] = 204
        assert webhooks.deliver_due(s) == 1
        d = s.scalars(select(WebhookDelivery)).one()
        assert d.status == "delivered" and d.attempts == 2
    req = received[-1]
    body = req.content
    payload = json.loads(body)
    assert payload["type"] == "role.updated" and payload["data"]["id"] == ROLE_HOSP and payload["facility_id"] == HOSP
    assert req.headers["cura-event"] == "role.updated"
    assert webhooks.verify_signature(secret, body, req.headers["cura-signature"])
    assert not webhooks.verify_signature(secret, body + b" ", req.headers["cura-signature"])
    assert not webhooks.verify_signature("whsec_wrong", body, req.headers["cura-signature"])
    t = int(req.headers["cura-signature"].split(",")[0][2:])
    assert not webhooks.verify_signature(secret, body, req.headers["cura-signature"], now=t + 3600)  # replay
    log = registry.get(f"/v1/webhooks/{created.json()['id']}/deliveries", headers=hospital).json()["data"]
    assert log[0]["status"] == "delivered" and log[0]["attempts"] == 2


def test_delivery_gives_up_after_the_last_backoff(registry, hospital, doctor, public_dns, monkeypatch):
    registry.post(f"/v1/facilities/{HOSP}/webhooks", json={"url": "https://hooks.example.com/x"}, headers=hospital)
    registry.patch(f"/v1/roles/{ROLE_HOSP}", json={"patch": {"consultation_fee": {"amount": 30}}}, headers=doctor)
    monkeypatch.setattr(netguard, "_transport", lambda: httpx.MockTransport(lambda r: httpx.Response(503)))
    with db.new_session() as s:
        for _ in range(webhooks.MAX_ATTEMPTS):
            d = s.scalars(select(WebhookDelivery)).one()
            d.next_attempt_at = d.created_at
            s.commit()
            webhooks.deliver_due(s)
        d = s.scalars(select(WebhookDelivery)).one()
        assert d.status == "failed" and d.attempts == webhooks.MAX_ATTEMPTS


def test_dns_rebinding_after_registration_is_caught_at_delivery(registry, hospital, doctor, monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    registry.post(f"/v1/facilities/{HOSP}/webhooks", json={"url": "https://hooks.example.com/x"}, headers=hospital)
    registry.patch(f"/v1/roles/{ROLE_HOSP}", json={"patch": {"consultation_fee": {"amount": 31}}}, headers=doctor)
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["169.254.169.254"])
    monkeypatch.setattr(netguard, "_transport", lambda: pytest.fail("must not connect"))
    with db.new_session() as s:
        webhooks.deliver_due(s)
        d = s.scalars(select(WebhookDelivery)).one()
        assert d.status == "pending" and "private" in d.error


def test_availability_reaches_every_workplace_and_subscribed_streams(registry, doctor):
    async def scenario():
        hosp_stream = sync.bus.subscribe(HOSP, asyncio.get_running_loop())
        irbid_stream = sync.bus.subscribe("JO-IR-HSP-00001", asyncio.get_running_loop())
        r = await asyncio.to_thread(
            registry.post,
            "/v1/availability",
            json={"subject_type": "practitioner", "subject_id": DOC, "state": "unavailable", "reason": "leave"},
            headers=doctor,
        )
        assert r.status_code == 201, r.text
        msg = await asyncio.wait_for(hosp_stream.queue.get(), 2)
        assert msg["type"] == "availability.changed" and msg["practitioner_id"] == DOC
        assert irbid_stream.queue.empty()  # he does not work there
        sync.bus.unsubscribe(hosp_stream)
        sync.bus.unsubscribe(irbid_stream)

    asyncio.run(scenario())


def test_sse_frames(registry, doctor):
    from cura.api.routes.integrations import event_stream

    async def scenario():
        async def connected():
            return False

        gen = event_stream(CLINIC, connected, heartbeat_s=5, max_events=1)
        assert (await gen.__anext__()).startswith("retry:")
        assert "event: hello" in await gen.__anext__()
        nxt = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        await asyncio.to_thread(
            registry.patch,
            "/v1/roles/JO-ROL-0000002",
            json={"patch": {"hours": {"opening_hours": "Sa 10:00-13:00"}}},
            headers=doctor,
        )
        frame = await asyncio.wait_for(nxt, 2)
        assert frame.startswith("id: ") and "event: change" in frame
        data = json.loads(frame.split("data: ", 1)[1])
        assert data["kind"] == "role" and data["id"] == "JO-ROL-0000002" and data["summary"]
        await gen.aclose()

    asyncio.run(scenario())
    assert sync.bus.count() == 0
    assert registry.get("/v1/stream", params={"facility_id": "JO-AM-HSP-99999"}).status_code == 404


def test_facility_api_keys(registry, hospital, actors):
    r = registry.post(f"/v1/facilities/{HOSP}/api-keys", json={"name": "Website"}, headers=hospital)
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key.startswith("curafk_") and r.json()["prefix"] in key
    listed = registry.get(f"/v1/facilities/{HOSP}/api-keys", headers=hospital)
    assert key not in listed.text and listed.json()["data"][0]["name"] == "Website"
    read = registry.get(f"/v1/facilities/{HOSP}", headers={"X-API-Key": key})
    assert read.status_code == 200 and read.headers["X-RateLimit-Limit"] == "600"
    # Read-only: a key is not a user.
    assert (
        registry.patch(f"/v1/facilities/{HOSP}", json={"patch": {"beds": 1}}, headers={"X-API-Key": key}).status_code
        == 401
    )
    other = actors.owner("other@example.com", "facility", "JO-AM-CLN-00001")
    assert registry.post(f"/v1/api-keys/{r.json()['id']}/revoke", headers=other).status_code == 404
    assert registry.post(f"/v1/api-keys/{r.json()['id']}/revoke", headers=hospital).json()["revoked_at"]
    assert registry.get(f"/v1/facilities/{HOSP}", headers={"X-API-Key": key}).status_code == 401
    assert registry.get(f"/v1/facilities/{HOSP}", headers={"X-API-Key": key[:-2] + "xx"}).status_code == 401


def test_activity_feed(registry, hospital, doctor):
    registry.patch(f"/v1/roles/{ROLE_HOSP}", json={"patch": {"consultation_fee": {"amount": 20}}}, headers=doctor)
    registry.post(
        "/v1/availability",
        json={"subject_type": "role", "subject_id": ROLE_HOSP, "state": "busy", "reason": "procedure"},
        headers=doctor,
    )
    feed = registry.get(f"/v1/facilities/{HOSP}/activity", headers=hospital)
    assert feed.status_code == 200, feed.text
    items = feed.json()["data"]
    assert items[0]["kind"] == "availability" and "busy" in items[0]["summary"]
    assert any(i["kind"] == "role" and i["fields"] == ["consultation_fee"] for i in items)
    assert registry.get(f"/v1/facilities/{HOSP}/activity", headers=doctor).status_code == 403
