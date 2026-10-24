"""Small security primitives shared by the new endpoints: windowed rate limits, the audit log,
OTP hashing, secret encryption, phone formatting and the caller's IP."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request
from sqlalchemy.orm import Session

from cura import normalize as nz
from cura.api.db import AuditLog
from cura.api.errors import Problem
from cura.api.settings import settings

# --- rate limits ---------------------------------------------------------------------------------


class WindowLimiter:
    """At most `limit` events per key in a sliding window. In-process (one API worker), like the bucket
    limiter in `deps`; a deployment with several workers moves both to Redis."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Records the attempt if allowed. Returns (allowed, seconds until the next one is allowed)."""
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(q[0] + window_s - now) + 1)
            q.append(now)
            return True, 0

    def last(self, key: str) -> float | None:
        with self._lock:
            q = self._events.get(key)
            return q[-1] if q else None

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


windows = WindowLimiter()


def throttle(key: str, limit: int, window_s: float, what: str = "attempts") -> None:
    allowed, retry = windows.hit(key, limit, window_s)
    if not allowed:
        raise Problem(
            429,
            "Too many requests",
            f"Too many {what}. Try again in {retry} s.",
            headers={"Retry-After": str(retry)},
            retry_after=retry,
        )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- audit ---------------------------------------------------------------------------------------


def audit(
    db: Session,
    action: str,
    *,
    actor_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    request: Request | None = None,
    **detail,
) -> None:
    """Adds an audit row to the session; it commits with the action it records."""
    db.add(
        AuditLog(
            action=action,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            ip=client_ip(request) if request is not None else None,
            detail={k: v for k, v in detail.items() if v is not None},
        )
    )


# --- one-time codes ------------------------------------------------------------------------------


def _key(purpose: bytes) -> bytes:
    return hmac.new(settings().jwt_secret.encode(), purpose, hashlib.sha256).digest()


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def code_hash(challenge_id: str, code: str) -> str:
    return hmac.new(_key(b"cura-otp-v1"), f"{challenge_id}:{code.strip()}".encode(), hashlib.sha256).hexdigest()


def code_matches(challenge_id: str, code: str, stored: str) -> bool:
    return hmac.compare_digest(code_hash(challenge_id, code), stored)


# --- encryption of secrets Cura must read back ----------------------------------------------------


def _fernet() -> Fernet:
    configured = settings().encryption_key
    key = configured.encode() if configured else base64.urlsafe_b64encode(_key(b"cura-fernet-v1"))
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("cannot decrypt: CURA_ENCRYPTION_KEY or CURA_JWT_SECRET changed") from exc


# --- phones --------------------------------------------------------------------------------------


def normalise_phone(raw: str | None) -> str | None:
    return nz.phone_e164(raw)


def is_mobile(e164: str) -> bool:
    import phonenumbers

    try:
        kind = phonenumbers.number_type(phonenumbers.parse(e164))
    except phonenumbers.NumberParseException:
        return False
    return kind in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)


def pretty_phone(e164: str) -> str:
    """+962795550101 → "+962 79 555 0101"; +96265000001 → "+962 6 500 0001"."""
    if e164.startswith("+9627") and len(e164) == 13:
        n = e164[4:]
        return f"+962 {n[:2]} {n[2:5]} {n[5:]}"
    if e164.startswith("+962") and len(e164) == 12:
        n = e164[4:]
        return f"+962 {n[0]} {n[1:4]} {n[4:]}"
    return e164


def mask_phone(e164: str) -> str:
    """Enough to recognise, not enough to learn: "+962 6 5•• ••12", "+962 79 5•• ••01"."""
    pretty = pretty_phone(e164)
    head, _, rest = pretty.rpartition(" ") if pretty.count(" ") >= 3 else ("", "", pretty)
    if not head:
        return pretty[:-4] + "••" + pretty[-2:]
    prefix, middle = head.rsplit(" ", 1)
    return f"{prefix} {middle[0]}{'•' * (len(middle) - 1)} {'•' * (len(rest) - 2)}{rest[-2:]}"


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}{'•' * max(1, len(local) - 1)}@{domain}"
