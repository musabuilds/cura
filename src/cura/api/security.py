"""Credentials: password hashing, opaque tokens, API keys, JWT access tokens and PKCE."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from cura.api.settings import settings

_hasher = PasswordHasher()  # Argon2id, library defaults (RFC 9106 second recommended profile)

SCOPES = {
    "profile": "Read your Cura account (email, name, verified claims)",
    "reviews:write": "Post reviews and ratings on your behalf",
    "proposals:write": "Suggest corrections and additions to the registry on your behalf",
    "practitioner:manage": "Edit the doctor profiles and facilities you have verified ownership of",
    "moderation": "Approve or reject proposals and claims (moderators only)",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def sha256(value: str) -> str:
    """For high-entropy secrets (tokens, keys) a fast hash is correct; Argon2 is for passwords."""
    return hashlib.sha256(value.encode()).hexdigest()


def opaque_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def new_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, hash). Only the hash is stored; the prefix locates the row."""
    prefix = secrets.token_hex(4)
    key = f"cura_{prefix}_{secrets.token_urlsafe(24)}"
    return key, prefix, sha256(key)


def parse_api_key(key: str) -> str | None:
    parts = key.split("_", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "cura" else None


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def issue_access_token(user_id: str, client_id: str, scope: str, role: str) -> tuple[str, int]:
    s = settings()
    now = datetime.now(UTC)
    claims = {
        "iss": s.jwt_issuer,
        "sub": user_id,
        "aud": "cura-api",
        "azp": client_id,
        "scope": scope,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=s.access_token_ttl_s)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256"), s.access_token_ttl_s


def decode_access_token(token: str) -> dict:
    s = settings()
    return jwt.decode(
        token,
        s.jwt_secret,
        algorithms=["HS256"],
        audience="cura-api",
        issuer=s.jwt_issuer,
        options={"require": ["exp", "sub", "iss", "aud"]},
    )


def pkce_verify(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256. `plain` is intentionally unsupported."""
    if not 43 <= len(verifier) <= 128:
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return constant_eq(digest, challenge)
