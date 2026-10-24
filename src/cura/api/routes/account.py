"""Phone-first sign-in for Cura's own app (`cura-mobile`), sessions, and the signed-in account.

    POST /v1/auth/phone/start    a six-digit code by SMS          (always 200 for a well-formed mobile)
    POST /v1/auth/phone/verify   code → access + refresh tokens   (422 wrong, 410 expired / used up)
    POST /v1/auth/refresh        rotate the refresh token         (reuse revokes the whole session)
    POST /v1/auth/logout         revoke the session               (always 204)
    GET  /v1/me · PATCH /v1/me   the account, what it owns, what it is waiting for
    GET  /v1/me/roles            every workplace of the caller's doctor profile

A verified mobile number is both the login and evidence: the registry already lists phone numbers for
many doctors and facilities (see `cura.api.verification`).

Defences: codes are random, six digits, HMAC-hashed with a server key, valid five minutes, five
attempts, one live code per number (a new one voids the old), compared in constant time. Starting is
limited per number (3 / 10 min, 30 s apart) and per IP; verifying per IP. The response to `start`
does not depend on whether an account exists. Demo numbers accept a fixed code, except in production,
where they are switched off whatever the configuration says.
"""

from __future__ import annotations

import secrets
import time
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cura.api import guard, live, messaging, security, store, verification
from cura.api.db import Claim, OtpChallenge, RefreshToken, RoleRow, User, get_session, utcnow
from cura.api.deps import Principal, owned_subjects, require_user
from cura.api.errors import Problem
from cura.api.routes.auth import USER_SCOPES, issue_tokens, revoke_family, user_scopes
from cura.api.settings import settings

router = APIRouter(prefix="/v1")
MOBILE_CLIENT = "cura-mobile"
FIRST_PARTY_CLIENTS = {"cura-first-party", MOBILE_CLIENT}
OTP_TTL = timedelta(minutes=5)
OTP_ATTEMPTS = 5
RESEND_S = 30


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhoneStartIn(Body):
    phone: Annotated[str, Field(min_length=6, max_length=32, examples=["0795550101"])]


class PhoneVerifyIn(Body):
    challenge_id: Annotated[str, Field(min_length=4, max_length=64)]
    code: Annotated[str, Field(pattern=r"^\d{6}$")]
    device_name: Annotated[str | None, Field(max_length=120)] = None


class RefreshIn(Body):
    refresh_token: Annotated[str, Field(min_length=10, max_length=200)]


class MePatch(Body):
    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    email: EmailStr | None = None


# --- helpers -------------------------------------------------------------------------------------


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def parse_phone(raw: str) -> str:
    e164 = guard.normalise_phone(raw)
    if not e164 or not guard.is_mobile(e164):
        raise Problem(
            422,
            "Invalid phone number",
            "Enter a mobile number, e.g. 0795550101 or +962795550101.",
            errors=[{"field": "phone", "message": "not a valid mobile number"}],
        )
    return e164


def public_email(user: User) -> str | None:
    if user.contact_email:
        return user.contact_email
    return None if user.email.startswith("phone:") else user.email


def user_out(db: Session, user: User) -> dict:
    owned = owned_subjects(db, user)
    claims = db.scalars(select(Claim).where(Claim.user_id == user.id).order_by(Claim.created_at)).all()
    practitioner = None
    if owned["practitioner"]:
        pid = sorted(owned["practitioner"])[0]
        practitioner = verification.practitioner_summary(db, (store.get(db, "practitioner", pid) or _Nil).doc)
    facility_claims = [c for c in claims if c.subject_type == "facility" and c.status == "approved"]
    docs = {d["id"]: d for d in store.docs(db, "facility", {c.subject_id for c in facility_claims})}
    summaries = {s["id"]: s for s in verification.facility_summaries(db, list(docs.values()))}
    facilities, seen = [], set()
    for c in facility_claims:
        if c.subject_id in seen or c.subject_id not in summaries:
            continue
        seen.add(c.subject_id)
        s = summaries[c.subject_id]
        facilities.append(
            {"facility": s, "role": "owner" if c.method == "created" else "admin", "verification": s["verification"]}
        )
    return {
        "id": user.id,
        "phone": user.phone,
        "name": user.display_name,
        "email": public_email(user),
        "practitioner": practitioner,
        "facilities": facilities,
        "pending_claims": [verification.claim_out(c) for c in claims if c.status == "pending"],
        # Account details kept for the developer dashboard and older clients.
        "display_name": user.display_name,
        "role": user.role,
        "owns": {k: sorted(v) for k, v in owned.items()},
        "claims": [
            {"id": c.id, "subject_type": c.subject_type, "subject_id": c.subject_id, "status": c.status} for c in claims
        ],
    }


