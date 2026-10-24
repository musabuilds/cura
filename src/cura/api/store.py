"""Registry storage: document ↔ row mapping, search and geo queries."""

from __future__ import annotations

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cura import availability, model
from cura import normalize as nz
from cura.api.db import FacilityRow, PractitionerRow, RecordVersion, RoleRow, utcnow
from cura.geo import haversine_m

AMMAN = ZoneInfo("Asia/Amman")
KINDS = {
    "facility": (FacilityRow, model.Facility),
    "practitioner": (PractitionerRow, model.Practitioner),
    "role": (RoleRow, model.Role),
}


def _search_text(doc: dict) -> str:
    names = [doc["name"].get("en"), doc["name"].get("ar"), *doc["name"].get("aliases", [])]
    keys = {nz.match_key(n, drop=set()) for n in names if n}
    return f" {doc['id'].lower()} " + " ".join(sorted(keys)) + " "


def _tags(doc: dict) -> str:
    tags = [f"spec:{s}" for s in doc.get("specialties") or []]
    tags += [f"ins:{i['insurer']}" for i in doc.get("insurance") or []]
    if doc.get("emergency"):
        tags.append("emergency")
    return " " + " ".join(tags) + " "


def row_values(kind: str, doc: dict) -> dict:
    if kind == "facility":
        loc = doc.get("location") or {}
        return {
            "type": doc["type"],
            "status": doc["status"],
            "ownership": doc["ownership"],
            "governorate": doc["address"].get("governorate"),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "search": _search_text(doc) + "|" + _tags(doc),
        }
    if kind == "practitioner":
        return {"status": doc["status"], "specialty": doc.get("specialty"), "search": _search_text(doc)}
    return {"practitioner_id": doc["practitioner_id"], "facility_id": doc["facility_id"]}


def validate(kind: str, doc: dict) -> dict:
    return KINDS[kind][1].model_validate(doc).model_dump(mode="json")


def put(
    db: Session,
    kind: str,
    doc: dict,
    *,
    bump: bool = True,
    dirty: bool = True,
    change_id: int | None = None,
    origin: str = "api",
):
    """Insert or replace a document. Validates against the canonical model first.

    Every new version is also appended to `record_versions` (see `RecordVersion`): the row holds the
    present, the history table holds every past.
    """
    row_cls, _ = KINDS[kind]
    doc = validate(kind, doc)
    row = db.get(row_cls, doc["id"])
    values = row_values(kind, doc)
    now = utcnow()
    if row is None:
        row = row_cls(id=doc["id"], doc=doc, version=1, dirty=dirty, updated_at=now, **values)
        db.add(row)
        db.add(_version(kind, row, now, change_id, origin))
        return row
    if row.doc == doc:
        return row
    current = _open_version(db, kind, row.id)
    if bump:
        if current is None:  # a record that predates history: keep the version being replaced
            current = _version(kind, row, row.updated_at, None, "seed")
            db.add(current)
        current.valid_to = now
        row.version += 1
        row.updated_at = now
    row.doc = doc
    for k, v in values.items():
        setattr(row, k, v)
    row.dirty = row.dirty or dirty
    if bump:
        db.add(_version(kind, row, now, change_id, origin))
    elif current is not None:
        current.doc = doc  # an in-place correction of the same version
    return row


def _version(kind: str, row, valid_from: datetime, change_id: int | None, origin: str) -> RecordVersion:
    return RecordVersion(
        kind=kind,
        record_id=row.id,
        version=row.version,
        doc=row.doc,
        valid_from=valid_from,
        change_id=change_id,
        origin=origin,
    )


def _open_version(db: Session, kind: str, rid: str) -> RecordVersion | None:
    db.flush()  # versions added earlier in this transaction must be visible
    return db.scalar(
        select(RecordVersion).where(
            RecordVersion.kind == kind, RecordVersion.record_id == rid, RecordVersion.valid_to.is_(None)
        )
    )


