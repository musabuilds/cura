"""The text a record is embedded as.

It describes what a place or a person *is* — names in both languages, kind, specialties, services,
equipment, insurers, where — and leaves out what changes daily (availability, ratings, hours), so an
hours edit never forces a re-embed. Codes are expanded to their English and Arabic labels: a patient
typing "دكتور قلب" should land on `cardiology` without knowing the code.
"""

from __future__ import annotations

import hashlib
from functools import cache

from cura.reference import read_csv


@cache
def _labels(vocabulary: str) -> dict[str, str]:
    out = {}
    for row in read_csv(f"{vocabulary}.csv"):
        names = [row.get("name_en"), row.get("name_ar"), *((row.get("aliases") or "").split(";"))]
        out[row["code"]] = " / ".join(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    return out


@cache
def _governorates() -> dict[str, str]:
    return {r["iso_3166_2"]: f"{r['name_en']} / {r['name_ar']}" for r in read_csv("governorates.csv")}


def _join(items) -> str:
    return "; ".join(dict.fromkeys(str(i) for i in items if i))


def _name(doc: dict) -> str:
    name = doc.get("name") or {}
    return _join([name.get("en"), name.get("ar"), *name.get("aliases", [])])


def facility_text(doc: dict) -> str:
    address = doc.get("address") or {}
    lines = [
        f"Name: {_name(doc)}",
        f"Type: {_labels('facility_types').get(doc.get('type'), doc.get('type'))}",
        f"Ownership: {doc.get('ownership')}" if doc.get("ownership") not in (None, "unknown") else "",
        "Location: "
        + _join(
            [
                address.get("neighborhood"),
                address.get("city"),
                address.get("district"),
                _governorates().get(address.get("governorate") or ""),
            ]
        ),
        "Emergency department: yes / طوارئ" if doc.get("emergency") else "",
        "Specialties: " + _join(_labels("specialties").get(s, s) for s in doc.get("specialties") or []),
        "Departments: " + _join(doc.get("departments") or []),
        "Services: " + _join(doc.get("services") or []),
        "Equipment: "
        + _join(e.get("name") or _labels("equipment").get(e["code"], e["code"]) for e in doc.get("equipment") or []),
        "Insurance accepted: "
        + _join(_labels("insurers").get(i["insurer"], i["insurer"]) for i in doc.get("insurance") or []),
        "Accreditations: " + _join(a["body"] for a in doc.get("accreditations") or []),
        "Languages: " + _join(doc.get("languages") or []),
    ]
    return "\n".join(line for line in lines if line and not line.endswith(": "))


def practitioner_text(doc: dict, workplaces: list[str] | None = None) -> str:
    lines = [
        f"Doctor: {_name(doc)}",
        f"Title: {doc.get('title')}" if doc.get("title") else "",
        f"Grade: {doc.get('grade')}" if doc.get("grade") not in (None, "unknown") else "",
        f"Specialty: {_labels('specialties').get(doc.get('specialty'), doc.get('specialty'))}"
        if doc.get("specialty")
        else "",
        "Subspecialties: " + _join(doc.get("subspecialties") or []),
        "Qualifications: " + _join(doc.get("qualifications") or []),
        "Languages: " + _join(doc.get("languages") or []),
        "Works at: " + _join(workplaces or []),
    ]
    return "\n".join(line for line in lines if line and not line.endswith(": "))


def content_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()
