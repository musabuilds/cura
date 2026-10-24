"""Request principal: who is calling (an app, a user, both, or nobody) and what they may do."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import security
from cura.api.db import App, Claim, User, get_session
from cura.api.errors import Problem
from cura.api.settings import settings


@dataclass
class Principal:
    app: App | None = None
    user: User | None = None
    scopes: set[str] = field(default_factory=set)
    client_id: str | None = None

    @property
    def is_moderator(self) -> bool:
        return bool(self.user and self.user.role in ("moderator", "admin") and "moderation" in self.scopes)


class RateLimiter:
    """Token bucket per caller. In-process: run one API worker per limiter, or move this to Redis."""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = defaultdict(lambda: (-1.0, 0.0))
        self._lock = Lock()

    def hit(self, key: str, per_minute: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets[key]
            if tokens < 0:
                tokens, last = float(per_minute), now
            tokens = min(per_minute, tokens + (now - last) * per_minute / 60)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False, 0
            self._buckets[key] = (tokens - 1, now)
            return True, int(tokens - 1)


limiter = RateLimiter()


def _app_from_key(db: Session, key: str) -> App | None:
    prefix = security.parse_api_key(key)
    if not prefix:
        return None
    app = db.scalar(select(App).where(App.api_key_prefix == prefix))
    if app and not app.disabled and security.constant_eq(app.api_key_hash, security.sha256(key)):
        return app
    return None


def principal(
    request: Request,
    db: Session = Depends(get_session),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> Principal:
    p = Principal()
    if x_api_key:
        p.app = _app_from_key(db, x_api_key)
        if p.app is None:
            raise Problem(401, "Invalid API key", "The X-API-Key header does not match an active app.")
        p.client_id = p.app.client_id
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Problem(401, "Invalid authorization header", "Use `Authorization: Bearer <access_token>`.")
        try:
            claims = security.decode_access_token(token)
        except jwt.PyJWTError as exc:
            raise Problem(
                401, "Invalid access token", str(exc), headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            ) from exc
        user = db.get(User, claims["sub"])
        if user is None or user.disabled:
            raise Problem(401, "Invalid access token", "The user no longer exists or is disabled.")
        if p.app and claims.get("azp") != p.app.client_id:
            raise Problem(401, "Token/app mismatch", "The access token was issued to a different app than the API key.")
        p.user = user
        p.scopes = set(claims.get("scope", "").split())
        p.client_id = claims.get("azp")

    s = settings()
    key, quota = (
        (f"app:{p.app.id}", s.rate_app_per_min)
        if p.app
        else (f"ip:{request.client.host if request.client else '?'}", s.rate_anonymous_per_min)
    )
    if p.user:
        key, quota = f"user:{p.user.id}", max(quota, s.rate_app_per_min)
    allowed, remaining = limiter.hit(key, quota)
    request.state.rate = (quota, remaining)
    if not allowed:
        raise Problem(
            429,
            "Rate limit exceeded",
            f"{quota} requests/minute. Register an app for a higher limit.",
            headers={"Retry-After": "60"},
        )
    return p


def require_user(*scopes: str):
    def dep(p: Principal = Depends(principal)) -> Principal:
        if p.user is None:
            raise Problem(
                401,
                "Authentication required",
                "Sign the user in with OAuth 2.0 (see /docs#auth).",
                headers={"WWW-Authenticate": "Bearer"},
            )
        missing = [s for s in scopes if s not in p.scopes]
        if missing:
            raise Problem(
                403,
                "Insufficient scope",
                f"Token lacks scope(s): {' '.join(missing)}",
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope" scope="{" ".join(scopes)}"'},
            )
        return p

    return dep


def require_moderator(p: Principal = Depends(require_user("moderation"))) -> Principal:
    if not p.is_moderator:
        raise Problem(403, "Moderator only", "This action needs a moderator account.")
    return p


def owned_subjects(db: Session, user: User | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"practitioner": set(), "facility": set()}
    if user is None:
        return out
    for claim in db.scalars(select(Claim).where(Claim.user_id == user.id, Claim.status == "approved")):
        out[claim.subject_type].add(claim.subject_id)
    return out
