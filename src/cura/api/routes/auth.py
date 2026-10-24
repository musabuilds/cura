"""Accounts, app registration and OAuth 2.0 (authorization code + PKCE, refresh rotation).

Third-party apps never see a user's password: they send the user to /oauth/authorize, receive a
one-time code on their redirect URI, and exchange it with the PKCE verifier at /oauth/token.
"""

from __future__ import annotations

import html
import secrets
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import security
from cura.api.db import App, AuthCode, Claim, RefreshToken, User, get_session, new_uuid, utcnow
from cura.api.deps import Principal, owned_subjects, require_user
from cura.api.errors import Problem
from cura.api.settings import settings

router = APIRouter()
FIRST_PARTY = "cura-first-party"
USER_SCOPES = ("profile", "reviews:write", "proposals:write", "practitioner:manage")


# --- schemas -------------------------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=10, max_length=256)]
    display_name: Annotated[str | None, Field(max_length=120)] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str
    scope: str


class AppIn(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=120)]
    redirect_uris: list[str] = []
    confidential: bool = False  # True for server-side apps that can keep a secret


# --- helpers -------------------------------------------------------------------------------------


def _user_scopes(user: User) -> list[str]:
    return [*USER_SCOPES, "moderation"] if user.role in ("moderator", "admin") else list(USER_SCOPES)


def _issue(db: Session, user: User, client_id: str, scope: str, family_id: str | None = None) -> TokenOut:
    access, ttl = security.issue_access_token(user.id, client_id, scope, user.role)
    refresh = security.opaque_token()
    db.add(
        RefreshToken(
            token_hash=security.sha256(refresh),
            family_id=family_id or new_uuid(),
            user_id=user.id,
            client_id=client_id,
            scope=scope,
            expires_at=utcnow() + timedelta(seconds=settings().refresh_token_ttl_s),
        )
    )
    db.commit()
    return TokenOut(access_token=access, expires_in=ttl, refresh_token=refresh, scope=scope)


def _authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        security.verify_password(security.hash_password("timing-equaliser"), password)  # no user enumeration
        raise Problem(401, "Invalid credentials", "Email or password is incorrect.")
    if user.disabled or not security.verify_password(user.password_hash, password):
        raise Problem(401, "Invalid credentials", "Email or password is incorrect.")
    return user


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


# --- accounts ------------------------------------------------------------------------------------


@router.post("/v1/auth/register", status_code=201, tags=["auth"])
def register(body: RegisterIn, db: Session = Depends(get_session)) -> dict:
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise Problem(409, "Email already registered", "Sign in instead, or reset your password.")
    user = User(email=email, password_hash=security.hash_password(body.password), display_name=body.display_name)
    db.add(user)
    db.commit()
    return {"id": user.id, "email": user.email}


@router.post("/v1/auth/login", tags=["auth"])
def login(body: LoginIn, db: Session = Depends(get_session)) -> TokenOut:
    """First-party sign-in (Cura's own dashboard and the CLI). Third-party apps must use /oauth/authorize."""
    user = _authenticate(db, body.email, body.password)
    return _issue(db, user, FIRST_PARTY, " ".join(_user_scopes(user)))


@router.get("/v1/me", tags=["auth"])
def me(p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)) -> dict:
    claims = db.scalars(select(Claim).where(Claim.user_id == p.user.id).order_by(Claim.created_at)).all()
    owned = owned_subjects(db, p.user)
    return {
        "id": p.user.id,
        "email": p.user.email,
        "display_name": p.user.display_name,
        "role": p.user.role,
        "scopes": sorted(p.scopes),
        "client_id": p.client_id,
        "owns": {k: sorted(v) for k, v in owned.items()},
        "claims": [
            {"id": c.id, "subject_type": c.subject_type, "subject_id": c.subject_id, "status": c.status} for c in claims
        ],
    }


# --- apps ----------------------------------------------------------------------------------------


