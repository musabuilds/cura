"""`cura demo seed` / `cura demo reset`: the pitch's cast, idempotent and removable without a trace.

Every demo record carries `"demo": true` and sits in the last hundred ids of the API-reserved range
(`store.DEMO_FLOOR`), so a reset finds them by id alone. Reset also removes everything the three demo
accounts created while the demo ran (records, roles, claims, events, webhooks, keys, sessions).

| account      | phone       | owns                                                              |
|--------------|-------------|-------------------------------------------------------------------|
| Musab        | 0790000001  | Dr. Musab Al-Jabiri (phone_match), Al-Jabiri Sports Medicine Clinic |
| Crest admin  | 0790000002  | Amman Crest Hospital (registered_phone)                           |
| new user     | 0790000003  | nothing: walks the onboarding from zero                           |
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from cura.api import contact, db, live, ratings, store
from cura.api.db import (
    App,
    AuthCode,
    AvailabilityEvent,
    Change,
    Claim,
    Embedding,
    FacilityApiKey,
    FacilityRow,
    Invitation,
    LoadSample,
    MergeCase,
    OtpChallenge,
    PractitionerPrivate,
    PractitionerRow,
    RecordVersion,
    RefreshToken,
    Review,
    ReviewReport,
    RoleRow,
    User,
    Webhook,
    WebhookDelivery,
    utcnow,
)

NOW = "2026-10-24T00:00:00Z"
QUALITY = {"completeness": 0.9, "confidence": 0.9, "last_verified_at": NOW}

HOSPITAL_ID = "JO-AM-HSP-99901"
CLINIC_ID = "JO-AM-CLN-99901"
MUSAB_ID = "JO-PRC-999901"
DUPLICATE_ID = "JO-PRC-999902"
MUSAB_PHONE, CREST_PHONE, NEW_PHONE = "+962790000001", "+962790000002", "+962790000003"
DEMO_PHONES = (MUSAB_PHONE, CREST_PHONE, NEW_PHONE)
USERS = {
    MUSAB_PHONE: ("usr_demo_musab", "Musab Al-Jabiri", "dr.musab@cura-demo.jo"),
    CREST_PHONE: ("usr_demo_crest_admin", "Amman Crest Hospital — Admin", "admin@crest-demo.cura.jo"),
    NEW_PHONE: ("usr_demo_new", None, None),
}


def _src(rid: str) -> list[dict]:
    return [{"source": "cura_demo", "ref": rid, "retrieved_at": NOW}]


HOSPITAL = {
    "id": HOSPITAL_ID,
    "type": "hospital",
    "ownership": "private",
    "name": {"en": "Amman Crest Hospital", "ar": "مستشفى تاج عمّان", "aliases": ["Crest Hospital"]},
    "address": {
        "governorate": "JO-AM",
        "city": "Amman",
        "neighborhood": "Abdoun",
        "street": "Prince Hashem bin Al Hussein Street",
        "building": "14",
        "text": "14 Prince Hashem bin Al Hussein Street, Abdoun, Amman",
    },
    "location": {"lat": 31.9446, "lon": 35.8853, "precision": "exact", "source": "cura_demo"},
    "contact": {
        "phones": ["+96265900100"],
        "emails": ["info@crest-demo.cura.jo"],
        "website": "https://crest-demo.cura.jo",
    },
    "hours": {"opening_hours": "24/7", "always_open": True},
    "emergency": True,
    "beds": 180,
    "specialties": [
        "emergency_medicine",
        "cardiology",
        "orthopedics",
        "sports_medicine",
        "pediatrics",
        "obstetrics_gynecology",
        "internal_medicine",
        "dermatology",
        "radiology",
    ],
    "departments": [
        "Emergency",
        "Cardiology",
        "Orthopaedics & Sports Medicine",
        "Paediatrics",
        "Obstetrics & Gynaecology",
        "Internal Medicine",
        "Dermatology",
        "Radiology",
    ],
    "services": ["24/7 emergency", "Cath lab", "MRI", "CT", "Physiotherapy", "Day surgery"],
    "insurance": [{"insurer": "globemed"}, {"insurer": "gig_insurance_jordan"}, {"insurer": "delta_insurance"}],
    "languages": ["ar", "en"],
    "amenities": {"parking": True, "wheelchair": True, "pharmacy": True},
    "identifiers": {"cura_demo": HOSPITAL_ID},
    "sources": _src(HOSPITAL_ID),
    "quality": QUALITY,
    "demo": True,
    "created_at": NOW,
    "updated_at": NOW,
}
CLINIC = {
    "id": CLINIC_ID,
    "type": "clinic",
    "ownership": "private",
    "name": {"en": "Al-Jabiri Sports Medicine Clinic", "ar": "عيادة الجابري للطب الرياضي", "aliases": []},
    "address": {
        "governorate": "JO-AM",
        "city": "Amman",
        "neighborhood": "Sweifieh",
        "street": "Wasfi Al-Tal Street",
        "floor": "3",
        "text": "Wasfi Al-Tal Street, 3rd floor, Sweifieh, Amman",
    },
    "location": {"lat": 31.9566, "lon": 35.8617, "precision": "exact", "source": "cura_demo"},
    "contact": {"phones": [MUSAB_PHONE], "emails": None, "website": None},
    "hours": {"opening_hours": "Su-Th 16:00-20:00; Sa 10:00-14:00"},
    "specialties": ["sports_medicine", "orthopedics"],
    "departments": ["Sports injuries", "Arthroscopy follow-up", "Rehabilitation"],
    "languages": ["ar", "en"],
    "identifiers": {"cura_demo": CLINIC_ID},
    "sources": _src(CLINIC_ID),
    "quality": QUALITY,
    "demo": True,
    "created_at": NOW,
    "updated_at": NOW,
}
MUSAB = {
    "id": MUSAB_ID,
    "name": {"en": "Musab Al-Jabiri", "ar": "مصعب الجابري", "aliases": []},
    "title": "Dr.",
    "gender": "male",
    "grade": "consultant",
    "specialty": "sports_medicine",
    "subspecialties": ["orthopedics", "sports injuries", "arthroscopy"],
    "languages": ["ar", "en"],
    "qualifications": [
        "MBBS, University of Jordan",
        "Jordanian Board of Orthopaedic Surgery",
        "Fellowship in Sports Medicine",
    ],
    "bio": {
        "en": "Consultant in sports medicine and orthopaedics. Treats athletes and active people: ligament and "
        "cartilage injuries, return-to-play plans, and non-surgical care first.",
        "ar": "استشاري الطب الرياضي وجراحة العظام. يعالج الرياضيين والنشطين: إصابات الأربطة والغضاريف، وخطط العودة "
        "إلى الملاعب، مع تقديم العلاج غير الجراحي أولاً.",
    },
    "identifiers": {"jmc": "JMC-24-11717", "cura_demo": MUSAB_ID},
    "sources": _src(MUSAB_ID),
    "quality": QUALITY,
    "demo": True,
    "created_at": NOW,
    "updated_at": NOW,
}
MUSAB_PRIVATE = {
    "phones": [
        {"number": MUSAB_PHONE, "label": "Clinic", "public": True},
        {"number": "+962790000009", "label": "Personal", "public": False},
    ],
    "email": "dr.musab@cura-demo.jo",
    "public_email": True,
}
DUPLICATE = {
    "id": DUPLICATE_ID,
    "name": {"en": "Mosab Jabri", "ar": "مصعب جابري", "aliases": []},
    "specialty": "orthopedics",
    "identifiers": {"cura_demo": DUPLICATE_ID},
    "sources": _src(DUPLICATE_ID),
    "quality": {"completeness": 0.2, "confidence": 0.4, "last_verified_at": NOW},
    "demo": True,
    "created_at": NOW,
    "updated_at": NOW,
}
# (id, en, ar, title, specialty, grade, languages, hours, fee)
CREST_DOCTORS = [
    ("JO-PRC-999903", "Lina Haddad", "لينا حداد", "cardiology", "consultant", "Su-Th 09:00-15:00", 35),
    ("JO-PRC-999904", "Kareem Nassar", "كريم نصار", "pediatrics", "specialist", "Su-Th 10:00-17:00", 25),
    ("JO-PRC-999905", "Rania Saleh", "رانيا صالح", "obstetrics_gynecology", "consultant", "Su,Tu,Th 09:00-14:00", 35),
    ("JO-PRC-999906", "Faisal Obeidat", "فيصل العبيدات", "emergency_medicine", "consultant", "24/7", None),
    ("JO-PRC-999907", "Dana Masri", "دانا المصري", "dermatology", "specialist", "Sa-Th 11:00-18:00", 30),
]


def _doctor(pid: str, en: str, ar: str, specialty: str, grade: str) -> dict:
    return {
        "id": pid,
        "name": {"en": en, "ar": ar, "aliases": []},
        "title": "Dr.",
        "grade": grade,
        "specialty": specialty,
        "languages": ["ar", "en"],
        "identifiers": {"cura_demo": pid},
        "sources": _src(pid),
        "quality": QUALITY,
        "demo": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _role(rid: str, pid: str, fid: str, specialty: str, hours: str | None, fee: float | None, **extra) -> dict:
    from cura import normalize as nz

    parsed = nz.parse_opening_hours(hours) if hours else {}
    doc = {
        "id": rid,
        "practitioner_id": pid,
        "facility_id": fid,
        "active": True,
        "period": {"start": "2024-01-01"},
        "confirmation": "confirmed",
        "specialty": specialty,
        "hours": {"opening_hours": hours, "weekly": parsed.get("weekly"), "always_open": parsed.get("always_open")}
        if hours
        else {},
        "consultation_fee": {"amount": fee, "currency": "JOD"} if fee is not None else None,
        "sources": _src(rid),
        "demo": True,
        "updated_at": NOW,
    }
    return doc | extra


def _invitation_hospital(s: Session) -> str | None:
    """The real registry hospital whose pending invitation Musab can confirm."""
    preferred = s.get(FacilityRow, "JO-AM-HSP-00001")
    if preferred is not None and preferred.type == "hospital" and preferred.status == "active":
        return preferred.id
    return s.scalar(
        select(FacilityRow.id)
        .where(
            FacilityRow.type == "hospital",
            FacilityRow.governorate == "JO-AM",
            FacilityRow.status == "active",
            FacilityRow.id < "JO-AM-HSP-90000",
        )
        .order_by(FacilityRow.id)
        .limit(1)
    )


def _user(s: Session, phone: str) -> User:
    uid, name, email = USERS[phone]
    user = s.scalar(select(User).where(User.phone == phone))
    if user is None:
        user = User(id=uid, email=f"phone:{phone}", password_hash="!", phone=phone)
        s.add(user)
    user.phone_verified_at = user.phone_verified_at or utcnow()
    if name and not user.display_name:
        user.display_name = name
    if email and not user.contact_email:
        user.contact_email = email
    s.flush()
    return user


def _claim(s: Session, user: User, kind: str, rid: str, method: str) -> Claim:
    claim = s.scalar(
        select(Claim).where(
            Claim.user_id == user.id, Claim.subject_type == kind, Claim.subject_id == rid, Claim.status == "approved"
        )
    )
    if claim is None:
        now = utcnow()
        claim = Claim(
            user_id=user.id,
            subject_type=kind,
            subject_id=rid,
            method=method,
            status="approved",
            level="verified",
            evidence={"demo": True},
            reviewed_at=now,
            verified_at=now,
            note="demo seed",
        )
        s.add(claim)
    return claim


def _event(s: Session, key: str, **fields) -> None:
    if s.scalar(select(AvailabilityEvent.id).where(AvailabilityEvent.idempotency_key == key)) is None:
        now = live.now()
        s.add(AvailabilityEvent(idempotency_key=key, starts_at=now - timedelta(hours=1), updated_at=utcnow(), **fields))


def seed() -> dict:
    db.init()
    with db.new_session() as s:
        for doc in (HOSPITAL, CLINIC):
            store.put(s, "facility", doc, bump=s.get(FacilityRow, doc["id"]) is not None, dirty=False, origin="seed")
        musab = MUSAB | {"contact": contact.project(MUSAB_PRIVATE)}
        people = [musab, DUPLICATE] + [_doctor(pid, en, ar, sp, gr) for pid, en, ar, sp, gr, _h, _f in CREST_DOCTORS]
        for doc in people:
            store.put(
                s, "practitioner", doc, bump=s.get(PractitionerRow, doc["id"]) is not None, dirty=False, origin="seed"
            )
        s.flush()
        contact.save(s, MUSAB_ID, MUSAB_PRIVATE, None)
        roles = [
            _role(
                "JO-ROL-9999901",
                MUSAB_ID,
                CLINIC_ID,
                "sports_medicine",
                "Su-Th 16:00-20:00; Sa 10:00-14:00",
                30,
                phones=[MUSAB_PHONE],
            ),
            _role("JO-ROL-9999902", MUSAB_ID, HOSPITAL_ID, "orthopedics", "Mo,We 09:00-13:00", 40),
        ]
        for i, (pid, _en, _ar, sp, _gr, hours, fee) in enumerate(CREST_DOCTORS, start=3):
            roles.append(_role(f"JO-ROL-99999{i:02d}", pid, HOSPITAL_ID, sp, hours, fee))
        invite_from = _invitation_hospital(s)
        if invite_from:
            roles.append(
                _role(
                    "JO-ROL-9999920",
                    MUSAB_ID,
                    invite_from,
                    "sports_medicine",
                    "Tu 14:00-18:00",
                    35,
                    confirmation="pending_practitioner",
                    period={"start": date.today().isoformat()},
                )
            )
        for doc in roles:
            existing = s.get(RoleRow, doc["id"])
            if existing is not None and existing.doc.get("confirmation") != doc["confirmation"]:
                continue  # the demo already moved it on (e.g. Musab accepted the invitation): leave it
            store.put(s, "role", doc, bump=existing is not None, dirty=False, origin="seed")
        s.flush()
        musab_user, crest_user, new_user = (_user(s, p) for p in DEMO_PHONES)
        _claim(s, musab_user, "practitioner", MUSAB_ID, "phone_match")
        _claim(s, musab_user, "facility", CLINIC_ID, "phone_match")
        _claim(s, crest_user, "facility", HOSPITAL_ID, "registered_phone")
        _event(
            s,
            "demo-seed-dana-conference",
            subject_type="practitioner",
            subject_id="JO-PRC-999907",
            practitioner_id="JO-PRC-999907",
            facility_id=None,
            state="unavailable",
            reason="conference",
            note="At the dermatology congress in Dubai. Back on Sunday.",
            ends_at=live.now() + timedelta(days=3),
        )
        _event(
            s,
            "demo-seed-faisal-oncall",
            subject_type="role",
            subject_id="JO-ROL-9999906",
            practitioner_id="JO-PRC-999906",
            facility_id=HOSPITAL_ID,
            state="available",
            reason="on_call",
            note="Leading the ER today",
            ends_at=live.now() + timedelta(hours=12),
        )
        s.commit()
        return {
            "facilities": {"amman_crest_hospital": HOSPITAL_ID, "al_jabiri_clinic": CLINIC_ID},
            "practitioners": {
                "musab": MUSAB_ID,
                "near_duplicate": DUPLICATE_ID,
                "crest_doctors": [d[0] for d in CREST_DOCTORS],
            },
            "pending_invitation": {"role_id": "JO-ROL-9999920", "facility_id": invite_from} if invite_from else None,
            "accounts": {
                p: USERS[p][0] if u is None else u.id
                for p, u in zip(DEMO_PHONES, (musab_user, crest_user, new_user), strict=True)
            },
            "demo_site": f"/demo/site/{HOSPITAL_ID}",
        }


def reset() -> dict:
    """Delete every demo record and everything the demo accounts did. Leaves the real registry as it was."""
    db.init()
    counts: dict[str, int] = {}
    with db.new_session() as s:
        users = set(s.scalars(select(User.id).where(User.phone.in_(DEMO_PHONES))))
        users |= {u for u in s.scalars(select(User.id).where(User.id.in_([v[0] for v in USERS.values()])))}
        created = (
            {
                kind: set(
                    s.scalars(
                        select(Change.subject_id).where(
                            Change.author_id.in_(users),
                            Change.op == "create",
                            Change.subject_type == kind,
                            Change.subject_id.is_not(None),
                        )
                    )
                )
                for kind in ("facility", "practitioner", "role")
            }
            if users
            else {"facility": set(), "practitioner": set(), "role": set()}
        )
        facilities = {i for i in s.scalars(select(FacilityRow.id)) if store.is_demo_id("facility", i)} | created[
            "facility"
        ]
        practitioners = {i for i in s.scalars(select(PractitionerRow.id)) if store.is_demo_id("practitioner", i)}
        practitioners |= created["practitioner"]
        roles = (
            set(
                s.scalars(
                    select(RoleRow.id).where(
                        or_(RoleRow.practitioner_id.in_(practitioners), RoleRow.facility_id.in_(facilities))
                    )
                )
            )
            | {i for i in s.scalars(select(RoleRow.id)) if store.is_demo_id("role", i)}
            | created["role"]
        )
        subjects = {"facility": facilities, "practitioner": practitioners, "role": roles}
        hooks = set(s.scalars(select(Webhook.id).where(Webhook.facility_id.in_(facilities))))

        def run(name: str, stmt) -> None:
            counts[name] = counts.get(name, 0) + (s.execute(stmt).rowcount or 0)

        run("webhook_deliveries", delete(WebhookDelivery).where(WebhookDelivery.webhook_id.in_(hooks)))
        run("webhooks", delete(Webhook).where(Webhook.id.in_(hooks)))
        run("api_keys", delete(FacilityApiKey).where(FacilityApiKey.facility_id.in_(facilities)))
        E = AvailabilityEvent
        run(
            "events",
            delete(E).where(
                or_(
                    E.practitioner_id.in_(practitioners),
                    E.facility_id.in_(facilities),
                    E.subject_id.in_(roles),
                    E.author_id.in_(users),
                    E.idempotency_key.like("demo-seed-%"),
                )
            ),
        )
        run(
            "load_samples",
            delete(LoadSample).where(or_(LoadSample.facility_id.in_(facilities), LoadSample.author_id.in_(users))),
        )
        run(
            "invitations",
            delete(Invitation).where(
                or_(
                    Invitation.facility_id.in_(facilities),
                    Invitation.practitioner_id.in_(practitioners),
                    Invitation.created_by.in_(users),
                )
            ),
        )
        review_scope = or_(
            and_(Review.subject_type == "facility", Review.subject_id.in_(facilities)),
            and_(Review.subject_type == "practitioner", Review.subject_id.in_(practitioners)),
            Review.user_id.in_(users),
        )
        touched = set(s.execute(select(Review.subject_type, Review.subject_id).where(Review.user_id.in_(users))).all())
        review_ids = set(s.scalars(select(Review.id).where(review_scope)))
        run(
            "review_reports",
            delete(ReviewReport).where(
                or_(ReviewReport.review_id.in_(review_ids), ReviewReport.reporter_id.in_(users))
            ),
        )
        run("reviews", delete(Review).where(Review.id.in_(review_ids)))
        run(
            "private_contact", delete(PractitionerPrivate).where(PractitionerPrivate.practitioner_id.in_(practitioners))
        )
        run(
            "claims",
            delete(Claim).where(
                or_(
                    Claim.user_id.in_(users),
                    and_(Claim.subject_type == "facility", Claim.subject_id.in_(facilities)),
                    and_(Claim.subject_type == "practitioner", Claim.subject_id.in_(practitioners)),
                )
            ),
        )
        run("merge_cases", delete(MergeCase).where(MergeCase.record_id.in_(facilities | practitioners)))
        for kind, ids in subjects.items():
            run("embeddings", delete(Embedding).where(Embedding.kind == kind, Embedding.record_id.in_(ids)))
            run("versions", delete(RecordVersion).where(RecordVersion.kind == kind, RecordVersion.record_id.in_(ids)))
        demo_changes = select(Change.id).where(
            or_(
                *[and_(Change.subject_type == k, Change.subject_id.in_(ids)) for k, ids in subjects.items()],
                and_(Change.author_id.in_(users), or_(Change.status != "applied", Change.subject_id.is_(None))),
            )
        )
        change_ids = set(s.scalars(demo_changes))
        # A demo account's applied edit to a real record stays in that record's history, unattributed.
        s.execute(update(RecordVersion).where(RecordVersion.change_id.in_(change_ids)).values(change_id=None))
        run("changes", delete(Change).where(Change.id.in_(change_ids)))
        s.execute(update(Change).where(Change.author_id.in_(users)).values(author_id=None))
        s.execute(update(Change).where(Change.reviewed_by.in_(users)).values(reviewed_by=None))
        run("roles", delete(RoleRow).where(RoleRow.id.in_(roles)))
        run("practitioners", delete(PractitionerRow).where(PractitionerRow.id.in_(practitioners)))
        run("facilities", delete(FacilityRow).where(FacilityRow.id.in_(facilities)))
        run("otp_challenges", delete(OtpChallenge).where(OtpChallenge.target.in_(DEMO_PHONES)))
        run("sessions", delete(RefreshToken).where(RefreshToken.user_id.in_(users)))
        s.execute(delete(AuthCode).where(AuthCode.user_id.in_(users)))
        s.execute(delete(App).where(App.owner_id.in_(users)))
        run("users", delete(User).where(User.id.in_(users)))
        for kind, rid in touched:
            if kind in ("facility", "practitioner") and rid not in subjects[kind]:
                ratings.recompute(s, kind, rid)
        s.commit()
    return {k: v for k, v in counts.items() if v}
