"""A doctor's contact details: the full set is private to the owner, the record holds the public part.

    PATCH /v1/practitioners/{id}  {"patch": {"contact": {"phones": [{"number": "0795550101", "label": "clinic",
                                                                     "public": true},
                                                                    {"number": "0790000009", "public": false}],
                                                         "email": "dr@example.jo", "public_email": false}}}

The private row is written first; the patch that reaches the `Change` log, the record, its history,
the change feed and the open dataset is replaced by the projection (public phones, and the email only
when `public_email`). Nothing private is ever in a document, so no read path can leak it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from cura.api import guard, policy
from cura.api.db import PractitionerPrivate, utcnow
from cura.api.errors import Problem


class PhoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: Annotated[str, Field(min_length=6, max_length=32)]
    label: Annotated[str | None, Field(max_length=40)] = None
    public: bool = False

    @field_validator("number")
    @classmethod
    def _e164(cls, v: str) -> str:
        e164 = guard.normalise_phone(v)
        if not e164:
            raise ValueError("not a valid phone number")
        return e164


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phones: Annotated[list[PhoneIn] | None, Field(max_length=5)] = None
    email: EmailStr | None = None
    public_email: bool = False


def private_contact(db: Session, practitioner_id: str) -> dict | None:
    row = db.get(PractitionerPrivate, practitioner_id)
    return row.contact if row and row.contact else None


def project(contact: dict | None) -> dict | None:
    """The public part: what the doctor chose to publish, in the record's `PractitionerContact` shape."""
    if not contact:
        return None
    phones = [{"number": p["number"], "label": p.get("label")} for p in contact.get("phones") or [] if p.get("public")]
    email = contact.get("email") if contact.get("public_email") else None
    if not phones and not email:
        return None
    return {"phones": phones or None, "email": email}


def prepare(current: dict | None, patch: dict | None) -> dict:
    """Validate the private contact that results from merging `patch` into `current`."""
    merged = policy.merge_patch(current or {}, patch) if isinstance(patch, dict) else {}
    try:
        return ContactIn.model_validate(merged).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise Problem(
            422,
            "Invalid contact",
            str(exc.errors()[0]["msg"]),
            errors=[{"field": "contact." + ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()],
        ) from exc


def save(db: Session, practitioner_id: str, clean: dict, user_id: str | None) -> None:
    row = db.get(PractitionerPrivate, practitioner_id)
    if row is None:
        row = PractitionerPrivate(practitioner_id=practitioner_id)
        db.add(row)
    row.contact, row.updated_by, row.updated_at = clean, user_id, utcnow()


def apply(db: Session, practitioner_id: str, patch: dict | None, user_id: str | None) -> dict | None:
    """Merge `patch` into the private contact, store it, and return the new public projection."""
    clean = prepare(private_contact(db, practitioner_id), patch)
    save(db, practitioner_id, clean, user_id)
    return project(clean)
