"""Canonical data model.

The model follows HL7 FHIR R4 resource boundaries so Cura can be exposed as FHIR later without a
remodel: Facility ≈ Organization+Location, Practitioner ≈ Practitioner, Role ≈ PractitionerRole.

Unknown vs. none
----------------
`null` always means **unknown** — no source has stated it. An empty list means a source stated
there are none. A tri-state `bool | None` is `None` until someone knows. Apps should render `null` as
"unknown", never as "no".
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^JO-(AM|BA|AZ|MD|IR|MA|JA|AJ|KA|AT|MN|AQ|XX)-[A-Z]{3}-\d{5}$"
PRACTITIONER_ID_PATTERN = r"^JO-PRC-\d{6}$"
ROLE_ID_PATTERN = r"^JO-ROL-\d{7}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)


class FacilityType(StrEnum):
    hospital = "hospital"
    clinic = "clinic"
    primary_care_center = "primary_care_center"
    doctor_office = "doctor_office"
    dental_clinic = "dental_clinic"
    pharmacy = "pharmacy"
    laboratory = "laboratory"
    imaging_center = "imaging_center"
    blood_bank = "blood_bank"
    rehabilitation = "rehabilitation"
    optician = "optician"
    dialysis_center = "dialysis_center"
    emergency_medical_service = "emergency_medical_service"
    other = "other"


class Ownership(StrEnum):
    moh = "moh"
    rms = "rms"
    university = "university"
    other_gov = "other_gov"
    private = "private"
    ngo = "ngo"
    unrwa = "unrwa"
    unknown = "unknown"


class Status(StrEnum):
    active = "active"
    temporarily_closed = "temporarily_closed"
    permanently_closed = "permanently_closed"
    merged = "merged"  # tombstone: see `replaced_by`
    unknown = "unknown"


class Grade(StrEnum):
    consultant = "consultant"
    specialist = "specialist"
    resident = "resident"
    general_practitioner = "general_practitioner"
    unknown = "unknown"


class Confirmation(StrEnum):
    """Whether both sides agree that a practitioner works at a facility."""

    confirmed = "confirmed"
    pending_facility = "pending_facility"  # the doctor added this workplace; its verified admin has not confirmed
    pending_practitioner = "pending_practitioner"  # the facility added a claimed doctor who has not confirmed
    declined = "declined"


class LocationPrecision(StrEnum):
    exact = "exact"  # surveyed point / building (OSM, the facility itself)
    approximate = "approximate"  # neighbourhood-level or user-reported
    unknown = "unknown"


class Money(Strict):
    amount: Annotated[float, Field(ge=0)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "JOD"


class Name(Strict):
    en: str | None = None
    ar: str | None = None
    aliases: list[str] = []


class Address(Strict):
    governorate: Annotated[str | None, Field(pattern=r"^JO-[A-Z]{2}$")] = None
    district: str | None = None  # liwa (ADM2)
    city: str | None = None
    neighborhood: str | None = None
    street: str | None = None
    building: str | None = None
    floor: str | None = None
    postcode: str | None = None
    text: str | None = None  # the address as published, verbatim


class Location(Strict):
    lat: Annotated[float, Field(ge=29.0, le=33.5)]
    lon: Annotated[float, Field(ge=34.8, le=39.4)]
    precision: LocationPrecision = LocationPrecision.unknown
    source: str | None = None


class Contact(Strict):
    phones: list[str] | None = None  # E.164
    emails: list[str] | None = None
    website: str | None = None


class TimeRange(Strict):
    day: Annotated[str, Field(pattern=r"^(mo|tu|we|th|fr|sa|su)$")]
    opens: Annotated[str, Field(pattern=r"^\d{2}:\d{2}$")]
    closes: Annotated[str, Field(pattern=r"^\d{2}:\d{2}$")]


class Hours(Strict):
    opening_hours: str | None = None  # OSM opening_hours syntax (https://wiki.openstreetmap.org/wiki/Key:opening_hours)
    weekly: list[TimeRange] | None = None
    always_open: bool | None = None
    text: str | None = None  # as published when it could not be parsed
    observes_public_holidays: bool | None = None


class Period(Strict):
    start: date | None = None
    end: date | None = None  # the last day; a role that ended is kept, never deleted


class BusyLevel(Strict):
    day: Annotated[str, Field(pattern=r"^(mo|tu|we|th|fr|sa|su)$")]
    hour: Annotated[int, Field(ge=0, le=23)]
    level: Annotated[float, Field(ge=0, le=1)]


class Equipment(Strict):
    code: str  # data/reference/equipment.csv, or "other"
    name: str | None = None
    count: int | None = None
    available: bool | None = None


class Accreditation(Strict):
    body: str  # JCI, HCAC, CAP, ISO 15189 ...
    status: str = "accredited"
    year: int | None = None
    valid_until: date | None = None


class InsuranceLink(Strict):
    insurer: str  # data/reference/insurers.csv code
    network: str | None = None  # tier/network name when the insurer has several
    source: str | None = None


class PriceItem(Strict):
    item: str
    item_ar: str | None = None
    category: str = "other"  # consultation | procedure | test | imaging | bed_day | other
    price: Money | None = None
    min: Money | None = None
    max: Money | None = None
    effective_from: date | None = None
    source: str | None = None


class RatingSummary(Strict):
    """Aggregated from Cura's own reviews (`/v1/.../reviews`). Never copied from Google."""

    overall: float | None = None
    count: int = 0
    dimensions: dict[str, float] = {}