@router.post("/v1/apps", status_code=201, tags=["apps"])
def create_app(
    body: AppIn, p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)
) -> dict:
    """Register an app. The API key and client secret are shown **once**; store them in your backend."""
    for uri in body.redirect_uris:
        if not (
            uri.startswith("https://")
            or uri.startswith("http://localhost")
            or uri.startswith("http://127.0.0.1")
            or ("://" in uri and not uri.startswith("http"))
        ):  # custom schemes for mobile apps
            raise Problem(422, "Invalid redirect URI", f"{uri}: use https, localhost, or an app-specific scheme.")
    key, prefix, key_hash = security.new_api_key()
    secret = security.opaque_token() if body.confidential else None
    app = App(
        owner_id=p.user.id,
        name=body.name,
        client_id="cura_app_" + secrets.token_hex(8),
        client_secret_hash=security.sha256(secret) if secret else None,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
        redirect_uris=body.redirect_uris,
        allowed_scopes=" ".join(USER_SCOPES),
    )
    db.add(app)
    db.commit()
    return {
        "id": app.id,
        "name": app.name,
        "client_id": app.client_id,
        "client_secret": secret,
        "api_key": key,
        "redirect_uris": app.redirect_uris,
        "allowed_scopes": app.allowed_scopes.split(),
    }


@router.get("/v1/apps", tags=["apps"])
def list_apps(p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)) -> list[dict]:
    apps = db.scalars(select(App).where(App.owner_id == p.user.id)).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "client_id": a.client_id,
            "api_key_prefix": a.api_key_prefix,
            "redirect_uris": a.redirect_uris,
            "disabled": a.disabled,
        }
        for a in apps
    ]


@router.post("/v1/apps/{app_id}/rotate-key", tags=["apps"])
def rotate_key(
    app_id: str, p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)
) -> dict:
    app = db.get(App, app_id)
    if app is None or app.owner_id != p.user.id:
        raise Problem(404, "App not found")
    key, app.api_key_prefix, app.api_key_hash = security.new_api_key()
    db.commit()
    return {"api_key": key}


# --- OAuth 2.0 -----------------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server", tags=["oauth"])
def oauth_metadata() -> dict:
    base = settings().public_base_url
    return {
        "issuer": settings().jwt_issuer,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": list(security.SCOPES),
    }


def _client(db: Session, client_id: str) -> App:
    app = db.scalar(select(App).where(App.client_id == client_id))
    if app is None or app.disabled:
        raise Problem(400, "invalid_client", "Unknown or disabled client_id.")
    return app


