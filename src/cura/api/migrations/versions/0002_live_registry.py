"""live registry: history, availability, onboarding, review reports, semantic index

The embeddings table uses pgvector on Postgres (extension + HNSW cosine index) and packed float32
bytes on SQLite, where search falls back to an exact scan.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 15:14:22.642977
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from cura.api.db import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "embeddings",
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("record_id", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("vector", Vector(1024), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("kind", "record_id"),
    )
    if postgres:
        # Approximate nearest neighbours by cosine distance; m/ef_construction are pgvector's defaults.
        op.execute("CREATE INDEX ix_embeddings_vector_hnsw ON embeddings USING hnsw (vector vector_cosine_ops)")
    op.create_table(
        "availability_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=20), nullable=False),
        sa.Column("practitioner_id", sa.String(length=20), nullable=True),
        sa.Column("facility_id", sa.String(length=20), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=24), nullable=True),
        sa.Column("note", sa.String(length=160), nullable=True),
        sa.Column(
            "details", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("author_id", "idempotency_key"),
    )
    with op.batch_alter_table("availability_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_availability_events_author_id"), ["author_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_availability_events_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_availability_events_facility_id"), ["facility_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_availability_events_practitioner_id"), ["practitioner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_availability_events_subject_id"), ["subject_id"], unique=False)
        batch_op.create_index("ix_availability_window", ["starts_at", "ends_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_availability_events_updated_at"), ["updated_at"], unique=False)

    op.create_table(
        "invitations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("facility_id", sa.String(length=20), nullable=False),
        sa.Column("practitioner_id", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_by", sa.String(length=36), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["facilities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["practitioner_id"],
            ["practitioners.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_invitations_facility_id"), ["facility_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_invitations_practitioner_id"), ["practitioner_id"], unique=False)

    op.create_table(
        "load_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("facility_id", sa.String(length=20), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.Float(), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["facilities.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("load_samples", schema=None) as batch_op:
        batch_op.create_index("ix_load_samples_facility_at", ["facility_id", "at"], unique=False)

    op.create_table(
        "record_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("record_id", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("doc", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("change_id", sa.Integer(), nullable=True),
        sa.Column("origin", sa.String(length=8), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_id"],
            ["changes.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "record_id", "version"),
    )
    with op.batch_alter_table("record_versions", schema=None) as batch_op:
        batch_op.create_index("ix_record_versions_subject_time", ["kind", "record_id", "valid_from"], unique=False)

    op.create_table(
        "review_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("reporter_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_by", sa.String(length=36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id", "reporter_id"),
    )
    with op.batch_alter_table("review_reports", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_review_reports_review_id"), ["review_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_review_reports_status"), ["status"], unique=False)

    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.add_column(sa.Column("method", sa.String(length=16), server_default="evidence", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.drop_column("method")

    with op.batch_alter_table("review_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_review_reports_status"))
        batch_op.drop_index(batch_op.f("ix_review_reports_review_id"))

    op.drop_table("review_reports")
    with op.batch_alter_table("record_versions", schema=None) as batch_op:
        batch_op.drop_index("ix_record_versions_subject_time")

    op.drop_table("record_versions")
    with op.batch_alter_table("load_samples", schema=None) as batch_op:
        batch_op.drop_index("ix_load_samples_facility_at")

    op.drop_table("load_samples")
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invitations_practitioner_id"))
        batch_op.drop_index(batch_op.f("ix_invitations_facility_id"))

    op.drop_table("invitations")
    with op.batch_alter_table("availability_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_availability_events_updated_at"))
        batch_op.drop_index("ix_availability_window")
        batch_op.drop_index(batch_op.f("ix_availability_events_subject_id"))
        batch_op.drop_index(batch_op.f("ix_availability_events_practitioner_id"))
        batch_op.drop_index(batch_op.f("ix_availability_events_facility_id"))
        batch_op.drop_index(batch_op.f("ix_availability_events_created_at"))
        batch_op.drop_index(batch_op.f("ix_availability_events_author_id"))

    op.drop_table("availability_events")
    op.drop_table("embeddings")
