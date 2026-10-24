"""Who works where: facility rosters, role confirmation, and onboarding doctors by invitation.

Two ways a doctor's account comes to own their record:

* **Self-onboarding** — the doctor finds themselves (`GET /v1/practitioners/match`) and claims the
  record with evidence (`POST /v1/claims`); a moderator checks the licence.
* **Facility onboarding** — a facility's verified admin invites a doctor on its roster
  (`POST /v1/facilities/{id}/invitations`). The link is the proof: whoever accepts it owns the profile,
  vouched for by the facility. One link, one use, fourteen days.

A doctor who leaves is never deleted from a roster. Their role ends (`active: false`, `period.end`)
and stays in the record's history.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cura.api import matching, security, store, writes
from cura.api.db import Claim, Invitation, RoleRow, get_session, utcnow
from cura.api.deps import Principal, owned_subjects, require_user
from cura.api.errors import Problem
from cura.api.settings import settings

router = APIRouter(prefix="/v1")
INVITE_TTL = timedelta(days=14)


class ConfirmIn(BaseModel):
    accept: bool


class EndIn(BaseModel):
    end_date: date | None = Field(None, description="The last working day. Default: today")
    reason: Annotated[str | None, Field(max_length=2000)] = None


class RosterAddIn(BaseModel):
    practitioner_id: str | None = Field(None, description="An existing practitioner (find one with /match)")
    practitioner: dict[str, Any] | None = Field(
        None, description="A new practitioner record, when the doctor is not in Cura yet"
    )
    role: dict[str, Any] = Field(default_factory=dict, description="Role fields: specialty, hours, fee, phones …")
    confirm_new: bool = Field(False, description="Create the new practitioner even though similar ones exist")


class InviteIn(BaseModel):
    practitioner_id: str
    email: EmailStr | None = Field(None, description="Kept so the admin can see who was invited; Cura sends nothing")


class AcceptIn(BaseModel):
    token: str


def _facility_admin(db: Session, p: Principal, facility_id: str) -> dict:
    row = store.get(db, "facility", facility_id)
    if row is None:
        raise Problem(404, "Facility not found", facility_id)
    if not p.is_moderator and facility_id not in owned_subjects(db, p.user)["facility"]:
        raise Problem(403, "Not your facility", "Only the facility's verified admin can manage its roster.")
    return row.doc


# --- roles ---------------------------------------------------------------------------------------


@router.post("/roles/{role_id}/confirmation", tags=["roster"])
def confirm_role(
    role_id: str,
    body: ConfirmIn,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """The side that did not add the role accepts or declines it. Declining ends it; it is kept."""
    row = store.get(db, "role", role_id)
    if row is None:
        raise Problem(404, "Role not found", role_id)
    doc, owned = row.doc, owned_subjects(db, p.user)
    waiting_on = {"pending_facility": "facility", "pending_practitioner": "practitioner"}.get(doc.get("confirmation"))
    if waiting_on is None:
        raise Problem(409, "Nothing to confirm", f"The role is {doc.get('confirmation')}.")
    if not p.is_moderator and doc[f"{waiting_on}_id"] not in owned[waiting_on]:
        raise Problem(403, "Not yours to confirm", f"Waiting on the {waiting_on}'s verified owner.")
    patch: dict = {"confirmation": "confirmed"} if body.accept else {"confirmation": "declined", "active": False}
    change = writes.record_change(db, p, "role", role_id, "update", patch, base_version=row.version)
    record = writes.apply_update(db, "role", row, patch, change.id)
    writes.mark_applied(change)
    db.commit()
    return {"status": "applied", "change_id": change.id, "record": record}


@router.post("/roles/{role_id}/end", tags=["roster"])
def end_role(
    role_id: str,
    body: EndIn,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """The doctor left this facility. Either side may record it; the role and its history stay."""
    row = store.get(db, "role", role_id)
    if row is None:
        raise Problem(404, "Role not found", role_id)
    if not (p.is_moderator or writes.owns(db, p, "role", row.doc)):
        raise Problem(403, "Not your record", "Only the doctor or the facility's admin can end this role.")
    if row.doc.get("active") is False:
        raise Problem(409, "Role already ended")
    end = body.end_date or date.today()
    start = (row.doc.get("period") or {}).get("start")
    patch = {"active": False, "period": {"start": start, "end": end.isoformat()}}
    change = writes.record_change(db, p, "role", role_id, "update", patch, reason=body.reason, base_version=row.version)
    record = writes.apply_update(db, "role", row, patch, change.id)
    writes.mark_applied(change)
    db.commit()
    return {"status": "applied", "change_id": change.id, "record": record}


@router.post("/facilities/{facility_id}/practitioners", status_code=201, tags=["roster"])
def add_to_roster(
    facility_id: str,
    body: RosterAddIn,
    response: Response,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """A facility admin adds a doctor: an existing practitioner, or a new one (checked for duplicates).
    A doctor who left and returns has their old role reactivated, not a second one created."""
    _facility_admin(db, p, facility_id)
    if bool(body.practitioner_id) == bool(body.practitioner):
        raise Problem(422, "Choose one", "Send either practitioner_id or practitioner.")
    created = None
    if body.practitioner is not None:
        writes.check_create("practitioner", body.practitioner)
        names = [(body.practitioner.get("name") or {}).get(k) for k in ("en", "ar")]
        candidates = matching.practitioner_matches(
            db, names=names, specialty=body.practitioner.get("specialty"), facility_id=facility_id
        )
        if candidates and not body.confirm_new:
            raise Problem(
                409,
                "Possible duplicate",
                "This doctor may already be in Cura. Add them by practitioner_id, or resend with confirm_new: true.",
                candidates=candidates,
            )
        change = writes.record_change(db, p, "practitioner", None, "create", body.practitioner)
        created = writes.apply_create(db, "practitioner", body.practitioner, change.id)
        change.subject_id = created["id"]
        writes.mark_applied(change)
        pid = created["id"]
    else:
        pid = body.practitioner_id
        prac = store.get(db, "practitioner", pid)
        if prac is None:
            raise Problem(404, "Practitioner not found", pid)
        if prac.doc.get("status") == "merged":
            raise Problem(409, "Practitioner was merged", f"Use {prac.doc.get('replaced_by')} instead.")
    existing = db.scalar(select(RoleRow).where(RoleRow.practitioner_id == pid, RoleRow.facility_id == facility_id))
    if existing is not None:
        if existing.doc.get("active") is not False and existing.doc.get("confirmation") != "declined":
            raise Problem(409, "Already on the roster", existing.id, existing_id=existing.id)
        patch = body.role | {"active": True, "period": {"start": date.today().isoformat(), "end": None}}
        system = {"confirmation": _confirmation(db, p, pid, facility_id)}
        writes.check_update("role", existing.doc, patch | system)
        change = writes.record_change(
            db, p, "role", existing.id, "update", patch | system, base_version=existing.version
        )
        role = writes.apply_update(db, "role", existing, patch | system, change.id)
        response.status_code = 200
    else:
        record = body.role | {"practitioner_id": pid, "facility_id": facility_id}
        record.setdefault("period", {"start": date.today().isoformat()})
        writes.check_create("role", record)
        change = writes.record_change(db, p, "role", None, "create", record)
        role = writes.apply_create(
            db, "role", record, change.id, system={"confirmation": _confirmation(db, p, pid, facility_id)}
        )
        change.subject_id = role["id"]
    writes.mark_applied(change)
    db.commit()
    return {"status": "applied", "practitioner_created": created, "role": role}


def _confirmation(db: Session, p: Principal, practitioner_id: str, facility_id: str) -> str:
    """A doctor with their own verified account agrees before a facility lists them. A facility that has
    not proven itself yet (created by its owner, unverified) cannot list anyone on its own say-so."""
    if p.is_moderator or practitioner_id in owned_subjects(db, p.user)["practitioner"]:
        return "confirmed"
    if writes.has_owner(db, "practitioner", practitioner_id) or writes.unverified_owner(db, p, "facility", facility_id):
        return "pending_practitioner"
    return "confirmed"


# --- invitations ---------------------------------------------------------------------------------


def _invite_out(inv: Invitation) -> dict:
    status = (
        "accepted"
        if inv.accepted_at
        else "revoked"
        if inv.revoked_at
        else "expired"
        if _aware(inv.expires_at) <= utcnow()
        else "open"
    )
    return {
        "id": inv.id,
        "facility_id": inv.facility_id,
        "practitioner_id": inv.practitioner_id,
        "email": inv.email,
        "status": status,
        "expires_at": inv.expires_at,
        "created_at": inv.created_at,
        "accepted_at": inv.accepted_at,
    }


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


@router.post("/facilities/{facility_id}/invitations", status_code=201, tags=["roster"])
def invite(
    facility_id: str,
    body: InviteIn,
    p: Principal = Depends(require_user("practitioner:manage")),
    db: Session = Depends(get_session),
) -> dict:
    """Invite a doctor on this facility's roster to own their Cura profile. The `token` is shown once:
    deliver the `url` to the doctor (email, SMS, WhatsApp). Accepting it needs a Cura account."""
    _facility_admin(db, p, facility_id)
    if writes.unverified_owner(db, p, "facility", facility_id):
        raise Problem(
            403,
            "Verify the facility first",
            "An invitation hands over a doctor's profile; prove you run "
            "this facility (POST /v1/facilities/{id}/claims) before sending one.",
        )
    role = db.scalar(
        select(RoleRow).where(RoleRow.practitioner_id == body.practitioner_id, RoleRow.facility_id == facility_id)
    )
    if role is None or not store.is_listed(role.doc):
        raise Problem(422, "Not on your roster", "Add the doctor to the facility first, then invite them.")
    if writes.has_owner(db, "practitioner", body.practitioner_id):
        raise Problem(409, "Profile already claimed", "This doctor already manages their own profile.")
    token = security.opaque_token()
    inv = Invitation(
        token_hash=security.sha256(token),
        facility_id=facility_id,
        practitioner_id=body.practitioner_id,
        email=str(body.email).lower() if body.email else None,
        created_by=p.user.id,
        expires_at=utcnow() + INVITE_TTL,
    )
    db.add(inv)
    db.commit()
    return _invite_out(inv) | {"token": token, "url": f"{settings().public_base_url}/invite/{token}"}


@router.get("/facilities/{facility_id}/invitations", tags=["roster"])
def list_invitations(
    facility_id: str, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    _facility_admin(db, p, facility_id)
    rows = db.scalars(
        select(Invitation).where(Invitation.facility_id == facility_id).order_by(Invitation.created_at.desc())
    ).all()
    return {"data": [_invite_out(i) for i in rows], "count": len(rows)}


@router.post("/invitations/{invitation_id}/revoke", tags=["roster"])
def revoke_invitation(
    invitation_id: str, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    inv = db.get(Invitation, invitation_id)
    if inv is None:
        raise Problem(404, "Invitation not found")
    _facility_admin(db, p, inv.facility_id)
    if inv.accepted_at or inv.revoked_at:
        raise Problem(409, "Invitation is closed", _invite_out(inv)["status"])
    inv.revoked_at = utcnow()
    db.commit()
    return _invite_out(inv)


@router.post("/invitations/accept", tags=["roster"])
def accept_invitation(
    body: AcceptIn, p: Principal = Depends(require_user("practitioner:manage")), db: Session = Depends(get_session)
) -> dict:
    """The doctor, signed in, accepts: their account now owns the profile, vouched for by the facility."""
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == security.sha256(body.token)))
    if inv is None or _invite_out(inv)["status"] != "open":
        raise Problem(404, "Invitation not valid", "It may have expired, been used or been revoked.")
    if writes.has_owner(db, "practitioner", inv.practitioner_id):
        raise Problem(409, "Profile already claimed", "Someone already manages this profile.")
    if owned_subjects(db, p.user)["practitioner"]:
        raise Problem(409, "You already own a profile", "One account manages one doctor profile.")
    now = utcnow()
    claim = Claim(
        user_id=p.user.id,
        subject_type="practitioner",
        subject_id=inv.practitioner_id,
        evidence={"invitation_id": inv.id, "facility_id": inv.facility_id},
        status="approved",
        method="facility_invite",
        reviewed_by=inv.created_by,
        reviewed_at=now,
        note=f"vouched for by {inv.facility_id}",
    )
    db.add(claim)
    inv.accepted_by, inv.accepted_at = p.user.id, now
    db.commit()
    return {"claim_id": claim.id, "practitioner_id": inv.practitioner_id, "facility_id": inv.facility_id}
