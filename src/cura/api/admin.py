"""Operational tasks: init, seed from the canonical dataset, export API edits back to staging.

The loop that keeps git and the live database in step:

    cura db export-changes   # applied API edits → sources/staging/cura_api.jsonl
    cura merge               # cura_api is the most trusted source, so edits win
    cura db seed             # canonical → database (rows edited since the export are left alone)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select

from cura.api import db, security, store
from cura.api.db import Change, User
from cura.paths import STAGING_DIR
from cura.pipeline.merge import load_canonical

# canonical doc path → staging field (inverse of pipeline.merge.fuse_facility)
FACILITY_FIELDS = {
    "name": lambda d: {"name_en": d["name"].get("en"), "name_ar": d["name"].get("ar")},
    "type": lambda d: {"type": d["type"]},
    "ownership": lambda d: {"ownership": d["ownership"]},
    "operator": lambda d: {"operator": d.get("operator")},
    "parent": lambda d: {"parent": d.get("parent")},
    "status": lambda d: {"status": d["status"]},
    "address": lambda d: {
        "governorate": d["address"].get("governorate"),
        "city": d["address"].get("city"),
        "district": d["address"].get("neighborhood"),
        "street": d["address"].get("street"),
        "building": d["address"].get("building"),
        "floor": d["address"].get("floor"),
        "postcode": d["address"].get("postcode"),
        "address": d["address"].get("text"),
    },
    "location": lambda d: {"lat": (d.get("location") or {}).get("lat"), "lon": (d.get("location") or {}).get("lon")},
    "contact": lambda d: {
        "phones": d["contact"].get("phones"),
        "emails": d["contact"].get("emails"),
        "website": d["contact"].get("website"),
    },
    "hours": lambda d: {"opening_hours": d["hours"].get("opening_hours") or d["hours"].get("text")},
    "emergency": lambda d: {"emergency": d.get("emergency")},
    "beds": lambda d: {"beds": d.get("beds")},
    "departments": lambda d: {"departments": d.get("departments")},
    "services": lambda d: {"services": d.get("services")},
    "specialties": lambda d: {"specialties": d.get("specialties")},
    "equipment": lambda d: {"equipment": [e.get("name") or e["code"] for e in d.get("equipment") or []]},
    "insurance": lambda d: {"insurance_accepted": [i["insurer"] for i in d.get("insurance") or []]},
    "accreditations": lambda d: {"accreditations": d.get("accreditations")},
    "languages": lambda d: {"languages": d.get("languages")},
    "amenities": lambda d: dict(d.get("amenities") or {}),
}
PRACTITIONER_FIELDS = {
    "name": lambda d: {"name_en": d["name"].get("en"), "name_ar": d["name"].get("ar")},
    "title": lambda d: {"title": d.get("title")},
    "grade": lambda d: {"grade": d.get("grade")},
    "gender": lambda d: {"gender": d.get("gender")},
    "specialty": lambda d: {"specialty": d.get("specialty")},
    "subspecialties": lambda d: {"subspecialties": d.get("subspecialties")},
    "languages": lambda d: {"languages": d.get("languages")},
    "qualifications": lambda d: {"qualifications": d.get("qualifications")},
}


def seed(force: bool = False) -> dict:
    db.init()
    counts: dict[str, int] = defaultdict(int)
    with next(db.get_session()) as session:
        for kind, name in (("facility", "facilities"), ("practitioner", "practitioners"), ("role", "roles")):
            for doc in load_canonical(name):
                row = store.get(session, kind, doc["id"])
                if row is not None and row.dirty and not force:
                    counts[f"{name}_kept_dirty"] += 1
                    continue
                if row is not None and row.doc == doc:
                    continue
                store.put(session, kind, doc, bump=row is not None, dirty=False)
                counts[name] += 1
            session.flush()
        session.commit()
    return dict(counts)


def export_changes() -> dict:
    """Write every applied API change as staging records, carrying only the fields that were edited."""
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    touched: dict[tuple[str, str], set[str]] = defaultdict(set)
    with next(db.get_session()) as session:
        for c in session.scalars(select(Change).where(Change.status == "applied").order_by(Change.id)):
            fields = {k for k in c.patch if not k.startswith("_")}
            touched[(c.subject_type, c.subject_id)] |= fields if c.op == "update" else {"*"}
        records = []
        for (kind, rid), fields in sorted(touched.items()):
            row = store.get(session, kind, rid)
            if row is None:
                continue
            doc = row.doc
            if kind == "facility":
                mapping = FACILITY_FIELDS
                data: dict = {"type": doc["type"], "name_en": doc["name"].get("en"), "name_ar": doc["name"].get("ar")}
            elif kind == "practitioner":
                mapping = PRACTITIONER_FIELDS
                data = {"name_en": doc["name"].get("en"), "name_ar": doc["name"].get("ar")}
            else:  # role → a practitioner record pinned to a facility id
                pr = store.get(session, "practitioner", doc["practitioner_id"]).doc
                kind, mapping = "practitioner", {}
                data = {
                    "name_en": pr["name"].get("en"),
                    "name_ar": pr["name"].get("ar"),
                    "facility_id": doc["facility_id"],
                    "practitioner_id": doc["practitioner_id"],
                    "schedule": doc["hours"].get("opening_hours") or doc["hours"].get("text"),
                    "consultation_fee": doc.get("consultation_fee"),
                    "clinic_phones": doc.get("phones"),
                    "profile_url": doc.get("profile_url"),
                }
                rid = doc["practitioner_id"]
            for f in mapping if "*" in fields else fields:
                if f in mapping:
                    data |= mapping[f](doc)
            if doc.get("rating", {}).get("count"):
                data["rating"] = doc["rating"]
            records.append(
                {
                    "kind": kind,
                    "source": "cura_api",
                    "source_ref": rid,
                    "retrieved_at": now,
                    "data": {k: v for k, v in data.items() if v is not None},
                }
            )
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    with (STAGING_DIR / "cura_api.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    (STAGING_DIR / "cura_api.meta.json").write_text(
        json.dumps(
            {
                "source": "cura_api",
                "title": "Edits made through the Cura API by verified owners, contributors and moderators",
                "publisher": "Cura contributors",
                "retrieved_at": now,
                "method": "api",
                "license": "ODbL-1.0",
                "robots_respected": True,
                "record_count": len(records),
                "notes": "Only fields changed through the API are present; every change is in the API change log.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"records": len(records)}


def create_admin(email: str, password: str) -> dict:
    db.init()
    with next(db.get_session()) as session:
        user = session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            user = User(email=email.lower(), password_hash=security.hash_password(password))
            session.add(user)
        user.role = "admin"
        session.commit()
        return {"id": user.id, "email": user.email, "role": user.role}


def run(task: str, email: str | None = None, password: str | None = None, message: str | None = None) -> dict:
    from cura.api import migrate

    if task in ("init", "upgrade"):
        db.init()
        return {"revision": migrate.current(db.engine())}
    if task == "revision":
        if not message:
            raise SystemExit("-m/--message is required")
        db.init()
        migrate.revision(db.engine(), message)
        return {"ok": True}
    if task == "seed":
        return seed()
    if task == "export-changes":
        return export_changes()
    if task == "create-admin":
        if not (email and password):
            raise SystemExit("--email and --password are required")
        return create_admin(email, password)
    raise SystemExit(f"unknown task {task}")