class SourceRef(Strict):
    source: str
    ref: str | None = None
    url: str | None = None
    retrieved_at: datetime | None = None


class Quality(Strict):
    completeness: Annotated[float, Field(ge=0, le=1)]  # share of core fields known
    confidence: Annotated[float, Field(ge=0, le=1)]  # corroboration across independent sources
    last_verified_at: datetime | None = None


class Bio(Strict):
    """A doctor's own words about their practice, in each language. Public."""

    en: Annotated[str | None, Field(max_length=2000)] = None
    ar: Annotated[str | None, Field(max_length=2000)] = None


class PublicPhone(Strict):
    number: Annotated[str, Field(pattern=r"^\+[1-9]\d{6,14}$")]  # E.164
    label: Annotated[str | None, Field(max_length=40)] = None


class PractitionerContact(Strict):
    """Only what the doctor chose to publish. Private numbers and a private email never enter the record
    (they live in `practitioner_private`, readable by the owner only), so they cannot leak through a
    read, the change feed, the history or the open dataset."""

    phones: list[PublicPhone] | None = None
    email: Annotated[str | None, Field(max_length=254)] = None


class Facility(Strict):
    id: Annotated[str, Field(pattern=ID_PATTERN)]
    status: Status = Status.active
    replaced_by: str | None = None
    type: FacilityType
    name: Name
    ownership: Ownership = Ownership.unknown
    operator: str | None = None
    parent: str | None = None  # chain / group
    address: Address = Address()
    location: Location | None = None
    contact: Contact = Contact()
    hours: Hours = Hours()
    emergency: bool | None = None
    beds: int | None = None
    specialties: list[str] | None = None  # data/reference/specialties.csv codes
    departments: list[str] | None = None
    services: list[str] | None = None
    equipment: list[Equipment] | None = None
    insurance: list[InsuranceLink] | None = None
    accreditations: list[Accreditation] | None = None
    prices: list[PriceItem] | None = None
    languages: list[str] | None = None
    amenities: dict[str, bool | str] = {}
    busy_hours: list[BusyLevel] | None = None
    rating: RatingSummary = RatingSummary()
    identifiers: dict[str, str] = {}  # crosswalk: {"osm": "node/1", "wikidata": "Q1", "moh": "..."}
    sources: list[SourceRef] = []
    quality: Quality
    demo: bool | None = None  # seeded by `cura demo seed`; removed by `cura demo reset`
    created_at: datetime
    updated_at: datetime


class Practitioner(Strict):
    id: Annotated[str, Field(pattern=PRACTITIONER_ID_PATTERN)]
    status: Status = Status.active
    replaced_by: str | None = None
    name: Name
    title: str | None = None
    gender: str | None = None  # only when a source states it
    grade: Grade = Grade.unknown
    specialty: str | None = None
    subspecialties: list[str] | None = None
    languages: list[str] | None = None
    qualifications: list[str] | None = None
    bio: Bio | None = None
    photo_url: Annotated[str | None, Field(max_length=500, pattern=r"^https://[^\s<>\"']+$")] = None
    contact: PractitionerContact | None = None  # public parts only, see PractitionerContact
    license_verified: bool = False  # set by the API when a claim is approved
    rating: RatingSummary = RatingSummary()
    identifiers: dict[str, str] = {}
    sources: list[SourceRef] = []
    quality: Quality
    demo: bool | None = None
    created_at: datetime
    updated_at: datetime


class Role(Strict):
    """A practitioner working at a facility — one per (practitioner, facility) pair.

    A doctor leaving a facility ends the role (`active: false`, `period.end`); the record stays so the
    history of who worked where, and when, survives.
    """

    id: Annotated[str, Field(pattern=ROLE_ID_PATTERN)]
    practitioner_id: Annotated[str, Field(pattern=PRACTITIONER_ID_PATTERN)]
    facility_id: Annotated[str, Field(pattern=ID_PATTERN)]
    active: bool | None = True
    period: Period | None = None
    confirmation: Confirmation = Confirmation.confirmed
    specialty: str | None = None
    hours: Hours = Hours()
    consultation_fee: Money | None = None
    insurance: list[InsuranceLink] | None = None
    phones: list[str] | None = None
    profile_url: str | None = None
    sources: list[SourceRef] = []
    demo: bool | None = None
    updated_at: datetime
