"""Helpers shared by the mobile-API tests (imported by name: tests/ is on sys.path)."""

from cura.api import db, guard, messaging, store


def phone_user(client, phone: str, name: str | None = None) -> dict:
    """Signs a phone number in and returns auth headers."""
    guard.windows.reset()
    ch = client.post("/v1/auth/phone/start", json={"phone": phone}).json()
    e164 = "+962" + phone[1:]
    code = messaging.console().last("sms", e164).code
    tokens = client.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": code}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    if name:
        client.patch("/v1/me", json={"name": name}, headers=headers)
    return headers


def set_doc(kind: str, rid: str, **fields) -> None:
    with db.new_session() as s:
        row = store.get(s, kind, rid)
        store.put(s, kind, row.doc | fields, dirty=False, origin="seed")
        s.commit()
