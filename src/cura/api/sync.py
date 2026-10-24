"""The change bus: every committed write that touches a facility is announced, once, after commit.

    write (any route) ─▶ Session flush ─▶ collected ─▶ commit ─▶ dispatch ─▶ SSE subscribers
                                                     rollback ─▶ dropped    └▶ webhook deliveries

Routes do not publish anything themselves. A SQLAlchemy listener on the session watches the rows that
make up the public state — facilities, practitioners, roles and availability events — so a write path
added later cannot forget to announce itself, and a write that rolls back is never announced.

An announcement is `{kind, id, version, summary, facility_id, at}`: enough to know what to re-read, never
the private parts of a record. Which facilities a change touches:

* a facility → itself; a role → its facility; an availability event → its facility, or (for a
  doctor-wide event) every facility where that doctor works;
* a practitioner → every facility where they work (their name, photo or bio shows on each roster).

Fan-out is in-process (one API worker, like the rate limiter). Several workers would put a broker
(Postgres LISTEN/NOTIFY or Redis) behind `ChangeBus.publish`; nothing else changes.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from cura.api.db import AvailabilityEvent, FacilityRow, PractitionerRow, RoleRow

log = logging.getLogger("cura.sync")
PENDING = "cura_sync_pending"


@dataclass(eq=False)
class Subscriber:
    facility_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    overflowed: bool = False


class ChangeBus:
    """Thread-safe fan-out from the (threadpool) write paths to (event-loop) SSE streams."""

    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._seq = count(1)

    def subscribe(self, facility_id: str, loop: asyncio.AbstractEventLoop) -> Subscriber:
        sub = Subscriber(facility_id, loop)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, message: dict) -> None:
        message = message | {"seq": next(self._seq)}
        with self._lock:
            targets = [s for s in self._subs if s.facility_id == message["facility_id"]]
        for sub in targets:
            try:
                sub.loop.call_soon_threadsafe(_offer, sub, message)
            except RuntimeError:  # the subscriber's loop has closed
                self.unsubscribe(sub)


def _offer(sub: Subscriber, message: dict) -> None:
    try:
        sub.queue.put_nowait(message)
    except asyncio.QueueFull:
        sub.overflowed = True  # the stream tells the client to resync from /v1/changes


bus = ChangeBus()


# --- collecting writes ---------------------------------------------------------------------------


def _collect(session: Session, _ctx) -> None:
    pending: list[dict] = session.info.setdefault(PENDING, [])
    for obj in list(session.new) + [o for o in session.dirty if session.is_modified(o)]:
        if isinstance(obj, FacilityRow):
            pending.append({"kind": "facility", "id": obj.id, "version": obj.version, "facility_id": obj.id})
        elif isinstance(obj, PractitionerRow):
            pending.append({"kind": "practitioner", "id": obj.id, "version": obj.version})
        elif isinstance(obj, RoleRow):
            pending.append(
                {
                    "kind": "role",
                    "id": obj.id,
                    "version": obj.version,
                    "facility_id": obj.facility_id,
                    "practitioner_id": obj.practitioner_id,
                    "ended": (obj.doc or {}).get("active") is False,
                }
            )
        elif isinstance(obj, AvailabilityEvent):
            pending.append(
                {
                    "kind": "availability",
                    "id": obj.id,
                    "version": None,
                    "facility_id": obj.facility_id,
                    "practitioner_id": obj.practitioner_id,
                    "state": obj.state,
                    "subject_type": obj.subject_type,
                }
            )


def _committed(session: Session) -> None:
    pending = session.info.pop(PENDING, None)
    if pending:
        dispatch(pending)


def _rolled_back(session: Session) -> None:
    session.info.pop(PENDING, None)


def install(session_factory) -> None:
    event.listen(session_factory, "after_flush", _collect)
    event.listen(session_factory, "after_commit", _committed)
    event.listen(session_factory, "after_soft_rollback", lambda s, _tx: _rolled_back(s))


# --- dispatching ---------------------------------------------------------------------------------

_queue: queue.Queue[list[dict] | None] = queue.Queue()
_worker: threading.Thread | None = None


def dispatch(pending: list[dict]) -> None:
    if _worker is not None and _worker.is_alive():
        _queue.put(pending)
    else:
        process(pending)


def _run() -> None:
    while (item := _queue.get()) is not None:
        try:
            process(item)
        except Exception:  # a failed announcement must never take the worker down
            log.exception("change dispatch failed")


def start_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="cura-sync", daemon=True)
        _worker.start()


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _queue.put(None)
        _worker.join(timeout=5)
        _worker = None


def _dedupe(pending: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for item in pending:
        seen[(item["kind"], item["id"])] = item  # the last flush carries the final version
    return list(seen.values())


def _name(doc: dict | None) -> str:
    if not doc:
        return "A record"
    name = doc.get("name") or {}
    return name.get("en") or name.get("ar") or doc.get("id", "")


def process(pending: list[dict]) -> None:
    """Resolve which facilities each change touches, then announce it to streams and webhooks."""
    from cura.api import db, webhooks

    items = _dedupe(pending)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    messages: list[dict] = []
    with db.new_session() as s:
        practitioner_ids = {
            i.get("practitioner_id") or (i["id"] if i["kind"] == "practitioner" else None) for i in items
        }
        practitioner_ids.discard(None)
        workplaces: dict[str, set[str]] = {}
        if practitioner_ids:
            for pid, fid in s.execute(
                select(RoleRow.practitioner_id, RoleRow.facility_id).where(
                    RoleRow.practitioner_id.in_(practitioner_ids)
                )
            ):
                workplaces.setdefault(pid, set()).add(fid)
        people = {
            r.id: r.doc for r in s.scalars(select(PractitionerRow).where(PractitionerRow.id.in_(practitioner_ids)))
        }
        for item in items:
            kind = item["kind"]
            if kind == "facility":
                targets = {item["id"]}
                row = s.get(FacilityRow, item["id"])
                summary = f"{_name(row.doc if row else None)} updated"
                event_type = "facility.updated"
            elif kind == "practitioner":
                targets = workplaces.get(item["id"], set())
                summary = f"{_name(people.get(item['id']))} updated their profile"
                event_type = "practitioner.updated"
            elif kind == "role":
                targets = {item["facility_id"]}
                who = _name(people.get(item["practitioner_id"]))
                summary = f"{who} left" if item["ended"] else f"{who}: schedule or details updated"
                event_type = "role.ended" if item["ended"] else "role.updated"
            else:
                targets = (
                    {item["facility_id"]} if item.get("facility_id") else workplaces.get(item["practitioner_id"], set())
                )
                if item.get("subject_type") == "facility":
                    summary = f"Facility is now {item['state']}"
                else:
                    summary = f"{_name(people.get(item.get('practitioner_id')))} is now {item['state']}"
                event_type = "availability.changed"
            for fid in sorted(targets):
                messages.append(
                    {
                        "type": event_type,
                        "kind": kind,
                        "id": item["id"],
                        "version": item.get("version"),
                        "summary": summary,
                        "facility_id": fid,
                        "practitioner_id": item.get("practitioner_id")
                        or (item["id"] if kind == "practitioner" else None),
                        "at": now,
                    }
                )
        if messages:
            webhooks.enqueue(s, messages)
            s.commit()
    for message in messages:
        bus.publish(message)
    if messages:
        webhooks.wake()
