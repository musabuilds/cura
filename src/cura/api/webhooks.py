"""Webhook delivery: signed POSTs to a facility's endpoints, retried with backoff, every attempt logged.

    Cura-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

Receivers recompute the HMAC over the raw body, compare in constant time, and reject a `t` more than
five minutes old (replay). Deliveries go through `netguard.fetch`, so a webhook URL can never reach a
private address even if its DNS changes after registration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import guard, netguard
from cura.api.db import Webhook, WebhookDelivery, utcnow

log = logging.getLogger("cura.webhooks")
EVENTS = ("practitioner.updated", "role.updated", "role.ended", "availability.changed", "facility.updated")
BACKOFF_S = (0, 10, 60, 300, 1800, 7200)  # attempt n waits BACKOFF_S[n]; after the last, the delivery fails
MAX_ATTEMPTS = len(BACKOFF_S)


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(secret: str, body: bytes, header: str, *, tolerance_s: int = 300, now: int | None = None) -> bool:
    """What a receiver runs. Shipped here so the docs and the tests use the same code."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((int(time.time()) if now is None else now) - t) > tolerance_s:
        return False
    expected = sign(secret, body, t).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def enqueue(db: Session, messages: list[dict]) -> int:
    """One delivery per (message, subscribed webhook of the message's facility)."""
    facility_ids = {m["facility_id"] for m in messages}
    hooks = db.scalars(
        select(Webhook).where(Webhook.facility_id.in_(facility_ids), Webhook.disabled_at.is_(None))
    ).all()
    n = 0
    for m in messages:
        for hook in hooks:
            if hook.facility_id != m["facility_id"] or m["type"] not in (hook.events or EVENTS):
                continue
            payload = {
                "type": m["type"],
                "created_at": m["at"],
                "facility_id": m["facility_id"],
                "data": {k: m[k] for k in ("kind", "id", "version", "summary", "practitioner_id")},
            }
            db.add(WebhookDelivery(webhook_id=hook.id, event=m["type"], payload=payload, next_attempt_at=utcnow()))
            n += 1
    return n


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def deliver(db: Session, delivery: WebhookDelivery, hook: Webhook) -> None:
    body = json.dumps(delivery.payload | {"id": delivery.id}, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "Cura-Signature": sign(guard.decrypt(hook.secret_enc), body),
        "Cura-Event": delivery.event,
        "Cura-Delivery": delivery.id,
        "User-Agent": "Cura-Webhooks/1.0",
    }
    delivery.attempts += 1
    try:
        result = netguard.fetch(hook.url, method="POST", content=body, headers=headers, max_bytes=1024)
        delivery.response_status = result.status
        ok = 200 <= result.status < 300
        delivery.error = None if ok else f"HTTP {result.status}"
    except netguard.Blocked as exc:
        ok, delivery.error = False, str(exc)[:300]
    if ok:
        delivery.status, delivery.delivered_at, delivery.next_attempt_at = "delivered", utcnow(), None
    elif delivery.attempts >= MAX_ATTEMPTS:
        delivery.status, delivery.next_attempt_at = "failed", None
    else:
        delivery.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF_S[delivery.attempts])


def deliver_due(db: Session, *, limit: int = 50) -> int:
    now = utcnow()
    due = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
    ).all()
    for delivery in due:
        hook = db.get(Webhook, delivery.webhook_id)
        if hook is None or hook.disabled_at is not None:
            delivery.status, delivery.error, delivery.next_attempt_at = "failed", "webhook deleted", None
            continue
        try:
            deliver(db, delivery, hook)
        except Exception:  # one broken endpoint must not stop the others
            log.exception("webhook delivery %s crashed", delivery.id)
            delivery.error, delivery.next_attempt_at = "internal error", utcnow() + timedelta(seconds=60)
        db.commit()
    return len(due)


# --- background worker ---------------------------------------------------------------------------

_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None


def wake() -> None:
    _wake.set()


def _run() -> None:
    from cura.api import db as dbm

    while not _stop.is_set():
        try:
            with dbm.new_session() as s:
                while deliver_due(s):
                    pass
        except Exception:
            log.exception("webhook worker pass failed")
        _wake.wait(timeout=5)
        _wake.clear()


def start_worker() -> None:
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, name="cura-webhooks", daemon=True)
        _thread.start()


def stop_worker() -> None:
    global _thread
    _stop.set()
    _wake.set()
    if _thread is not None:
        _thread.join(timeout=10)
        _thread = None