def history(db: Session, kind: str, rid: str, *, limit: int = 50, before: int | None = None) -> list[RecordVersion]:
    stmt = select(RecordVersion).where(RecordVersion.kind == kind, RecordVersion.record_id == rid)
    if before is not None:
        stmt = stmt.where(RecordVersion.version < before)
    return list(db.scalars(stmt.order_by(RecordVersion.version.desc()).limit(limit)))


def as_of(db: Session, kind: str, rid: str, at: datetime) -> RecordVersion | None:
    """The version that was current at `at`."""
    return db.scalar(
        select(RecordVersion)
        .where(
            RecordVersion.kind == kind,
            RecordVersion.record_id == rid,
            RecordVersion.valid_from <= at,
            or_(RecordVersion.valid_to.is_(None), RecordVersion.valid_to > at),
        )
        .order_by(RecordVersion.version.desc())
        .limit(1)
    )


def diff_paths(old: dict | None, new: dict, prefix: str = "") -> list[str]:
    """Dotted paths whose value differs between two documents (objects recurse; lists compare whole)."""
    old = old or {}
    out = []
    for key in sorted(set(old) | set(new)):
        if key == "updated_at":
            continue
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        path = f"{prefix}{key}"
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff_paths(a, b, f"{path}.")
        else:
            out.append(path)
    return out


def get(db: Session, kind: str, rid: str):
    return db.get(KINDS[kind][0], rid)


WIDTH = {"facility": 5, "practitioner": 6, "role": 7}
# The last 100 numbers of each reserved range hold the demo records (`cura demo seed`), so a reset can
# find them by id alone and API-created records never collide with them.
DEMO_FLOOR = {kind: 10**w - 100 for kind, w in WIDTH.items()}


def is_demo_id(kind: str, rid: str) -> bool:
    try:
        return int(rid.rsplit("-", 1)[1]) >= DEMO_FLOOR[kind]
    except (ValueError, IndexError):
        return False


def next_reserved_id(db: Session, kind: str, prefix: str) -> str:
    """IDs for API-created records come from the reserved top range (see pipeline IdAllocator)."""
    row_cls, _ = KINDS[kind]
    width = WIDTH[kind]
    floor = 9 * 10 ** (width - 1)
    ids = db.scalars(select(row_cls.id).where(row_cls.id.like(f"{prefix}-%"))).all()
    nums = [n for n in (int(i.rsplit("-", 1)[1]) for i in ids) if floor <= n < DEMO_FLOOR[kind]]
    return f"{prefix}-{(max(nums) + 1) if nums else floor:0{width}d}"


# --- queries -------------------------------------------------------------------------------------


def _text_filter(column, q: str | None):
    key = nz.match_key(q, drop=set()) if q else ""
    return [column.like(f"%{tok}%") for tok in key.split()]


