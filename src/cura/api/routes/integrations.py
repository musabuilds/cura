"""Keeping hospital websites and apps current: webhooks, a live stream, facility API keys, the activity feed.

    POST   /v1/facilities/{id}/webhooks       register an https endpoint; the signing secret is shown once
    GET    /v1/facilities/{id}/webhooks       list (never the secret)
    DELETE /v1/webhooks/{id}                  stop deliveries
    GET    /v1/webhooks/{id}/deliveries       the delivery log: status, attempts, response code
    POST   /v1/facilities/{id}/api-keys       a read-only key for the facility's website (shown once)
    GET    /v1/facilities/{id}/api-keys       list by prefix
    POST   /v1/api-keys/{id}/revoke
    GET    /v1/stream?facility_id=            Server-Sent Events: `event: change` whenever it changes
    GET    /v1/facilities/{id}/activity       recent changes to the facility and its roster (admins)

Everything that manages a facility's integrations needs an approved claim on that facility (or a
moderator); an id belonging to someone else's facility answers 404, so ids cannot be probed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cura.api import guard, netguard, security, store, verification, webhooks
from cura.api.db import (
    AvailabilityEvent,
    Change,
    FacilityApiKey,
    RoleRow,
    User,
    Webhook,
    WebhookDelivery,
    get_session,
    utcnow,
)
from cura.api.deps import Principal, owned_subjects, principal, require_user
from cura.api.errors import Problem
from cura.api.settings import settings
from cura.api.sync import bus

router = APIRouter(prefix="/v1")
MAX_WEBHOOKS = 5
MAX_KEYS = 10


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: Annotated[str, Field(min_length=12, max_length=2048)]
    events: Annotated[list[str], Field(min_length=1, max_length=len(webhooks.EVENTS))] = list(webhooks.EVENTS)


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=80)]


def _manage(db: Session, p: Principal, facility_id: str) -> None:
    if store.get(db, "facility", facility_id) is None:
        raise Problem(404, "Facility not found", facility_id)
    if not p.is_moderator and facility_id not in owned_subjects(db, p.user)["facility"]:
        raise Problem(403, "Not your facility", "Only the facility's verified admin can do this.")


def _hook_out(h: Webhook) -> dict:
    return {
        "id": h.id,
        "facility_id": h.facility_id,
        "url": h.url,
        "events": h.events,
        "created_at": verification.aware(h.created_at),
        "active": h.disabled_at is None,
    }


def _own_hook(db: Session, p: Principal, webhook_id: str) -> Webhook:
    hook = db.get(Webhook, webhook_id)
    if hook is None or (not p.is_moderator and hook.facility_id not in owned_subjects(db, p.user)["facility"]):
        raise Problem(404, "Webhook not found")
    return hook


# --- webhooks ------------------------------------------------------------------------------------


@router.post("/facilities/{facility_id}/webhooks", status_code=201, tags=["sync"])
def create_webhook(
    facility_id: str,
    body: WebhookIn,
    request: Request,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Signed `POST`s to your endpoint when the facility, its doctors, their roles or availability change.
    The URL must be https on the default port and resolve to public addresses only."""
    _manage(db, p, facility_id)
    guard.throttle(f"webhooks:{p.user.id}", 10, 3600, "webhook registrations")
    unknown = sorted(set(body.events) - set(webhooks.EVENTS))
    if unknown:
        raise Problem(
            422,
            "Unknown events",
            f"Allowed: {list(webhooks.EVENTS)}",
            errors=[{"field": "events", "message": str(unknown)}],
        )
    try:
        host, _ = netguard.check_url(body.url)
        netguard.resolve_public(host)
    except netguard.Blocked as exc:
        raise Problem(422, "Webhook URL not allowed", str(exc), errors=[{"field": "url", "message": str(exc)}]) from exc
    active = db.scalars(select(Webhook).where(Webhook.facility_id == facility_id, Webhook.disabled_at.is_(None))).all()
    if len(active) >= MAX_WEBHOOKS:
        raise Problem(409, "Too many webhooks", f"A facility can have {MAX_WEBHOOKS}; delete one first.")
    secret = "whsec_" + security.opaque_token(32)
    hook = Webhook(
        facility_id=facility_id,
        url=body.url,
        events=sorted(set(body.events)),
        secret_enc=guard.encrypt(secret),
        created_by=p.user.id,
    )
    db.add(hook)
    db.flush()
    guard.audit(
        db,
        "webhook.created",
        actor_id=p.user.id,
        subject_type="facility",
        subject_id=facility_id,
        request=request,
        webhook=hook.id,
        host=host,
    )
    db.commit()
    return _hook_out(hook) | {"secret": secret}


