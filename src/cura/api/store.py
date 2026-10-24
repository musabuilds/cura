"""Registry storage: document ↔ row mapping, search and geo queries."""

from __future__ import annotations

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cura import model
from cura import normalize as nz
from cura.api.db import FacilityRow, PractitionerRow, RoleRow, utcnow
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


def put(db: Session, kind: str, doc: dict, *, bump: bool = True, dirty: bool = True):
    """Insert or replace a document. Validates against the canonical model first."""
    row_cls, _ = KINDS[kind]
    doc = validate(kind, doc)
    row = db.get(row_cls, doc["id"])
    values = row_values(kind, doc)
    if row is None:
        row = row_cls(id=doc["id"], doc=doc, version=1, dirty=dirty, updated_at=utcnow(), **values)
        db.add(row)
    else:
        row.doc = doc
        for k, v in values.items():
            setattr(row, k, v)
        if bump:
            row.version += 1
            row.updated_at = utcnow()
        row.dirty = row.dirty or dirty
    return row


def get(db: Session, kind: str, rid: str):
    return db.get(KINDS[kind][0], rid)


def next_reserved_id(db: Session, kind: str, prefix: str) -> str:
    """IDs for API-created records come from the reserved top range (see pipeline IdAllocator)."""
    row_cls, _ = KINDS[kind]
    width = {"facility": 5, "practitioner": 6, "role": 7}[kind]
    floor = 9 * 10 ** (width - 1)
    ids = db.scalars(select(row_cls.id).where(row_cls.id.like(f"{prefix}-%"))).all()
    nums = [int(i.rsplit("-", 1)[1]) for i in ids if int(i.rsplit("-", 1)[1]) >= floor]
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
            scored = [(d, r) for d, r in scored if is_open(r.doc)]
        offset = int(cursor or 0)
        page = scored[offset : offset + limit]
        distances = {r.id: round(d) for d, r in page}
        nxt = str(offset + limit) if offset + limit < len(scored) else None
        return [r.doc for _, r in page], nxt, distances
    if cursor:
        conds.append(FacilityRow.id > cursor)
    stmt = stmt.where(and_(*conds)).order_by(FacilityRow.id)
    if open_now:
        rows = [r for r in db.scalars(stmt.limit(limit * 10)).all() if is_open(r.doc)][:limit]
    else:
        rows = db.scalars(stmt.limit(limit + 1)).all()
    nxt = rows[limit - 1].id if len(rows) > limit else None
    return [r.doc for r in rows[:limit]], nxt, distances


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


def roles_for(db: Session, *, practitioner_id: str | None = None, facility_id: str | None = None) -> list[dict]:
    stmt = select(RoleRow)
    if practitioner_id:
        stmt = stmt.where(RoleRow.practitioner_id == practitioner_id)
    if facility_id:
        stmt = stmt.where(RoleRow.facility_id == facility_id)
    return [r.doc for r in db.scalars(stmt.order_by(RoleRow.id))]


_DAY = ["mo", "tu", "we", "th", "fr", "sa", "su"]


def is_open(doc: dict, at: datetime | None = None) -> bool:
    hours = doc.get("hours") or {}
    if hours.get("always_open"):
        return True
    weekly = hours.get("weekly")
    if not weekly:
        return False  # unknown hours: never claim "open"
    now = (at or datetime.now(AMMAN)).astimezone(AMMAN)
    day, hhmm = _DAY[now.weekday()], now.strftime("%H:%M")
    prev = _DAY[(now.weekday() - 1) % 7]
    for span in weekly:
        o, c = span["opens"], span["closes"]
        if span["day"] == day and (o <= hhmm < c or (c <= o and hhmm >= o)):
            return True
        if span["day"] == prev and c <= o and hhmm < c:  # past-midnight span from yesterday
            return True
    return False


def parse_near(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", value)
    return (float(m[1]), float(m[2])) if m else None