def _consent_page(app: App, params: dict, scopes: list[str], error: str = "") -> HTMLResponse:
    items = "".join(f"<li><b>{html.escape(s)}</b> — {html.escape(security.SCOPES[s])}</li>" for s in scopes)
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in params.items()
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to Cura</title><style>body{{font:16px system-ui;max-width:420px;margin:48px auto;padding:0 16px;color:#111}}
input{{display:block;width:100%;padding:10px;margin:6px 0 14px;box-sizing:border-box}}button{{padding:10px 16px}}.err{{color:#b00}}</style></head>
<body><h1>Cura</h1><p><b>{html.escape(app.name)}</b> wants to:</p><ul>{items}</ul>{err}
<form method="post" action="/oauth/authorize">{hidden}<label>Email<input name="email" type="email" required autocomplete="username"></label>
<label>Password<input name="password" type="password" required autocomplete="current-password"></label>
<button name="decision" value="allow">Allow</button> <button name="decision" value="deny" formnovalidate>Deny</button></form></body></html>"""
    return HTMLResponse(
        body,
        headers={
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
            "Cache-Control": "no-store",
        },
    )


def _validate_authorize(
    db: Session,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str,
) -> tuple[App, list[str]]:
    app = _client(db, client_id)
    if redirect_uri not in app.redirect_uris:
        raise Problem(400, "invalid_request", "redirect_uri is not registered for this client.")
    if response_type != "code":
        raise Problem(400, "unsupported_response_type", "Only response_type=code is supported.")
    if code_challenge_method != "S256" or not code_challenge:
        raise Problem(400, "invalid_request", "PKCE with code_challenge_method=S256 is required.")
    scopes = scope.split() or ["profile"]
    bad = [s for s in scopes if s not in app.allowed_scopes.split()]
    if bad:
        raise Problem(400, "invalid_scope", f"Not allowed for this client: {bad}")
    return app, scopes


@router.get("/oauth/authorize", response_class=HTMLResponse, tags=["oauth"])
def authorize_form(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    response_type: str = "code",
    scope: str = "profile",
    state: str = "",
    code_challenge_method: str = "S256",
    db: Session = Depends(get_session),
):
    app, scopes = _validate_authorize(
        db, client_id, redirect_uri, response_type, scope, code_challenge, code_challenge_method
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": response_type,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    return _consent_page(app, params, scopes)


@router.post("/oauth/authorize", tags=["oauth"])
def authorize_submit(
    client_id: str = Form(),
    redirect_uri: str = Form(),
    code_challenge: str = Form(),
    email: str = Form(""),
    password: str = Form(""),
    decision: str = Form("deny"),
    response_type: str = Form("code"),
    scope: str = Form("profile"),
    state: str = Form(""),
    code_challenge_method: str = Form("S256"),
    db: Session = Depends(get_session),
):
    app, scopes = _validate_authorize(
        db, client_id, redirect_uri, response_type, scope, code_challenge, code_challenge_method
    )
    sep = "&" if "?" in redirect_uri else "?"
    if decision != "allow":
        return RedirectResponse(
            f"{redirect_uri}{sep}{urlencode({'error': 'access_denied', 'state': state})}", status_code=303
        )
    try:
        user = _authenticate(db, email, password)
    except Problem:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }
        return _consent_page(app, params, scopes, "Email or password is incorrect.")
    scopes = [s for s in scopes if s in _user_scopes(user)]
    code = security.opaque_token()
    db.add(
        AuthCode(
            code_hash=security.sha256(code),
            client_id=client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            scope=" ".join(scopes),
            code_challenge=code_challenge,
            expires_at=utcnow() + timedelta(seconds=settings().auth_code_ttl_s),
        )
    )
    db.commit()
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}", status_code=303)


@router.post("/oauth/token", tags=["oauth"])
def token(
    grant_type: str = Form(),
    client_id: str = Form(),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
    client_secret: str = Form(""),
    db: Session = Depends(get_session),
) -> TokenOut:
    first_party = client_id == FIRST_PARTY and grant_type == "refresh_token"
    app = None if first_party else _client(db, client_id)
    if (
        app
        and app.client_secret_hash
        and not security.constant_eq(app.client_secret_hash, security.sha256(client_secret))
    ):
        raise Problem(401, "invalid_client", "Client authentication failed.")
    if grant_type == "authorization_code":
        row = db.get(AuthCode, security.sha256(code))
        if row is None or row.used or row.client_id != client_id or _aware(row.expires_at) < utcnow():
            raise Problem(400, "invalid_grant", "The code is invalid, expired or already used.")
        row.used = True  # single use even if the checks below fail
        db.commit()
        if row.redirect_uri != redirect_uri or not security.pkce_verify(code_verifier, row.code_challenge):
            raise Problem(400, "invalid_grant", "redirect_uri or code_verifier does not match.")
        user = db.get(User, row.user_id)
        return _issue(db, user, client_id, row.scope)
    if grant_type == "refresh_token":
        row = db.get(RefreshToken, security.sha256(refresh_token))
        if row is None or row.client_id != client_id:
            raise Problem(400, "invalid_grant", "Unknown refresh token.")
        if row.rotated or row.revoked:
            # Replay of a rotated token means it leaked: kill the whole family.
            for t in db.scalars(select(RefreshToken).where(RefreshToken.family_id == row.family_id)):
                t.revoked = True
            db.commit()
            raise Problem(400, "invalid_grant", "Refresh token reuse detected; the session has been revoked.")
        if _aware(row.expires_at) < utcnow():
            raise Problem(400, "invalid_grant", "Refresh token expired.")
        user = db.get(User, row.user_id)
        if user is None or user.disabled:
            raise Problem(400, "invalid_grant", "User disabled.")
        row.rotated = True
        return _issue(db, user, client_id, row.scope, family_id=row.family_id)
    raise Problem(400, "unsupported_grant_type", "Use authorization_code or refresh_token.")


@router.post("/v1/auth/refresh", tags=["auth"])
def first_party_refresh(refresh_token: Annotated[str, Query()], db: Session = Depends(get_session)) -> TokenOut:
    return token(grant_type="refresh_token", client_id=FIRST_PARTY, refresh_token=refresh_token, db=db)


@router.post("/oauth/revoke", status_code=200, tags=["oauth"])
def revoke(token: str = Form(), db: Session = Depends(get_session)) -> dict:
    """RFC 7009: always 200, whether or not the token existed."""
    row = db.get(RefreshToken, security.sha256(token))
    if row:
        for t in db.scalars(select(RefreshToken).where(RefreshToken.family_id == row.family_id)):
            t.revoked = True
        db.commit()
    return {}