def search_facilities(
    db: Session,
    *,
    q=None,
    type=None,
    governorate=None,
    ownership=None,
    specialty=None,
    insurer=None,
    emergency=None,
    near: tuple[float, float] | None = None,
    radius_km: float = 5.0,
    open_now: bool = False,
    include_closed: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[dict], str | None, dict[str, float]]:
    stmt = select(FacilityRow)
    conds = _text_filter(FacilityRow.search, q)
    if type:
        conds.append(FacilityRow.type.in_(type.split(",")))
    if governorate:
        govs = [g if g.startswith("JO-") else f"JO-{g.upper()}" for g in governorate.split(",")]
        conds.append(FacilityRow.governorate.in_(govs))
    if ownership:
        conds.append(FacilityRow.ownership.in_(ownership.split(",")))
    if specialty:
        conds.append(or_(*[FacilityRow.search.like(f"% spec:{s} %") for s in specialty.split(",")]))
    if insurer:
        conds.append(or_(*[FacilityRow.search.like(f"% ins:{s} %") for s in insurer.split(",")]))
    if emergency:
        conds.append(FacilityRow.search.like("% emergency %"))
    if not include_closed:
        conds.append(FacilityRow.status.notin_(["merged", "permanently_closed"]))
    distances: dict[str, float] = {}
    if near:
        lat, lon = near
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(0.2, abs(math.cos(math.radians(lat)))))
        conds += [FacilityRow.lat.between(lat - dlat, lat + dlat), FacilityRow.lon.between(lon - dlon, lon + dlon)]
        rows = db.scalars(stmt.where(and_(*conds))).all()
        scored = sorted(
            ((haversine_m(lat, lon, r.lat, r.lon), r) for r in rows if r.lat is not None),
            key=lambda t: t[0],
        )
        scored = [(d, r) for d, r in scored if d <= radius_km * 1000]
        if open_now:
            opened = _open_ids(db, [r.doc for _, r in scored])
            scored = [(d, r) for d, r in scored if r.id in opened]
        offset = int(cursor or 0)
        page = scored[offset : offset + limit]
        distances = {r.id: round(d) for d, r in page}
        nxt = str(offset + limit) if offset + limit < len(scored) else None
        return [r.doc for _, r in page], nxt, distances
    if cursor:
        conds.append(FacilityRow.id > cursor)
    stmt = stmt.where(and_(*conds)).order_by(FacilityRow.id)
    if open_now:
        batch = db.scalars(stmt.limit(limit * 10)).all()
        opened = _open_ids(db, [r.doc for r in batch])
        rows = [r for r in batch if r.id in opened][:limit]
    else:
        rows = db.scalars(stmt.limit(limit + 1)).all()
    nxt = rows[limit - 1].id if len(rows) > limit else None
    return [r.doc for r in rows[:limit]], nxt, distances


def _open_ids(db: Session, docs: list[dict]) -> set[str]:
    from cura.api import live  # imported here: live depends on db only, store stays importable alone

    states = live.facility_live(db, docs, live.now())
    return {fid for fid, s in states.items() if s["state"] in ("open", "limited")}


def search_practitioners(
    db: Session, *, q=None, specialty=None, facility=None, cursor=None, limit=50
) -> tuple[list[dict], str | None]:
    conds = _text_filter(PractitionerRow.search, q)
    conds.append(PractitionerRow.status != "merged")
    if specialty:
        conds.append(PractitionerRow.specialty.in_(specialty.split(",")))
    stmt = select(PractitionerRow)
    if facility:
        stmt = stmt.join(RoleRow, RoleRow.practitioner_id == PractitionerRow.id).where(RoleRow.facility_id == facility)
    if cursor:
        conds.append(PractitionerRow.id > cursor)
    rows = db.scalars(stmt.where(and_(*conds)).order_by(PractitionerRow.id).limit(limit + 1)).all()
    nxt = rows[limit - 1].id if len(rows) > limit else None
    return [r.doc for r in rows[:limit]], nxt


def docs(db: Session, kind: str, ids) -> list[dict]:
    row_cls, _ = KINDS[kind]
    ids = list(ids)
    return [r.doc for r in db.scalars(select(row_cls).where(row_cls.id.in_(ids)))] if ids else []


def summary(doc: dict | None) -> dict | None:
    if not doc:
        return None
    return {
        "id": doc["id"],
        "type": doc["type"],
        "name": doc["name"],
        "address": doc["address"],
        "location": doc["location"],
    }


def is_listed(role: dict) -> bool:
    """Shown on a facility's public roster: confirmed by both sides and not ended."""
    return role.get("active") is not False and role.get("confirmation", "confirmed") == "confirmed"


def roles_for(db: Session, *, practitioner_id: str | None = None, facility_id: str | None = None) -> list[dict]:
    stmt = select(RoleRow)
    if practitioner_id:
        stmt = stmt.where(RoleRow.practitioner_id == practitioner_id)
    if facility_id:
        stmt = stmt.where(RoleRow.facility_id == facility_id)
    return [r.doc for r in db.scalars(stmt.order_by(RoleRow.id))]


def is_open(doc: dict, at: datetime | None = None) -> bool:
    """Published hours only; `availability.resolve_facility` adds closures and other live events."""
    return bool(availability.within_hours(doc.get("hours"), at or datetime.now(AMMAN)))


def parse_near(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", value)
    return (float(m[1]), float(m[2])) if m else None
