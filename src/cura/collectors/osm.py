"""OpenStreetMap collector — every health-tagged node, way and relation inside Jordan via Overpass.

OSM is the geographic backbone of Cura: it is the only open source with coordinates for thousands of
clinics and pharmacies. Data is © OpenStreetMap contributors, ODbL-1.0, which is why the published
dataset is ODbL too.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from cura.collectors._common import http_client, now_iso, raw_dir, write_staging

SOURCE = "osm"
JORDAN_AREA_ID = 3600184818  # relation 184818 (Jordan) + 3600000000

QUERY = f"""
[out:json][timeout:300];
area({JORDAN_AREA_ID})->.jo;
(
  nwr["amenity"~"^(hospital|clinic|doctors|dentist|pharmacy)$"](area.jo);
  nwr["healthcare"](area.jo);
  nwr["emergency"="ambulance_station"](area.jo);
  nwr["shop"="optician"](area.jo);
);
out center tags;
"""

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

AMENITY_TYPE = {
    "hospital": "hospital",
    "clinic": "clinic",
    "doctors": "doctor_office",
    "dentist": "dental_clinic",
    "pharmacy": "pharmacy",
}
HEALTHCARE_TYPE = {
    "hospital": "hospital",
    "clinic": "clinic",
    "centre": "clinic",
    "doctor": "doctor_office",
    "dentist": "dental_clinic",
    "pharmacy": "pharmacy",
    "laboratory": "laboratory",
    "sample_collection": "laboratory",
    "blood_bank": "blood_bank",
    "blood_donation": "blood_bank",
    "physiotherapist": "rehabilitation",
    "rehabilitation": "rehabilitation",
    "optometrist": "optician",
    "dialysis": "dialysis_center",
    "radiology": "imaging_center",
}


def fetch(retries: int = 6) -> dict[str, Any]:
    delay = 15.0
    last: Exception | None = None
    with http_client(timeout=360) as client:
        for attempt in range(retries):
            url = ENDPOINTS[attempt % len(ENDPOINTS)]
            try:
                resp = client.post(url, data={"data": QUERY})
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last = exc
                print(f"overpass {url} failed ({exc!s:.80}); retry in {delay:.0f}s")
                time.sleep(delay)
                delay *= 1.6
    raise RuntimeError(f"Overpass unreachable after {retries} attempts: {last}")


def _type(tags: dict[str, str]) -> str | None:
    if tags.get("emergency") == "ambulance_station":
        return "emergency_medical_service"
    if tags.get("shop") == "optician":
        return "optician"
    if (amenity := tags.get("amenity")) in AMENITY_TYPE:
        # amenity=clinic + healthcare=dentist etc.: the healthcare tag is more specific.
        specific = HEALTHCARE_TYPE.get(tags.get("healthcare", ""))
        if amenity == "clinic" and specific and specific not in ("clinic", "hospital"):
            return specific
        return AMENITY_TYPE[amenity]
    if (hc := tags.get("healthcare")) in HEALTHCARE_TYPE:
        return HEALTHCARE_TYPE[hc]
    if tags.get("healthcare"):
        return "other"
    return None


def _ownership(tags: dict[str, str]) -> str | None:
    op = " ".join(tags.get(k, "") for k in ("operator", "operator:en", "operator:ar")).lower()
    if "ministry of health" in op or "وزارة الصحة" in op:
        return "moh"
    if "royal medical" in op or "الخدمات الطبية الملكية" in op:
        return "rms"
    if "unrwa" in op or "الأونروا" in op or "الاونروا" in op:
        return "unrwa"
    if "university" in op or "جامعة" in op:
        return "university"
    return {
        "private": "private",
        "government": "other_gov",
        "public": "other_gov",
        "military": "rms",
        "ngo": "ngo",
        "charity": "ngo",
        "community": "ngo",
        "religious": "ngo",
    }.get(tags.get("operator:type", "").lower())


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def to_record(el: dict[str, Any], retrieved_at: str) -> dict[str, Any] | None:
    tags = el.get("tags", {})
    ftype = _type(tags)
    if ftype is None:
        return None
    lat = el.get("lat", el.get("center", {}).get("lat"))
    lon = el.get("lon", el.get("center", {}).get("lon"))
    phones = _split(tags.get("phone")) + _split(tags.get("contact:phone")) + _split(tags.get("contact:mobile"))
    data: dict[str, Any] = {
        "name_en": tags.get("name:en") or None,
        "name_ar": tags.get("name:ar") or None,
        "name": tags.get("name") or None,
        "type": ftype,
        "ownership": _ownership(tags),
        "operator": tags.get("operator") or None,
        "parent": tags.get("brand") or None,
        "city": tags.get("addr:city") or None,
        "district": tags.get("addr:suburb") or tags.get("addr:district") or None,
        "street": tags.get("addr:street") or None,
        "building": tags.get("addr:housenumber") or None,
        "floor": tags.get("level") or tags.get("addr:floor") or None,
        "postcode": tags.get("addr:postcode") or None,
        "lat": lat,
        "lon": lon,
        "phones": list(dict.fromkeys(phones)),
        "emails": _split(tags.get("email")) + _split(tags.get("contact:email")),
        "website": tags.get("website") or tags.get("contact:website") or None,
        "opening_hours": tags.get("opening_hours") or None,
        "emergency": {"yes": True, "no": False}.get(tags.get("emergency", "")),
        "beds": int(tags["beds"]) if tags.get("beds", "").isdigit() else None,
        "specialties": _split(tags.get("healthcare:speciality")),
        "extra": {
            k: v
            for k, v in {
                "wheelchair": tags.get("wheelchair"),
                "dispensing": tags.get("dispensing"),
                "drive_through": tags.get("drive_through"),
                "check_date": tags.get("check_date"),
                "wikidata": tags.get("wikidata"),
                "osm_amenity": tags.get("amenity"),
                "osm_healthcare": tags.get("healthcare"),
            }.items()
            if v
        },
    }
    return {
        "kind": "facility",
        "source": SOURCE,
        "source_ref": f"{el['type']}/{el['id']}",
        "source_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        "retrieved_at": retrieved_at,
        "data": {k: v for k, v in data.items() if v not in (None, [], {})},
    }


def run() -> int:
    payload = fetch()
    retrieved_at = now_iso()
    (raw_dir(SOURCE) / "overpass.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    records = [r for el in payload.get("elements", []) if (r := to_record(el, retrieved_at))]
    return write_staging(
        SOURCE,
        records,
        {
            "title": "OpenStreetMap — health-tagged features in Jordan",
            "publisher": "OpenStreetMap contributors",
            "urls": ["https://www.openstreetmap.org/relation/184818"],
            "retrieved_at": retrieved_at,
            "method": "api",
            "license": "ODbL-1.0",
            "robots_respected": True,
            "notes": f"Overpass snapshot, osm_base={payload.get('osm3s', {}).get('timestamp_osm_base')}. "
            "Unnamed features are kept; the merge step decides whether they are publishable.",
        },
    )
