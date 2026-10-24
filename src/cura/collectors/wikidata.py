"""Wikidata collector — hospitals and health facilities located in Jordan (CC0)."""

from __future__ import annotations

from cura.collectors._common import http_client, now_iso, write_staging

SOURCE = "wikidata"
ENDPOINT = "https://query.wikidata.org/sparql"
QUERY = """
SELECT ?item ?en ?ar ?coord ?beds ?website ?inception ?operatorLabel WHERE {
  ?item wdt:P31/wdt:P279* wd:Q16917 ; wdt:P17 wd:Q810 .
  OPTIONAL { ?item rdfs:label ?en FILTER(LANG(?en) = "en") }
  OPTIONAL { ?item rdfs:label ?ar FILTER(LANG(?ar) = "ar") }
  OPTIONAL { ?item wdt:P625 ?coord }
  OPTIONAL { ?item wdt:P6801 ?beds }
  OPTIONAL { ?item wdt:P856 ?website }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P137 ?operator }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,ar". }
}
"""
# Statements known to be wrong on Wikidata, suppressed until fixed upstream: (QID, field).
SUPPRESS = {
    ("Q22689891", "website"),  # Al-Bashir Hospital carries Al-Khalidi's site (kmc.jo)
}
CLASS_TYPE = "hospital"  # hospital (Q16917) and its subclasses: teaching, military, children's …


def run() -> int:
    with http_client(timeout=120) as client:
        resp = client.get(ENDPOINT, params={"query": QUERY, "format": "json"})
        resp.raise_for_status()
        rows = resp.json()["results"]["bindings"]
    retrieved_at = now_iso()
    by_item: dict[str, dict] = {}
    for row in rows:
        qid = row["item"]["value"].rsplit("/", 1)[1]
        rec = by_item.setdefault(qid, {"name_en": None, "name_ar": None})
        val = lambda k: row.get(k, {}).get("value")  # noqa: E731, B023
        rec["name_en"] = rec["name_en"] or val("en")
        rec["name_ar"] = rec["name_ar"] or val("ar")
        if (coord := val("coord")) and "lat" not in rec:
            lon, lat = coord.removeprefix("Point(").removesuffix(")").split()
            rec["lat"], rec["lon"] = float(lat), float(lon)
        if (beds := val("beds")) and beds.split(".")[0].isdigit():
            rec["beds"] = int(beds.split(".")[0])
        rec["website"] = rec.get("website") or val("website")
        rec["operator"] = rec.get("operator") or val("operatorLabel")
        if inception := val("inception"):
            rec.setdefault("extra", {})["founded"] = inception[:10]
    records = [
        {
            "kind": "facility",
            "source": SOURCE,
            "source_ref": qid,
            "source_url": f"https://www.wikidata.org/wiki/{qid}",
            "retrieved_at": retrieved_at,
            "data": {
                k: v
                for k, v in (data | {"type": CLASS_TYPE, "wikidata": qid}).items()
                if v is not None and (qid, k) not in SUPPRESS
            },
        }
        for qid, data in by_item.items()
        if data["name_en"] or data["name_ar"]
    ]
    return write_staging(
        SOURCE,
        records,
        {
            "title": "Wikidata — hospitals in Jordan",
            "publisher": "Wikidata contributors",
            "urls": [ENDPOINT],
            "retrieved_at": retrieved_at,
            "method": "api",
            "license": "CC0-1.0",
            "robots_respected": True,
            "notes": "Coordinates on Wikidata are often city-level; the merge treats them as approximate.",
        },
    )
