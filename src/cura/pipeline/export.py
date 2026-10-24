"""Build release artefacts into dist/: JSON Schemas, GeoJSON, flat CSVs and a SQLite database.

The JSONL files under data/ are the canonical, diffable form. Everything here is derived and is
published as a GitHub release asset, never committed.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from cura import model
from cura.paths import DIST_DIR, SCHEMA_DIR
from cura.pipeline.merge import load_canonical

SCHEMAS = {"facility": model.Facility, "practitioner": model.Practitioner, "role": model.Role}


def write_schemas() -> list[Path]:
    SCHEMA_DIR.mkdir(exist_ok=True)
    out = []
    for name, cls in SCHEMAS.items():
        schema = cls.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://raw.githubusercontent.com/NextMoveIntl/cura/main/schema/{name}.schema.json"
        path = SCHEMA_DIR / f"{name}.schema.json"
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.append(path)
    return out


def _flat_facility(f: dict) -> dict:
    loc = f.get("location") or {}
    return {
        "id": f["id"],
        "status": f["status"],
        "type": f["type"],
        "name_en": f["name"].get("en"),
        "name_ar": f["name"].get("ar"),
        "ownership": f["ownership"],
        "governorate": f["address"].get("governorate"),
        "district": f["address"].get("district"),
        "city": f["address"].get("city"),
        "address": f["address"].get("text"),
        "lat": loc.get("lat"),
        "lon": loc.get("lon"),
        "phones": ";".join(f["contact"].get("phones") or []),
        "website": f["contact"].get("website"),
        "opening_hours": f["hours"].get("opening_hours") or f["hours"].get("text"),
        "emergency": f.get("emergency"),
        "beds": f.get("beds"),
        "specialties": ";".join(f.get("specialties") or []),
        "insurance": ";".join(i["insurer"] for i in f.get("insurance") or []),
        "completeness": f["quality"]["completeness"],
        "confidence": f["quality"]["confidence"],
        "updated_at": f["updated_at"],
    }


def _flat_practitioner(p: dict) -> dict:
    return {
        "id": p["id"],
        "name_en": p["name"].get("en"),
        "name_ar": p["name"].get("ar"),
        "title": p.get("title"),
        "grade": p["grade"],
        "specialty": p.get("specialty"),
        "subspecialties": ";".join(p.get("subspecialties") or []),
        "gender": p.get("gender"),
        "languages": ";".join(p.get("languages") or []),
        "updated_at": p["updated_at"],
    }


def _flat_role(r: dict) -> dict:
    fee = r.get("consultation_fee") or {}
    return {
        "id": r["id"],
        "practitioner_id": r["practitioner_id"],
        "facility_id": r["facility_id"],
        "specialty": r.get("specialty"),
        "schedule": r["hours"].get("opening_hours") or r["hours"].get("text"),
        "fee_amount": fee.get("amount"),
        "fee_currency": fee.get("currency"),
        "profile_url": r.get("profile_url"),
    }


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # BOM so Excel opens Arabic correctly
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run() -> dict[str, str]:
    DIST_DIR.mkdir(exist_ok=True)
    facilities = [f for f in load_canonical("facilities") if f["status"] != "merged"]
    practitioners = load_canonical("practitioners")
    roles = load_canonical("roles")
    write_schemas()

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": f["id"],
                "geometry": {"type": "Point", "coordinates": [f["location"]["lon"], f["location"]["lat"]]},
                "properties": _flat_facility(f),
            }
            for f in facilities
            if f.get("location")
        ],
    }
    (DIST_DIR / "facilities.geojson").write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")

    flat = {
        "facilities": [_flat_facility(f) for f in facilities],
        "practitioners": [_flat_practitioner(p) for p in practitioners],
        "roles": [_flat_role(r) for r in roles],
    }
    for name, rows in flat.items():
        _csv(DIST_DIR / f"{name}.csv", rows)

    db_path = DIST_DIR / "cura.sqlite"
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    for name, rows in flat.items():
        if not rows:
            continue
        cols = list(rows[0])
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)}, PRIMARY KEY (id))")
        con.executemany(
            f"INSERT INTO {name} VALUES ({', '.join('?' for _ in cols)})", [tuple(r.values()) for r in rows]
        )
    con.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, kind TEXT, json TEXT)")
    for kind, rows in (("facility", facilities), ("practitioner", practitioners), ("role", roles)):
        con.executemany(
            "INSERT INTO documents VALUES (?, ?, ?)", [(r["id"], kind, json.dumps(r, ensure_ascii=False)) for r in rows]
        )
    con.execute("CREATE INDEX ix_fac_gov ON facilities (governorate, type)")
    con.execute("CREATE INDEX ix_roles_fac ON roles (facility_id)")
    con.execute("CREATE INDEX ix_roles_prc ON roles (practitioner_id)")
    con.commit()
    con.close()
    return {p.name: f"{p.stat().st_size / 1024:.0f} KB" for p in sorted(DIST_DIR.iterdir())}