@router.get("/facilities/{facility_id}/webhooks", tags=["sync"])
def list_webhooks(
    facility_id: str, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    _manage(db, p, facility_id)
    rows = db.scalars(select(Webhook).where(Webhook.facility_id == facility_id).order_by(Webhook.created_at)).all()
    return {"data": [_hook_out(h) for h in rows]}


@router.delete("/webhooks/{webhook_id}", status_code=204, tags=["sync"])
def delete_webhook(
    webhook_id: str,
    request: Request,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> Response:
    hook = _own_hook(db, p, webhook_id)
    if hook.disabled_at is None:
        hook.disabled_at = utcnow()
        guard.audit(
            db,
            "webhook.deleted",
            actor_id=p.user.id,
            subject_type="facility",
            subject_id=hook.facility_id,
            request=request,
            webhook=hook.id,
        )
        db.commit()
    return Response(status_code=204)


@router.get("/webhooks/{webhook_id}/deliveries", tags=["sync"])
def webhook_deliveries(
    webhook_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    hook = _own_hook(db, p, webhook_id)
    rows = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == hook.id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "data": [
            {
                "id": d.id,
                "event": d.event,
                "status": d.status,
                "attempts": d.attempts,
                "response_status": d.response_status,
                "error": d.error,
                "created_at": verification.aware(d.created_at),
                "delivered_at": verification.aware(d.delivered_at),
                "next_attempt_at": verification.aware(d.next_attempt_at),
                "payload": d.payload,
            }
            for d in rows
        ]
    }


# --- facility API keys ---------------------------------------------------------------------------


def _key_out(k: FacilityApiKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": f"curafk_{k.prefix}",
        "scope": k.scope,
        "created_at": verification.aware(k.created_at),
        "last_used_at": verification.aware(k.last_used_at),
        "revoked_at": verification.aware(k.revoked_at),
    }


@router.post("/facilities/{facility_id}/api-keys", status_code=201, tags=["sync"])
def create_api_key(
    facility_id: str,
    body: KeyIn,
    request: Request,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """A read-only key for the facility's website or systems: send it as `X-API-Key`. Shown once."""
    _manage(db, p, facility_id)
    live_keys = db.scalars(
        select(FacilityApiKey).where(FacilityApiKey.facility_id == facility_id, FacilityApiKey.revoked_at.is_(None))
    ).all()
    if len(live_keys) >= MAX_KEYS:
        raise Problem(409, "Too many keys", f"A facility can have {MAX_KEYS} active keys; revoke one first.")
    key, prefix, key_hash = security.new_facility_key()
    row = FacilityApiKey(
        facility_id=facility_id, name=body.name, prefix=prefix, key_hash=key_hash, created_by=p.user.id
    )
    db.add(row)
    db.flush()
    guard.audit(
        db,
        "api_key.created",
        actor_id=p.user.id,
        subject_type="facility",
        subject_id=facility_id,
        request=request,
        key=row.id,
    )
    db.commit()
    return _key_out(row) | {"key": key}


@router.get("/facilities/{facility_id}/api-keys", tags=["sync"])
def list_api_keys(
    facility_id: str, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    _manage(db, p, facility_id)
    rows = db.scalars(
        select(FacilityApiKey).where(FacilityApiKey.facility_id == facility_id).order_by(FacilityApiKey.created_at)
    ).all()
    return {"data": [_key_out(k) for k in rows]}


@router.post("/api-keys/{key_id}/revoke", tags=["sync"])
def revoke_api_key(
    key_id: str,
    request: Request,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    row = db.get(FacilityApiKey, key_id)
    if row is None or (not p.is_moderator and row.facility_id not in owned_subjects(db, p.user)["facility"]):
        raise Problem(404, "API key not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        guard.audit(
            db,
            "api_key.revoked",
            actor_id=p.user.id,
            subject_type="facility",
            subject_id=row.facility_id,
            request=request,
            key=row.id,
        )
        db.commit()
    return _key_out(row)


# --- live stream ---------------------------------------------------------------------------------

_connections: dict[str, int] = {}
_conn_lock = threading.Lock()


def _acquire(ip: str) -> bool:
    s = settings()
    with _conn_lock:
        if sum(_connections.values()) >= s.sse_max_connections or _connections.get(ip, 0) >= s.sse_max_per_ip:
            return False
        _connections[ip] = _connections.get(ip, 0) + 1
        return True


def _release(ip: str) -> None:
    with _conn_lock:
        _connections[ip] = max(0, _connections.get(ip, 1) - 1)
        if not _connections[ip]:
            _connections.pop(ip, None)


def _frame(event: str, data: dict, eid: int | None = None) -> str:
    head = f"id: {eid}\n" if eid is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def event_stream(facility_id: str, is_disconnected, *, heartbeat_s: float = 15.0, max_events: int | None = None):
    """The SSE body. `is_disconnected` is an async callable; `max_events` bounds it (tests)."""
    loop = asyncio.get_running_loop()
    sub = bus.subscribe(facility_id, loop)
    sent = 0
    try:
        yield "retry: 3000\n\n"
        yield _frame("hello", {"facility_id": facility_id, "at": utcnow().isoformat()})
        while True:
            if await is_disconnected():
                return
            try:
                message = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_s)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if sub.overflowed:
                sub.overflowed = False
                yield _frame("resync", {"detail": "events were dropped; re-read the facility"})
            yield _frame(
                "change",
                {k: message.get(k) for k in ("type", "kind", "id", "version", "summary", "practitioner_id", "at")},
                message.get("seq"),
            )
            sent += 1
            if max_events is not None and sent >= max_events:
                return
    finally:
        bus.unsubscribe(sub)


@router.get("/stream", tags=["sync"])
async def stream(
    request: Request,
    facility_id: Annotated[str, Query(max_length=20)],
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
):
    """Server-Sent Events for one facility: `event: change` with `{type, kind, id, version, summary}` when
    the facility, one of its doctors, a role or an availability touching it changes. Public, read-only;
    re-read the record (`GET /v1/facilities/{id}?expand=practitioners`) to get the new state."""
    if store.get(db, "facility", facility_id) is None:
        raise Problem(404, "Facility not found", facility_id)
    db.close()  # a stream can stay open for hours; it must not hold a database connection
    ip = guard.client_ip(request)
    if not _acquire(ip):
        raise Problem(429, "Too many streams", "Close another stream first.", headers={"Retry-After": "30"})

    async def body():
        try:
            async for chunk in event_stream(facility_id, request.is_disconnected):
                yield chunk
        finally:
            _release(ip)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --- activity ------------------------------------------------------------------------------------


@router.get("/facilities/{facility_id}/activity", tags=["sync"])
def activity(
    facility_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    p: Principal = Depends(require_user("profile")),
    db: Session = Depends(get_session),
) -> dict:
    """Recent changes to the facility and its roster: who changed what, and when (newest first)."""
    _manage(db, p, facility_id)
    roles = db.scalars(select(RoleRow).where(RoleRow.facility_id == facility_id)).all()
    role_ids = [r.id for r in roles]
    practitioner_ids = sorted({r.practitioner_id for r in roles})
    names = {d["id"]: d["name"] for d in store.docs(db, "practitioner", practitioner_ids)}
    role_doctor = {r.id: r.practitioner_id for r in roles}
    subjects = [and_(Change.subject_type == "facility", Change.subject_id == facility_id)]
    if role_ids:
        subjects.append(and_(Change.subject_type == "role", Change.subject_id.in_(role_ids)))
    if practitioner_ids:
        subjects.append(and_(Change.subject_type == "practitioner", Change.subject_id.in_(practitioner_ids)))
    changes = db.scalars(
        select(Change).where(Change.status == "applied", or_(*subjects)).order_by(Change.applied_at.desc()).limit(limit)
    ).all()
    events = db.scalars(
        select(AvailabilityEvent)
        .where(
            or_(
                AvailabilityEvent.facility_id == facility_id,
                and_(
                    AvailabilityEvent.subject_type == "practitioner",
                    AvailabilityEvent.practitioner_id.in_(practitioner_ids or [""]),
                ),
            )
        )
        .order_by(AvailabilityEvent.updated_at.desc())
        .limit(limit)
    ).all()
    authors = {c.author_id for c in changes} | {e.author_id for e in events}
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_({a for a in authors if a})))}

    def actor(uid: str | None) -> dict | None:
        if not uid or uid not in users:
            return None
        return {"name": users[uid].display_name or "Cura user", "is_you": uid == p.user.id}

    items = []
    for c in changes:
        pid = c.subject_id if c.subject_type == "practitioner" else role_doctor.get(c.subject_id or "")
        fields = sorted(k for k in (c.patch or {}) if not k.startswith("_"))
        items.append(
            {
                "at": verification.aware(c.applied_at or c.created_at),
                "kind": c.subject_type,
                "subject_id": c.subject_id,
                "op": c.op,
                "fields": fields,
                "practitioner": {"id": pid, "name": names.get(pid)} if pid else None,
                "actor": actor(c.author_id),
                "summary": _summary(c, names.get(pid) if pid else None, fields),
            }
        )
    for e in events:
        who = names.get(e.practitioner_id) if e.practitioner_id else None
        items.append(
            {
                "at": verification.aware(e.updated_at),
                "kind": "availability",
                "subject_id": e.subject_id,
                "op": "cancel" if e.cancelled_at else "set",
                "fields": ["state"],
                "practitioner": {"id": e.practitioner_id, "name": who} if e.practitioner_id else None,
                "actor": actor(e.author_id),
                "summary": (
                    f"{(who or {}).get('en') or 'Facility'}: {e.state}" + (f" ({e.reason})" if e.reason else "")
                )
                + (" — cancelled" if e.cancelled_at else ""),
            }
        )
    items.sort(key=lambda i: i["at"], reverse=True)
    return {"data": items[:limit]}


def _summary(c: Change, name: dict | None, fields: list[str]) -> str:
    who = (name or {}).get("en") or (name or {}).get("ar")
    if c.subject_type == "facility":
        return f"Facility {'created' if c.op == 'create' else 'updated'}: {', '.join(fields) or 'details'}"
    if c.subject_type == "role":
        if c.op == "create":
            return f"{who or 'A doctor'} joined the roster"
        if (c.patch or {}).get("active") is False:
            return f"{who or 'A doctor'} left"
        if "confirmation" in fields:
            return f"{who or 'A doctor'}: workplace {(c.patch or {}).get('confirmation')}"
        return f"{who or 'A doctor'} updated {', '.join(fields)}"
    return f"{who or 'A doctor'} updated their profile ({', '.join(fields)})"
