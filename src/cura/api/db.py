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

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(120))
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


class Claim(Base):
    """ "I am this doctor" / "I manage this facility". Approved by a moderator after evidence checks."""

    __tablename__ = "claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(16))  # practitioner | facility
    subject_id: Mapped[str] = mapped_column(String(20), index=True)
    evidence: Mapped[dict] = mapped_column(JSONDoc, default=dict)  # licence no., work email, document url
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Change(Base):
    """Every write — applied or proposed — is a Change. Applied changes form the public change feed."""

    __tablename__ = "changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String(16), index=True)  # facility | practitioner | role
    subject_id: Mapped[str | None] = mapped_column(String(20), index=True)  # None until a create is applied
    op: Mapped[str] = mapped_column(String(8))  # create | update
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


def get_session() -> Iterator[Session]:
    engine()
    assert _Session is not None
    with _Session() as session:
        yield session
