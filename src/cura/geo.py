"""Reverse geocoding against governorate (ADM1) and district/liwa (ADM2) polygons.

Boundaries: geoBoundaries gbOpen JOR (ADM1 CC-BY-2.5, ADM2 public domain), simplified. A point on a
shared border can land on either side; that is acceptable at governorate granularity.
"""

from __future__ import annotations

import json
import math
from functools import cache

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from cura.paths import GEO_DIR


@cache
def _layer(level: str) -> tuple[STRtree, list[dict]]:
    with (GEO_DIR / f"jo_{level}.geojson").open(encoding="utf-8") as fh:
        features = json.load(fh)["features"]
    geoms = [shape(f["geometry"]) for f in features]
    return STRtree(geoms), [f["properties"] | {"_geom": g} for f, g in zip(features, geoms, strict=True)]


def _locate(level: str, lat: float, lon: float) -> dict | None:
    tree, props = _layer(level)
    pt = Point(lon, lat)
    for idx in tree.query(pt):
        if props[idx]["_geom"].covers(pt):
            return props[idx]
    # Coastline/border simplification can leave real points just outside: snap within ~2 km.
    idx = tree.nearest(pt)
    if idx is not None and props[idx]["_geom"].distance(pt) < 0.02:
        return props[idx]
    return None


def governorate_at(lat: float, lon: float) -> str | None:
    hit = _locate("adm1", lat, lon)
    return hit.get("shapeISO") if hit else None


def district_at(lat: float, lon: float) -> str | None:
    hit = _locate("adm2", lat, lon)
    return hit.get("shapeName") if hit else None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
