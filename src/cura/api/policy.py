"""Write governance: who may change which field, and whether a change applies now or waits for review.

Three outcomes for every write:

* **apply**   — the caller has authority over the record and the fields (verified owner, moderator).
* **propose** — anyone signed in may suggest; a moderator reviews it.
* **reject**  — the field is system-owned (ids, provenance, quality, ratings) and nobody writes it.

Identity-defining fields (name, type, location, ownership, specialty) always go through review, even
for owners: they are what other apps join on, and a typo there breaks every consumer.
"""

from __future__ import annotations

import copy
from typing import Literal

from cura.api.errors import Problem

Decision = Literal["apply", "propose"]

SYSTEM = {
    "id",
    "identifiers",
    "sources",
    "quality",
    "rating",
    "created_at",
    "updated_at",
    "replaced_by",
    "license_verified",
    "confirmation",  # POST /v1/roles/{id}/confirmation
    "practitioner_id",
    "facility_id",
}

OWNER_EDITABLE = {
    "facility": {
        "contact",
        "hours",
        "services",
        "departments",
        "equipment",
        "insurance",
        "prices",
        "amenities",
        "languages",
        "emergency",
        "beds",
        "specialties",
        "busy_hours",  # the facility's own declared busy times; live load comes from /load samples
    },
    "practitioner": {"title", "languages", "qualifications", "subspecialties"},
    # Either side owns a role: the doctor, or the facility's admin (who can end it when a doctor leaves).
    "role": {"hours", "consultation_fee", "insurance", "phones", "active", "period", "profile_url", "specialty"},
}
REVIEWED = {
    "facility": {"name", "type", "ownership", "operator", "parent", "address", "location", "status", "accreditations"},
    "practitioner": {"name", "specialty", "grade", "gender", "status"},
    "role": set(),
}


def merge_patch(target: dict, patch: dict) -> dict:
    """RFC 7396 JSON Merge Patch."""
    out = copy.deepcopy(target)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_patch(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def check_fields(kind: str, patch: dict) -> None:
    if not patch:
        raise Problem(422, "Empty patch", "Send at least one field to change.")
    forbidden = sorted(set(patch) & SYSTEM)
    if forbidden:
        raise Problem(
            422, "Read-only fields", f"These fields are maintained by Cura and cannot be written: {forbidden}"
        )
    unknown = sorted(set(patch) - OWNER_EDITABLE[kind] - REVIEWED[kind])
    if unknown:
        raise Problem(422, "Unknown fields", f"Not writable on a {kind}: {unknown}")
    if (
        kind == "facility"
        and isinstance(patch.get("status"), str)
        and patch["status"] not in ("active", "temporarily_closed", "permanently_closed")
    ):
        raise Problem(422, "Invalid status", "status must be active, temporarily_closed or permanently_closed")


def decide(kind: str, patch: dict, *, is_moderator: bool, owns: bool) -> Decision:
    check_fields(kind, patch)
    if is_moderator:
        return "apply"
    if owns and set(patch) <= OWNER_EDITABLE[kind]:
        return "apply"
    return "propose"
