"""`GET /v1/search` — one search box over facilities and doctors, in Arabic or English, by name or by need."""

from __future__ import annotations

from functools import cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cura.api import store
from cura.api.db import get_session
from cura.api.deps import Principal, principal
from cura.api.errors import Problem
from cura.search import embedder as E
from cura.search import index

router = APIRouter(prefix="/v1")


@cache
def _embedder() -> E.Embedder | None:
    return E.from_settings()


@router.get("/search", tags=["search"])
def search(
    q: Annotated[str, Query(min_length=2, max_length=300, examples=["طبيب قلب في إربد يقبل ميدنت"])],
    kind: Annotated[str, Query(description="facility, practitioner, or both (comma-separated)")] = (
        "facility,practitioner"
    ),
    mode: Annotated[
        Literal["hybrid", "semantic", "lexical"],
        Query(description="hybrid fuses name matching with meaning; falls back to lexical without a model"),
    ] = "hybrid",
    type: str | None = None,
    governorate: str | None = None,
    specialty: str | None = None,
    insurer: str | None = None,
    emergency: bool | None = None,
    near: Annotated[str | None, Query(description="lat,lon")] = None,
    radius_km: Annotated[float, Query(gt=0, le=100)] = 10.0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    _: Principal = Depends(principal),
    db: Session = Depends(get_session),
) -> dict:
    """Natural-language search: "heart doctor in Irbid who takes MedNet", "مختبر قريب مفتوح".
    Each result carries its rank in each method, so a client can see why it matched."""
    kinds = [k for k in kind.split(",") if k in index.ROWS]
    if not kinds:
        raise Problem(422, "Invalid kind", "facility and/or practitioner")
    point = store.parse_near(near)
    if near and point is None:
        raise Problem(422, "Invalid near", "Use near=<lat>,<lon>")
    embedder = _embedder()
    if mode == "semantic" and embedder is None:
        raise Problem(
            503, "Semantic search is not configured", "This instance has no embedding model; use mode=hybrid."
        )
    filters = {
        "type": type,
        "governorate": governorate,
        "specialty": specialty,
        "insurer": insurer,
        "emergency": emergency,
        "near": point,
        "radius_km": radius_km,
    }
    data, used = index.hybrid(db, embedder, q, kinds=kinds, filters=filters, mode=mode, limit=limit)
    return {
        "data": data,
        "count": len(data),
        "mode": used,
        "model": embedder.model if embedder and used != "lexical" else None,
        "semantic": bool(embedder and embedder.semantic and used != "lexical"),
    }
