"""Persistence. Postgres in production, SQLite for local development and tests.

Each registry record is stored as its canonical JSON document plus a handful of extracted columns
used for filtering. The document is the contract; columns are indexes. `version` increments on every
applied change and is exposed as the ETag for optimistic concurrency.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from cura.api.settings import settings

JSONDoc = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    # Microsecond precision: the change feed pages on updated_at, so ties must be rare.
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


def prefixed_id(prefix: str):
    """Typed opaque ids (`usr_…`, `clm_…`): readable in logs, still 128 random bits."""

    def make() -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    return make


class Base(DeclarativeBase):
    pass


class FacilityRow(Base):
    __tablename__ = "facilities"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    ownership: Mapped[str] = mapped_column(String(16), index=True)
    governorate: Mapped[str | None] = mapped_column(String(5), index=True)
    search: Mapped[str] = mapped_column(Text, default="")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    doc: Mapped[dict] = mapped_column(JSONDoc)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)  # changed via API since last export
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (Index("ix_facilities_latlon", "lat", "lon"),)


class PractitionerRow(Base):
    __tablename__ = "practitioners"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    specialty: Mapped[str | None] = mapped_column(String(64), index=True)
    search: Mapped[str] = mapped_column(Text, default="")
    doc: Mapped[dict] = mapped_column(JSONDoc)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RoleRow(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id"), index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"), index=True)
    doc: Mapped[dict] = mapped_column(JSONDoc)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (UniqueConstraint("practitioner_id", "facility_id"),)


class User(Base):
    """An account. Developers sign in with email + password; doctors and facility staff with a verified
    mobile number (`phone`). A phone account has no password and its `email` column holds an
    unroutable `phone:+962…` placeholder (the column predates phone sign-in and is unique, not null);
    the address a phone user gives us is `contact_email`, private and never a login."""

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=prefixed_id("usr"))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)  # E.164, verified by OTP
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    # user < moderator < admin. Practitioner / facility rights come from approved claims, not roles.
    role: Mapped[str] = mapped_column(String(16), default="user")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class App(Base):
    """A third-party application (an OAuth client) registered by a developer."""

    __tablename__ = "apps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    client_id: Mapped[str] = mapped_column(String(64), unique=True)
    client_secret_hash: Mapped[str | None] = mapped_column(String(255))  # None → public client (PKCE only)
    api_key_prefix: Mapped[str] = mapped_column(String(16), unique=True)
    api_key_hash: Mapped[str] = mapped_column(String(64))
    redirect_uris: Mapped[list] = mapped_column(JSONDoc, default=list)
    allowed_scopes: Mapped[str] = mapped_column(String(255))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthCode(Base):
    __tablename__ = "auth_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(255))
    code_challenge: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class RefreshToken(Base):
    """Rotating refresh tokens. Reuse of a rotated token revokes its whole family (RFC 9700 §4.14)."""

    __tablename__ = "refresh_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotated: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    device_name: Mapped[str | None] = mapped_column(String(120))


class Claim(Base):
    """ "I am this doctor" / "I manage this facility". An approved claim is the only source of edit rights.

    `method` says how it was proven and `level` how strongly (see `cura.api.verification`):
    phone_match / registered_phone / work_email / website_token / facility_invite -> verified (instant);
    evidence / documents / with_profile -> reviewed (a moderator); created -> unverified (the account made
    the record itself and has proven nothing yet); dispute -> always a moderator.
    """

    __tablename__ = "claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=prefixed_id("clm"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(16))  # practitioner | facility
    subject_id: Mapped[str] = mapped_column(String(20), index=True)
    evidence: Mapped[dict] = mapped_column(JSONDoc, default=dict)  # licence no., work email, document url
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # evidence (a moderator checked it) | facility_invite (the facility vouched) | with_profile
    method: Mapped[str] = mapped_column(String(16), default="evidence", server_default="evidence")
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str | None] = mapped_column(String(12))  # unverified | pending | verified | reviewed
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Change(Base):
    """Every write — applied or proposed — is a Change. Applied changes form the public change feed."""

    __tablename__ = "changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String(16), index=True)  # facility | practitioner | role
    subject_id: Mapped[str | None] = mapped_column(String(20), index=True)  # None until a create is applied
    op: Mapped[str] = mapped_column(String(8))  # create | update | merge
    patch: Mapped[dict] = mapped_column(JSONDoc)  # RFC 7396 JSON Merge Patch
    reason: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|applied|rejected
    auto_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), default="cura_api")  # cura_api | import
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    review_note: Mapped[str | None] = mapped_column(Text)
    base_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    client_id: Mapped[str | None] = mapped_column(String(64))
    ratings: Mapped[dict] = mapped_column(JSONDoc)  # {dimension: 1..5}
    text: Mapped[str | None] = mapped_column(Text)
    visit_month: Mapped[str | None] = mapped_column(String(7))  # YYYY-MM; month only, for privacy
    status: Mapped[str] = mapped_column(String(16), default="visible")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("subject_type", "subject_id", "user_id"),)


class ReviewReport(Base):
    """A flag on a review. Owners cannot edit or delete reviews; they report them to a moderator."""

    __tablename__ = "review_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), index=True)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(24))  # spam | abusive | conflict_of_interest | personal_data | off_topic
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | upheld | dismissed
    resolved_by: Mapped[str | None] = mapped_column(String(36))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("review_id", "reporter_id"),)


class RecordVersion(Base):
    """Append-only history of every registry record.

    Each applied change writes the full new document and closes the previous version's interval, so
    the registry can answer "what were this doctor's hours on 1 July?" and a model can learn from how
    schedules move over seasons. Rows are never updated except to set `valid_to`, and never deleted.
    """

    __tablename__ = "record_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    record_id: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer)
    doc: Mapped[dict] = mapped_column(JSONDoc)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # None → current
    change_id: Mapped[int | None] = mapped_column(ForeignKey("changes.id"))
    origin: Mapped[str] = mapped_column(String(8))  # seed | api
    __table_args__ = (
        UniqueConstraint("kind", "record_id", "version"),
        Index("ix_record_versions_subject_time", "kind", "record_id", "valid_from"),
    )


class AvailabilityEvent(Base):
    """A time-bounded override of a schedule: a doctor on leave, a hospital closed for a storm.

    The regular schedule (`hours` on the role or facility) says when a place is normally open; events
    say what is actually happening. They are append-only: ending one early sets `ends_at`, withdrawing
    one sets `cancelled_at`, and nothing is deleted, so absence patterns stay learnable.

    `practitioner_id` and `facility_id` are denormalised from the subject so the resolver can fetch
    every event that touches a role (the role's own, its practitioner's, its facility's) in one query.
    """

    __tablename__ = "availability_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subject_type: Mapped[str] = mapped_column(String(16))  # practitioner | role | facility
    subject_id: Mapped[str] = mapped_column(String(20), index=True)
    practitioner_id: Mapped[str | None] = mapped_column(String(20), index=True)
    facility_id: Mapped[str | None] = mapped_column(String(20), index=True)
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(String(160))
    details: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # None → until changed
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)  # feed cursor
    __table_args__ = (
        UniqueConstraint("author_id", "idempotency_key"),
        Index("ix_availability_window", "starts_at", "ends_at"),
    )


class LoadSample(Base):
    """How busy a facility is at a moment (0–1), reported by its staff. Feeds `busy_hours` patterns."""

    __tablename__ = "load_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[float] = mapped_column(Float)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (Index("ix_load_samples_facility_at", "facility_id", "at"),)


class Invitation(Base):
    """A facility onboarding one of its doctors: a one-time link that lets an account own that profile.

    Cura does not send email. The facility's app delivers the link; only its hash is stored.
    """

    __tablename__ = "invitations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"), index=True)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id"), index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[str | None] = mapped_column(String(36))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OtpChallenge(Base):
    """A one-time code in flight: phone sign-in (`pc_…`) or proving control of a facility (`fc_…`).

    The code is never stored: `code_hash` is HMAC-SHA256 keyed with a server secret over the challenge id
    and the code, so a database leak does not become a table of six-digit codes to replay, and a hash
    from one challenge is useless for another. For `website_token` it is the hash of the token.
    """

    __tablename__ = "otp_challenges"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16))  # login | facility_claim
    target: Mapped[str] = mapped_column(String(254), index=True)  # E.164 phone, email, or website host
    channel: Mapped[str] = mapped_column(String(8))  # sms | voice | email | web
    code_hash: Mapped[str] = mapped_column(String(64))
    claim_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PractitionerPrivate(Base):
    """A doctor's full contact details, including numbers they did not publish. Owner-only.

    The record (`practitioners.doc.contact`) holds the public projection of this row and nothing else.
    """

    __tablename__ = "practitioner_private"
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id"), primary_key=True)
    contact: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MergeCase(Base):
    """A record created over the matcher's objection ("this may be the same doctor"), queued for a
    moderator with the candidates it resembled. Resolved by `POST /v1/{collection}/{id}/merge` or dismissed."""

    __tablename__ = "merge_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=prefixed_id("mc"))
    kind: Mapped[str] = mapped_column(String(16))
    record_id: Mapped[str] = mapped_column(String(20), index=True)
    candidates: Mapped[list] = mapped_column(JSONDoc, default=list)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open | merged | dismissed
    created_by: Mapped[str | None] = mapped_column(String(36))
    resolved_by: Mapped[str | None] = mapped_column(String(36))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Webhook(Base):
    """A facility's endpoint that receives signed change notifications. The signing secret is shown once
    and stored encrypted (Fernet): Cura has to sign with it, so a hash would not do."""

    __tablename__ = "webhooks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=prefixed_id("wh"))
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    events: Mapped[list] = mapped_column(JSONDoc, default=list)
    secret_enc: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=prefixed_id("dlv"))
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id"), index=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONDoc)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|delivered|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FacilityApiKey(Base):
    """A read-only key a facility gives its own website: identifies the caller and raises its rate limit.
    Only the SHA-256 is stored; `prefix` finds the row."""

    __tablename__ = "facility_api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=prefixed_id("key"))
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32), default="read")
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Security-relevant actions: sign-ins, lockouts, token reuse, ownership granted, keys and webhooks.
    Append-only. `detail` never holds a code, a token, a secret or a full phone number."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(16))
    subject_id: Mapped[str | None] = mapped_column(String(40))
    ip: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONDoc, default=dict)


class Vector(TypeDecorator):
    """A float32 embedding: pgvector's `vector(dim)` on Postgres, packed little-endian bytes elsewhere."""

    impl = LargeBinary
    cache_ok = True

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import VECTOR

            return dialect.type_descriptor(VECTOR(self.dim))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return np.asarray(value, dtype="<f4").tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return np.asarray(value, dtype=np.float32)
        return np.frombuffer(value, dtype="<f4")


EMBEDDING_DIM = 1024  # bge-m3. Changing the model's dimension needs a migration (the column is typed).


class Embedding(Base):
    """The semantic index: one vector per registry record, derived from its document.

    `source_version` is the record version the vector was computed from; the embedding worker
    re-embeds any record whose `version` has moved past it, and skips the model call when the
    embedded text (`content_hash`) did not change — an hours edit does not change what a place *is*.
    """

    __tablename__ = "embeddings"
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    model: Mapped[str] = mapped_column(String(80))
    source_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    vector: Mapped[np.ndarray] = mapped_column(Vector(EMBEDDING_DIM))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_engine = None
_Session: sessionmaker[Session] | None = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = settings().database_url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _fk_on(dbapi_conn, _):  # SQLite ignores foreign keys unless asked
                dbapi_conn.execute("PRAGMA foreign_keys=ON")

        _Session = sessionmaker(_engine, expire_on_commit=False)
        from cura.api import sync  # every committed registry write is announced (SSE, webhooks)

        sync.install(_Session)
    return _engine


def init() -> None:
    """Bring the schema to the latest migration (see `cura.api.migrate`)."""
    from cura.api import migrate

    migrate.upgrade(engine())


def reset_engine() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine, _Session = None, None


def new_session() -> Session:
    engine()
    assert _Session is not None
    return _Session()


def get_session() -> Iterator[Session]:
    engine()
    assert _Session is not None
    with _Session() as session:
        yield session
