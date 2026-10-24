"""Read API: facilities, practitioners, reference vocabularies, prices, the change feed, stats.

Reads are open (the data is ODbL). An `X-API-Key` only raises the rate limit.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cura.api import store
from cura.api.db import FacilityRow, PractitionerRow, RoleRow, get_session
from cura.api.deps import Principal, principal
from cura.api.errors import Problem
from cura.paths import DATA_DIR
from cura.reference import read_csv

router = APIRouter(prefix="/v1")
REFERENCE = (
    "governorates",
    "facility_types",
    "ownership",
    "specialties",
    "insurers",
    "equipment",
    "holidays",
    "review_dimensions",
)
Limit = Annotated[int, Query(ge=1, le=200)]


def _etag(response: Response, row) -> None:
    response.headers["ETag"] = f'"{row.version}"'
    response.headers["Last-Modified"] = row.updated_at.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _page(data: list, next_cursor: str | None, **extra) -> dict:
    return {"data": data, "next_cursor": next_cursor, "count": len(data), **extra}


@router.get("/facilities", tags=["registry"])
def list_facilities(
    q: str | None = None,
    type: Annotated[str | None, Query(description="comma-separated facility type codes")] = None,
    governorate: Annotated[str | None, Query(description="JO-AM or AM; comma-separated")] = None,
    ownership: str | None = None,
    specialty: str | None = None,
    insurer: str | None = None,
    emergency: bool | None = None,
    near: Annotated[
        str | None, Query(description="lat,lon — results sorted by distance", examples=["31.9539,35.9106"])
    ] = None,
    radius_km: Annotated[float, Query(gt=0, le=100)] = 5.0,
    open_now: Annotated[bool, Query(description="only places whose published hours say they are open now")] = False,
    include_closed: bool = False,
    cursor: str | None = None,
    limit: Limit = 50,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    point = store.parse_near(near)
    if near and point is None:
        raise Problem(422, "Invalid near", "Use near=<lat>,<lon>, e.g. near=31.9539,35.9106")
    data, nxt, dist = store.search_facilities(
        db,
        q=q,
        type=type,
        governorate=governorate,
        ownership=ownership,
        specialty=specialty,
        insurer=insurer,
        emergency=emergency,
        near=point,
        radius_km=radius_km,
        open_now=open_now,
        include_closed=include_closed,
        cursor=cursor,
        limit=limit,
    )
    if dist:
        data = [d | {"distance_m": dist[d["id"]]} for d in data]
    return _page(data, nxt)


@router.get("/facilities/{facility_id}", tags=["registry"])
def get_facility(
    facility_id: str,
    response: Response,
    expand: str | None = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    row = store.get(db, "facility", facility_id)
    if row is None:
        raise Problem(404, "Facility not found", facility_id)
    _etag(response, row)
    doc = dict(row.doc)
    doc["open_now"] = store.is_open(doc) if (doc["hours"].get("weekly") or doc["hours"].get("always_open")) else None
    if expand == "practitioners":
        roles = store.roles_for(db, facility_id=facility_id)
        people = {
            r.id: r.doc
            for r in db.scalars(
                select(PractitionerRow).where(PractitionerRow.id.in_([x["practitioner_id"] for x in roles]))
            )
        }
        doc["practitioners"] = [{"role": r, "practitioner": people.get(r["practitioner_id"])} for r in roles]
    return doc


@router.get("/facilities/{facility_id}/practitioners", tags=["registry"])
def facility_practitioners(
    facility_id: str, _: Principal = Depends(principal), db: Session = Depends(get_session)
) -> dict:
    if store.get(db, "facility", facility_id) is None:
        raise Problem(404, "Facility not found", facility_id)
    roles = store.roles_for(db, facility_id=facility_id)
    people = {
        r.id: r.doc
        for r in db.scalars(
            select(PractitionerRow).where(PractitionerRow.id.in_([x["practitioner_id"] for x in roles]))
        )
    }
    return _page([{"role": r, "practitioner": people.get(r["practitioner_id"])} for r in roles], None)


@router.get("/practitioners", tags=["registry"])
def list_practitioners(
    q: str | None = None,
    specialty: str | None = None,
    facility: str | None = None,
    cursor: str | None = None,
    limit: Limit = 50,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    data, nxt = store.search_practitioners(db, q=q, specialty=specialty, facility=facility, cursor=cursor, limit=limit)
    return _page(data, nxt)


@router.get("/practitioners/{practitioner_id}", tags=["registry"])
def get_practitioner(
    practitioner_id: str, response: Response, _: Principal = Depends(principal), db: Session = Depends(get_session)
) -> dict:
    row = store.get(db, "practitioner", practitioner_id)
    if row is None:
        raise Problem(404, "Practitioner not found", practitioner_id)
    _etag(response, row)
    roles = store.roles_for(db, practitioner_id=practitioner_id)
    facilities = {
        r.id: r.doc
        for r in db.scalars(select(FacilityRow).where(FacilityRow.id.in_([x["facility_id"] for x in roles])))
    }
    return row.doc | {"roles": [r | {"facility": _summary(facilities.get(r["facility_id"]))} for r in roles]}


def _summary(doc: dict | None) -> dict | None:
    if not doc:
        return None
    return {
        "id": doc["id"],
        "type": doc["type"],
        "name": doc["name"],
        "address": doc["address"],
        "location": doc["location"],
    }


@router.get("/roles/{role_id}", tags=["registry"])
def get_role(
    role_id: str, response: Response, _: Principal = Depends(principal), db: Session = Depends(get_session)
) -> dict:
    row = store.get(db, "role", role_id)
    if row is None:
        raise Problem(404, "Role not found", role_id)
    _etag(response, row)
    return row.doc


@router.get("/reference/{name}", tags=["reference"])
def reference(name: str, _: Principal = Depends(principal)) -> dict:
    if name not in REFERENCE:
        raise Problem(404, "Unknown vocabulary", f"One of: {', '.join(REFERENCE)}")
    return {"data": read_csv(f"{name}.csv")}


@router.get("/prices", tags=["reference"])
def prices(category: str | None = None, q: str | None = None, _: Principal = Depends(principal)) -> dict:
    path = DATA_DIR / "prices.jsonl"
    rows = [json.loads(line) for line in path.open(encoding="utf-8")] if path.exists() else []
    if category:
        rows = [r for r in rows if r.get("category") == category]
    if q:
        rows = [r for r in rows if q.lower() in (r.get("item", "") + " " + r.get("item_ar", "")).lower()]
    return {"data": rows, "count": len(rows)}


@router.get("/changes", tags=["sync"])
def changes(
    since: Annotated[datetime, Query(description="ISO-8601; returns records updated after this instant")],
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Incremental sync: poll with the `next_since` you got last time to keep a local copy current."""
    out = []
    for kind, cls in (("facility", FacilityRow), ("practitioner", PractitionerRow), ("role", RoleRow)):
        for row in db.scalars(select(cls).where(cls.updated_at > since).order_by(cls.updated_at).limit(limit)):
            out.append(
                {"kind": kind, "id": row.id, "version": row.version, "updated_at": row.updated_at, "doc": row.doc}
            )
    out.sort(key=lambda c: c["updated_at"])
    out = out[:limit]
    return {"data": out, "count": len(out), "next_since": out[-1]["updated_at"] if out else since}


@router.get("/stats", tags=["registry"])
def stats(request: Request, _: Principal = Depends(principal), db: Session = Depends(get_session)) -> dict:
    def by(col):
        return dict(db.execute(select(col, func.count()).where(FacilityRow.status != "merged").group_by(col)).all())

    return {
        "facilities": db.scalar(select(func.count()).select_from(FacilityRow).where(FacilityRow.status != "merged")),
        "practitioners": db.scalar(select(func.count()).select_from(PractitionerRow)),
        "roles": db.scalar(select(func.count()).select_from(RoleRow)),
        "by_type": by(FacilityRow.type),
        "by_governorate": {k or "unknown": v for k, v in by(FacilityRow.governorate).items()},
        "by_ownership": by(FacilityRow.ownership),
    }
