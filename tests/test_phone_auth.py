"""Phone sign-in for the first-party app: codes, lockout, enumeration resistance, sessions, the account."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from cura.api import messaging
from cura.api.settings import settings

PHONE = "0795550101"
E164 = "+962795550101"


def start(client, phone=PHONE, **kw):
    return client.post("/v1/auth/phone/start", json={"phone": phone, **kw})


def last_code(to=E164) -> str:
    sent = messaging.console().last("sms", to)
    assert sent is not None, "no SMS was sent"
    return sent.code


def sign_in(client, phone=PHONE, e164=E164) -> dict:
    ch = start(client, phone).json()
    r = client.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": last_code(e164)})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_start_sends_a_code_and_never_returns_it(registry):
    r = start(registry, "+962 79 555 0101")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone"] == "+962 79 555 0101" and body["expires_in"] == 300 and body["resend_in"] == 30
    assert body["channel"] == "sms" and body["challenge_id"].startswith("pc_")
    code = last_code()
    assert len(code) == 6 and code not in r.text


def test_verify_creates_the_account_then_signs_it_in(registry):
    first = sign_in(registry)
    assert first["is_new"] is True and first["token_type"] == "Bearer" and first["expires_in"] == 900
    user = first["user"]
    assert user["phone"] == E164 and user["id"].startswith("usr_")
    assert user["practitioner"] is None and user["facilities"] == [] and user["pending_claims"] == []
    from cura.api import guard

    guard.windows.reset()  # the resend gap is not what this test is about
    again = sign_in(registry)
    assert again["is_new"] is False and again["user"]["id"] == user["id"]
    me = registry.get("/v1/me", headers=bearer(again)).json()
    assert me["id"] == user["id"] and me["client_id"] == "cura-mobile"


def test_no_account_enumeration(registry):
    sign_in(registry)  # +962795550101 now has an account
    known = start(registry, "0795550102")
    unknown = start(registry, "0795550103")
    assert known.status_code == unknown.status_code == 200
    strip = lambda b: {k: v for k, v in b.items() if k not in ("challenge_id", "phone")}  # noqa: E731
    assert strip(known.json()) == strip(unknown.json())
    assert set(known.json()) == set(unknown.json())


def test_malformed_phone_and_unknown_fields_are_rejected(registry):
    assert start(registry, "12").status_code == 422
    assert start(registry, "065000001").status_code == 422  # a landline cannot receive the code
    assert start(registry, PHONE, extra="x").status_code == 422


def test_brute_force_locks_the_code_after_five_attempts(registry):
    ch = start(registry).json()
    good = last_code()
    wrong = "000000" if good != "000000" else "111111"
    left = []
    for _ in range(5):
        r = registry.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": wrong})
        assert r.status_code == 422
        left.append(r.json()["attempts_left"])
    assert left == [4, 3, 2, 1, 0]
    # Locked: even the right code no longer works.
    r = registry.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": good})
    assert r.status_code == 410


def test_expired_and_reused_codes(registry):
    from cura.api import db
    from cura.api.db import OtpChallenge

    ch = start(registry).json()
    code = last_code()
    with db.new_session() as s:
        row = s.get(OtpChallenge, ch["challenge_id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert (
        registry.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": code}).status_code
        == 410
    )
    assert registry.post("/v1/auth/phone/verify", json={"challenge_id": "pc_nope", "code": code}).status_code == 410


def test_a_new_code_voids_the_old_one_and_resends_are_limited(registry, monkeypatch):
    from cura.api.routes import account

    first = start(registry).json()
    old = last_code()
    assert start(registry).status_code == 429  # 30 s between codes
    monkeypatch.setattr(account, "RESEND_S", 0)
    second = start(registry).json()
    assert (
        registry.post("/v1/auth/phone/verify", json={"challenge_id": first["challenge_id"], "code": old}).status_code
        == 410
    )
    assert start(registry).status_code == 200  # third code in ten minutes
    r = start(registry)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert second["challenge_id"] != first["challenge_id"]


def test_demo_numbers_accept_the_fixed_code_in_development(registry):
    ch = start(registry, "0790000003").json()
    assert messaging.console().last("sms", "+962790000003") is None  # nothing is sent for a demo number
    r = registry.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": "246810"})
    assert r.status_code == 200 and r.json()["user"]["phone"] == "+962790000003"


def test_demo_code_is_hard_disabled_in_production(registry, monkeypatch):
    monkeypatch.setenv("CURA_ENVIRONMENT", "production")
    monkeypatch.setenv("CURA_JWT_SECRET", "x" * 64)
    monkeypatch.setenv("CURA_DEMO_PHONES", "0790000001")
    settings.cache_clear()
    messaging.sms.cache_clear()
    assert settings().demo_numbers == set()
    ch = start(registry, "0790000001").json()
    r = registry.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": "246810"})
    assert r.status_code == 422  # a real random code was sent instead
    real = last_code("+962790000001")
    r = registry.post("/v1/auth/phone/verify", json={"challenge_id": ch["challenge_id"], "code": real})
    assert r.status_code == 200


def test_refresh_rotates_and_reuse_revokes_the_session(registry):
    tokens = sign_in(registry)
    rotated = registry.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200, rotated.text
    new = rotated.json()
    assert new["refresh_token"] != tokens["refresh_token"] and new["token_type"] == "Bearer"
    replay = registry.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401
    # The whole family is dead, including the token issued by the legitimate rotation.
    assert registry.post("/v1/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401


def test_logout_revokes_and_always_answers_204(registry):
    tokens = sign_in(registry)
    assert registry.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert registry.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert registry.post("/v1/auth/logout", json={"refresh_token": "never-issued-token"}).status_code == 204


def test_patch_me_updates_private_account_fields(registry):
    tokens = sign_in(registry)
    r = registry.patch("/v1/me", json={"name": "Rami Haddad", "email": "rami@example.jo"}, headers=bearer(tokens))
    assert r.status_code == 200 and r.json()["name"] == "Rami Haddad" and r.json()["email"] == "rami@example.jo"
    assert registry.patch("/v1/me", json={"role": "admin"}, headers=bearer(tokens)).status_code == 422
    assert registry.patch("/v1/me", json={}, headers=bearer(tokens)).status_code == 422


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c, s: jwt.encode(c, s, algorithm="HS512"),  # algorithm not pinned
        lambda c, s: jwt.encode(c | {"aud": "someone-else"}, s, algorithm="HS256"),
        lambda c, s: jwt.encode(c | {"iss": "https://evil.example"}, s, algorithm="HS256"),
        lambda c, s: jwt.encode(c | {"exp": int(datetime.now(UTC).timestamp()) - 10}, s, algorithm="HS256"),
        lambda c, s: jwt.encode(c, "not-the-secret-" * 4, algorithm="HS256"),
        lambda c, s: jwt.encode({k: v for k, v in c.items() if k != "iat"}, s, algorithm="HS256"),
        lambda c, s: jwt.encode(c, None, algorithm="none"),
    ],
)
def test_access_tokens_are_checked_strictly(registry, mutate):
    tokens = sign_in(registry)
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    forged = mutate(claims, settings().jwt_secret)
    r = registry.get("/v1/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert "Traceback" not in r.text
