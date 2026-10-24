"""Read API: facilities, practitioners, reference vocabularies, prices, the change feed, stats.

Reads are open (the data is ODbL). An `X-API-Key` only raises the rate limit.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cura.api import contact, live, store, verification, writes
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


Fields = Annotated[
    str | None,
    Query(description="Comma-separated top-level fields to return (id is always included), e.g. `name,location`"),
]


def _project(doc: dict, fields: str | None) -> dict:
    """Sparse fieldsets: a mobile list screen asks for what it draws. Nothing is removed from the record."""
    if not fields:
        return doc
    keep = {"id", *(f.strip() for f in fields.split(",") if f.strip())}
    return {k: v for k, v in doc.items() if k in keep}


def _open_now(state: dict) -> bool | None:
    return None if state["state"] == "unknown" else state["state"] in ("open", "limited")


def _roster(db: Session, facility_id: str, include_all: bool) -> list[dict]:
    roles = store.roles_for(db, facility_id=facility_id)
    if not include_all:
        roles = [r for r in roles if store.is_listed(r)]
    states = live.role_availability(db, roles, live.now())
    people = {d["id"]: d for d in store.docs(db, "practitioner", {r["practitioner_id"] for r in roles})}
    return [
        {"role": r | {"availability": states[r["id"]]}, "practitioner": people.get(r["practitioner_id"])} for r in roles
    ]


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
    fields: Fields = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """`open_now` honours live closures and openings set by the facility, not only its published hours."""
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
    return _page([_project(d, fields) for d in data], nxt)


@router.get("/facilities/{facility_id}", tags=["registry"])
def get_facility(
    facility_id: str,
    response: Response,
    expand: str | None = None,
    fields: Fields = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """The record, plus `live` — what is happening now (schedule + closures/events) — and `open_now`."""
    row = store.get(db, "facility", facility_id)
    if row is None:
        raise Problem(404, "Facility not found", facility_id)
    _etag(response, row)
    doc = dict(row.doc)
    doc["live"] = live.facility_live(db, [doc], live.now())[facility_id]
    doc["open_now"] = _open_now(doc["live"])
    doc["verification"] = verification.verifications(db, "facility", [facility_id])[facility_id]
    doc["version"] = row.version
    if expand == "practitioners":
        doc["practitioners"] = _roster(db, facility_id, include_all=False)
    return _project(doc, fields)


@router.get("/facilities/{facility_id}/practitioners", tags=["registry"])
def facility_practitioners(
    facility_id: str,
    include: Annotated[
        Literal["listed", "all"],
        Query(description="`all` adds doctors who left and roles awaiting confirmation (the admin roster)"),
    ] = "listed",
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    if store.get(db, "facility", facility_id) is None:
        raise Problem(404, "Facility not found", facility_id)
    return _page(_roster(db, facility_id, include_all=include == "all"), None)


@router.get("/practitioners", tags=["registry"])
def list_practitioners(
    q: str | None = None,
    specialty: str | None = None,
    facility: str | None = None,
    cursor: str | None = None,
    limit: Limit = 50,
    fields: Fields = None,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    data, nxt = store.search_practitioners(db, q=q, specialty=specialty, facility=facility, cursor=cursor, limit=limit)
    return _page([_project(d, fields) for d in data], nxt)


@router.get("/practitioners/{practitioner_id}", tags=["registry"])
def get_practitioner(
    practitioner_id: str,
    response: Response,
    fields: Fields = None,
    p: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """The doctor, every place they work or worked (`active`, `period`) and their availability at each now."""
    row = store.get(db, "practitioner", practitioner_id)
    if row is None:
        raise Problem(404, "Practitioner not found", practitioner_id)
    _etag(response, row)
    out = practitioner_view(db, row, p)
    if out.get("contact_visibility") == "owner":
        # The owner's view carries private contact details: never cache it where others could read it.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Authorization"
    return _project(out, fields)


def practitioner_view(db: Session, row, p: Principal | None) -> dict:
    """The doctor, every place they work or worked, availability at each now, and the summary fields
    apps draw. `contact` is the public part, except for the profile's owner, who sees everything."""
    roles = [
        (r.doc, r.version)
        for r in db.scalars(select(RoleRow).where(RoleRow.practitioner_id == row.id).order_by(RoleRow.id))
        if r.doc.get("confirmation") != "declined"
    ]
    docs = [d for d, _ in roles]
    facilities = {
        s["id"]: s
        for s in verification.facility_summaries(db, store.docs(db, "facility", {d["facility_id"] for d in docs}))
    }
    states = live.role_availability(db, docs, live.now())
    summary = verification.practitioner_summary(db, row.doc)
    out = row.doc | {
        "specialty_label": summary["specialty_label"],
        "facilities_count": summary["facilities_count"],
        "verification": summary["verification"],
        "version": row.version,
        "contact_visibility": "public",
        "roles": [
            d | {"facility": facilities.get(d["facility_id"]), "availability": states[d["id"]], "version": v}
            for d, v in roles
        ],
    }
    if p is not None and p.user is not None and (p.is_moderator or writes.owns(db, p, "practitioner", row.doc)):
        out["contact"] = contact.private_contact(db, row.id) or {"phones": [], "public_email": False}
        out["contact_visibility"] = "owner"
    return out


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