class _Nil:
    doc = None


# --- phone sign-in -------------------------------------------------------------------------------


@router.post("/auth/phone/start", tags=["auth"])
def phone_start(body: PhoneStartIn, request: Request, db: Session = Depends(get_session)) -> dict:
    """Send a sign-in code. The answer is the same whether or not the number has an account."""
    ip = guard.client_ip(request)
    guard.throttle(f"otp:start:ip:{ip}", 20, 600, "sign-in attempts from this network")
    phone = parse_phone(body.phone)
    demo = phone in settings().demo_numbers
    key = f"otp:start:phone:{phone}"
    if not demo:
        last = guard.windows.last(key)
        if last is not None and time.monotonic() - last < RESEND_S:
            wait = int(RESEND_S - (time.monotonic() - last)) + 1
            raise Problem(
                429,
                "Too many requests",
                f"Wait {wait} s before asking for another code.",
                headers={"Retry-After": str(wait)},
                retry_after=wait,
            )
        guard.throttle(key, 3, 600, "codes for this number")
    now = utcnow()
    # One live code per number: asking again voids the previous one.
    db.execute(
        update(OtpChallenge)
        .where(OtpChallenge.purpose == "login", OtpChallenge.target == phone, OtpChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    challenge_id = "pc_" + secrets.token_urlsafe(18)
    code = settings().demo_otp if demo else guard.new_code()
    db.add(
        OtpChallenge(
            id=challenge_id,
            purpose="login",
            target=phone,
            channel="sms",
            code_hash=guard.code_hash(challenge_id, code),
            max_attempts=OTP_ATTEMPTS,
            expires_at=now + OTP_TTL,
            ip=ip,
        )
    )
    guard.audit(db, "auth.otp.sent", request=request, phone=guard.mask_phone(phone), demo=demo or None)
    db.commit()
    if not demo:
        messaging.sms().send_sms(
            phone, f"Your Cura code is {code}. It expires in 5 minutes. Never share it.", code=code
        )
    return {
        "challenge_id": challenge_id,
        "phone": guard.pretty_phone(phone),
        "expires_in": int(OTP_TTL.total_seconds()),
        "resend_in": RESEND_S,
        "channel": "sms",
    }


def check_code(db: Session, ch: OtpChallenge | None, code: str, request: Request, purpose: str) -> OtpChallenge:
    """Shared by sign-in and facility codes: expiry, attempts, constant-time compare, lockout."""
    now = utcnow()
    if ch is None or ch.purpose != purpose:
        raise Problem(410, "Code expired", "Request a new code.")
    if ch.consumed_at is not None or _aware(ch.expires_at) <= now or ch.attempts >= ch.max_attempts:
        raise Problem(410, "Code expired", "This code has expired or was used up. Request a new one.")
    if not guard.code_matches(ch.id, code, ch.code_hash):
        ch.attempts += 1
        left = ch.max_attempts - ch.attempts
        if left <= 0:
            ch.consumed_at = now
            guard.audit(db, "auth.otp.locked", request=request, subject_id=ch.id, target=_masked_target(ch))
        else:
            guard.audit(db, "auth.otp.failed", request=request, subject_id=ch.id, attempts_left=left)
        db.commit()
        raise Problem(
            422,
            "Wrong code",
            f"{left} attempt{'s' if left != 1 else ''} left." if left else "No attempts left. Request a new code.",
            attempts_left=left,
            errors=[{"field": "code", "message": "incorrect"}],
        )
    ch.consumed_at = now
    return ch


def _masked_target(ch: OtpChallenge) -> str:
    return (
        guard.mask_email(ch.target)
        if "@" in ch.target
        else guard.mask_phone(ch.target)
        if ch.target.startswith("+")
        else ch.target
    )


@router.post("/auth/phone/verify", tags=["auth"])
def phone_verify(body: PhoneVerifyIn, request: Request, db: Session = Depends(get_session)) -> dict:
    guard.throttle(f"otp:verify:ip:{guard.client_ip(request)}", 30, 600, "code attempts from this network")
    ch = check_code(db, db.get(OtpChallenge, body.challenge_id), body.code, request, "login")
    phone = ch.target
    user = db.scalar(select(User).where(User.phone == phone))
    is_new = user is None
    if user is None:
        user = User(email=f"phone:{phone}", password_hash="!", phone=phone, phone_verified_at=utcnow())
        db.add(user)
        db.flush()
    elif user.disabled:
        db.commit()
        raise Problem(403, "Account disabled", "Contact Cura support.")
    else:
        user.phone_verified_at = utcnow()
    guard.audit(db, "auth.phone.signin", actor_id=user.id, request=request, new_account=is_new or None)
    tokens = issue_tokens(db, user, MOBILE_CLIENT, " ".join(user_scopes(user)), device_name=body.device_name)
    return tokens | {"user": user_out(db, user), "is_new": is_new}


# --- sessions ------------------------------------------------------------------------------------


@router.post("/auth/refresh", tags=["auth"])
def refresh(
    request: Request,
    body: RefreshIn | None = None,
    refresh_token: str | None = None,
    db: Session = Depends(get_session),
) -> dict:
    """Rotate a first-party refresh token (JSON body; the query parameter is kept for older clients).
    Presenting a token that was already rotated revokes every token of that session."""
    guard.throttle(f"auth:refresh:ip:{guard.client_ip(request)}", 60, 60, "refreshes")
    token = body.refresh_token if body else refresh_token
    if not token:
        raise Problem(422, "refresh_token required")
    row = db.get(RefreshToken, security.sha256(token))
    if row is None or row.client_id not in FIRST_PARTY_CLIENTS:
        raise Problem(401, "Invalid refresh token", "Sign in again.")
    if row.rotated or row.revoked:
        revoke_family(db, row.family_id)
        guard.audit(db, "auth.refresh.reuse", actor_id=row.user_id, request=request, family=row.family_id[:8])
        db.commit()
        raise Problem(401, "Session revoked", "This refresh token was already used; the session has been signed out.")
    if _aware(row.expires_at) < utcnow():
        raise Problem(401, "Session expired", "Sign in again.")
    user = db.get(User, row.user_id)
    if user is None or user.disabled:
        raise Problem(401, "Invalid refresh token", "Sign in again.")
    row.rotated = True
    return issue_tokens(db, user, row.client_id, row.scope, family_id=row.family_id, device_name=row.device_name)


@router.post("/auth/logout", status_code=204, tags=["auth"])
def logout(body: RefreshIn, request: Request, db: Session = Depends(get_session)) -> Response:
    row = db.get(RefreshToken, security.sha256(body.refresh_token))
    if row is not None:
        revoke_family(db, row.family_id)
        guard.audit(db, "auth.logout", actor_id=row.user_id, request=request)
        db.commit()
    return Response(status_code=204)


# --- the account ---------------------------------------------------------------------------------


@router.get("/me", tags=["auth"])
def me(p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)) -> dict:
    return user_out(db, p.user) | {"scopes": sorted(p.scopes), "client_id": p.client_id}


@router.patch("/me", tags=["auth"])
def patch_me(
    body: MePatch, request: Request, p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)
) -> dict:
    """Account fields only (name, email). Private: never published, never a login."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise Problem(422, "Empty patch", "Send name and/or email.")
    user = db.get(User, p.user.id)
    if "name" in fields:
        user.display_name = fields["name"].strip() if fields["name"] else None
    if "email" in fields:
        user.contact_email = str(fields["email"]).lower() if fields["email"] else None
    guard.audit(db, "account.updated", actor_id=user.id, request=request, fields=sorted(fields))
    db.commit()
    return user_out(db, user)


@router.get("/me/roles", tags=["roster"])
def my_roles(p: Principal = Depends(require_user("profile")), db: Session = Depends(get_session)) -> dict:
    """Every workplace of the caller's doctor profile, including pending, declined and ended ones."""
    owned = sorted(owned_subjects(db, p.user)["practitioner"])
    if not owned:
        return {"data": [], "practitioner_id": None}
    rows = db.scalars(select(RoleRow).where(RoleRow.practitioner_id == owned[0]).order_by(RoleRow.id)).all()
    docs = [r.doc for r in rows]
    states = live.role_availability(db, docs, live.now())
    facilities = {
        s["id"]: s
        for s in verification.facility_summaries(db, store.docs(db, "facility", {d["facility_id"] for d in docs}))
    }
    return {
        "practitioner_id": owned[0],
        "data": [
            {
                "role": r.doc | {"availability": states[r.id], "version": r.version},
                "facility": facilities.get(r.facility_id),
            }
            for r in rows
        ],
    }


__all__ = ["FIRST_PARTY_CLIENTS", "MOBILE_CLIENT", "USER_SCOPES", "check_code", "parse_phone", "router", "user_out"]
