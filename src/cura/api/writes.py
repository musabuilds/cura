"""The one way registry records change: a `Change` row, a validated document, a new version.

Every route that writes — edits, proposals, roster changes, merges — goes through `record_change` and
`apply_update` / `apply_create`, so attribution, validation and history are never skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura import normalize as nz
from cura.api import policy, store
from cura.api.db import Change, Claim, utcnow
from cura.api.deps import Principal, owned_subjects
from cura.api.errors import Problem

PLACEHOLDER_ID = {"facility": "JO-XX-OTH-99999", "practitioner": "JO-PRC-999999", "role": "JO-ROL-9999999"}


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def invalid(exc: ValidationError, title: str = "Invalid record") -> Problem:
    return Problem(
        422,
        title,
        str(exc.errors()[0]["msg"]),
        errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
    )


def owns(db: Session, p: Principal, kind: str, doc: dict) -> bool:
    if "practitioner:manage" not in p.scopes:
        return False
    owned = owned_subjects(db, p.user)
    if kind == "role":
        return doc["practitioner_id"] in owned["practitioner"] or doc["facility_id"] in owned["facility"]
    return doc["id"] in owned[kind]


def unverified_owner(db: Session, p: Principal, kind: str, rid: str) -> bool:
    """The caller owns the record only through having created it (`created`, nothing proven yet).

    Such an owner manages their own record, but cannot put their side of a role on someone else's
    public roster on their own say-so: the other side, or a verification, has to come first."""
    if p.is_moderator or p.user is None:
        return False
    levels = {
        c.level or ("unverified" if c.method == "created" else "reviewed")
        for c in db.scalars(
            select(Claim).where(
                Claim.user_id == p.user.id,
                Claim.subject_type == kind,
                Claim.subject_id == rid,
                Claim.status == "approved",
            )
        )
    }
    return bool(levels) and levels <= {"unverified"}


def has_owner(db: Session, kind: str, rid: str) -> bool:
    return (
        db.scalar(
            select(Claim.id).where(Claim.subject_type == kind, Claim.subject_id == rid, Claim.status == "approved")
        )
        is not None
    )


def record_change(
    db: Session,
    p: Principal | None,
    kind: str,
    rid: str | None,
    op: str,
    patch: dict,
    *,
    reason: str | None = None,
    evidence_url: str | None = None,
    base_version: int | None = None,
) -> Change:
    change = Change(
        subject_type=kind,
        subject_id=rid,
        op=op,
        patch=patch,
        reason=reason,
        evidence_url=evidence_url,
        author_id=p.user.id if p and p.user else None,
        client_id=p.client_id if p else None,
        base_version=base_version,
    )
    db.add(change)
    db.flush()  # the id links the change to the version it produces
    return change


def mark_applied(change: Change, *, auto: bool = True, reviewer: str | None = None) -> None:
    change.status, change.auto_applied, change.applied_at = "applied", auto, utcnow()
    if reviewer:
        change.reviewed_by = reviewer


def derive_hours(doc: dict, patch: dict) -> dict:
    """`opening_hours` is what people write; `weekly` is what availability is computed from. When a
    write sets the first without the second, derive it, so a schedule edit never leaves stale spans."""
    hours = patch.get("hours")
    if isinstance(hours, dict) and hours.get("opening_hours") and "weekly" not in hours:
        parsed = nz.parse_opening_hours(hours["opening_hours"])
        doc = doc | {
            "hours": (doc.get("hours") or {})
            | {"weekly": parsed.get("weekly"), "always_open": parsed.get("always_open")}
        }
    return doc


def apply_update(db: Session, kind: str, row, patch: dict, change_id: int | None = None) -> dict:
    new = derive_hours(policy.merge_patch(row.doc, patch), patch)
    new["updated_at"] = now_iso()
    try:
        return store.put(db, kind, new, change_id=change_id).doc
    except ValidationError as exc:
        raise invalid(exc, "Patch produces an invalid record") from exc


def check_update(kind: str, doc: dict, patch: dict) -> None:
    """Validate a patch without applying it, so reviewers never see one that cannot apply."""
    try:
        store.validate(kind, derive_hours(policy.merge_patch(doc, patch), patch) | {"updated_at": now_iso()})
    except ValidationError as exc:
        raise invalid(exc, "Patch produces an invalid record") from exc


def blank(kind: str, record: dict, rid: str) -> dict:
    now = now_iso()
    base: dict[str, Any] = {"id": rid, "updated_at": now}
    if kind in ("facility", "practitioner"):
        base |= {
            "created_at": now,
            "sources": [{"source": "cura_api", "ref": rid, "retrieved_at": now}],
            "quality": {"completeness": 0, "confidence": 0.45, "last_verified_at": now},
            "identifiers": {"cura_api": rid},
        }
    else:
        base |= {"sources": [{"source": "cura_api", "ref": rid}]}
    record = derive_hours(record, record)
    return {k: v for k, v in record.items() if k not in policy.SYSTEM - {"practitioner_id", "facility_id"}} | base


def check_create(kind: str, record: dict) -> None:
    try:
        store.validate(kind, blank(kind, record, PLACEHOLDER_ID[kind]))
    except ValidationError as exc:
        raise invalid(exc) from exc


def apply_create(
    db: Session, kind: str, record: dict, change_id: int | None = None, *, system: dict | None = None
) -> dict:
    """Create a record in the API-reserved ID range. `system` sets fields callers may not send."""
    if kind == "facility":
        gov = (record.get("address") or {}).get("governorate") or "JO-XX"
        from cura.pipeline.merge import TYPE_SEGMENT

        prefix = f"JO-{gov[3:]}-{TYPE_SEGMENT.get(record.get('type', 'other'), 'OTH')}"
    else:
        prefix = {"practitioner": "JO-PRC", "role": "JO-ROL"}[kind]
    rid = store.next_reserved_id(db, kind, prefix)
    try:
        return store.put(db, kind, blank(kind, record, rid) | (system or {}), change_id=change_id).doc
    except ValidationError as exc:
        raise invalid(exc) from exc


def change_out(c: Change) -> dict:
    return {
        "id": c.id,
        "subject_type": c.subject_type,
        "subject_id": c.subject_id,
        "op": c.op,
        "patch": c.patch,
        "status": c.status,
        "auto_applied": c.auto_applied,
        "reason": c.reason,
        "evidence_url": c.evidence_url,
        "created_at": c.created_at,
        "applied_at": c.applied_at,
        "review_note": c.review_note,
    }
