"""baseline: schema of v0.1.0

Revision ID: 0001
Revises: -
Create Date: 2026-10-24 15:11:31.654692
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "facilities",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("ownership", sa.String(length=16), nullable=False),
        sa.Column("governorate", sa.String(length=5), nullable=True),
        sa.Column("search", sa.Text(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("doc", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("facilities", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_facilities_governorate"), ["governorate"], unique=False)
        batch_op.create_index("ix_facilities_latlon", ["lat", "lon"], unique=False)
        batch_op.create_index(batch_op.f("ix_facilities_ownership"), ["ownership"], unique=False)
        batch_op.create_index(batch_op.f("ix_facilities_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_facilities_type"), ["type"], unique=False)
        batch_op.create_index(batch_op.f("ix_facilities_updated_at"), ["updated_at"], unique=False)

    op.create_table(
        "practitioners",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("specialty", sa.String(length=64), nullable=True),
        sa.Column("search", sa.Text(), nullable=False),
        sa.Column("doc", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("practitioners", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_practitioners_specialty"), ["specialty"], unique=False)
        batch_op.create_index(batch_op.f("ix_practitioners_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_practitioners_updated_at"), ["updated_at"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "apps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=255), nullable=True),
        sa.Column("api_key_prefix", sa.String(length=16), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "redirect_uris",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("allowed_scopes", sa.String(length=255), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_prefix"),
        sa.UniqueConstraint("client_id"),
    )
    with op.batch_alter_table("apps", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_apps_owner_id"), ["owner_id"], unique=False)

    op.create_table(
        "auth_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_table(
        "changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=20), nullable=True),
        sa.Column("op", sa.String(length=8), nullable=False),
        sa.Column(
            "patch", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("auto_applied", sa.Boolean(), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("changes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_changes_applied_at"), ["applied_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_changes_author_id"), ["author_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_changes_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_changes_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_changes_subject_id"), ["subject_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_changes_subject_type"), ["subject_type"], unique=False)

    op.create_table(
        "claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=20), nullable=False),
        sa.Column(
            "evidence", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_claims_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_claims_subject_id"), ["subject_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_claims_user_id"), ["user_id"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated", sa.Boolean(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_refresh_tokens_family_id"), ["family_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_refresh_tokens_user_id"), ["user_id"], unique=False)

    op.create_table(
        "reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column(
            "ratings", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("visit_month", sa.String(length=7), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_type", "subject_id", "user_id"),
    )
    with op.batch_alter_table("reviews", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reviews_subject_id"), ["subject_id"], unique=False)

    op.create_table(
        "roles",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("practitioner_id", sa.String(length=20), nullable=False),
        sa.Column("facility_id", sa.String(length=20), nullable=False),
        sa.Column("doc", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["facilities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["practitioner_id"],
            ["practitioners.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("practitioner_id", "facility_id"),
    )
    with op.batch_alter_table("roles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_roles_facility_id"), ["facility_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_roles_practitioner_id"), ["practitioner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_roles_updated_at"), ["updated_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("roles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_roles_updated_at"))
        batch_op.drop_index(batch_op.f("ix_roles_practitioner_id"))
        batch_op.drop_index(batch_op.f("ix_roles_facility_id"))

    op.drop_table("roles")
    with op.batch_alter_table("reviews", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reviews_subject_id"))

    op.drop_table("reviews")
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_user_id"))
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_family_id"))

    op.drop_table("refresh_tokens")
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_claims_user_id"))
        batch_op.drop_index(batch_op.f("ix_claims_subject_id"))
        batch_op.drop_index(batch_op.f("ix_claims_status"))

    op.drop_table("claims")
    with op.batch_alter_table("changes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_changes_subject_type"))
        batch_op.drop_index(batch_op.f("ix_changes_subject_id"))
        batch_op.drop_index(batch_op.f("ix_changes_status"))
        batch_op.drop_index(batch_op.f("ix_changes_created_at"))
        batch_op.drop_index(batch_op.f("ix_changes_author_id"))
        batch_op.drop_index(batch_op.f("ix_changes_applied_at"))

    op.drop_table("changes")
    op.drop_table("auth_codes")
    with op.batch_alter_table("apps", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_apps_owner_id"))

    op.drop_table("apps")
    op.drop_table("users")
    with op.batch_alter_table("practitioners", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_practitioners_updated_at"))
        batch_op.drop_index(batch_op.f("ix_practitioners_status"))
        batch_op.drop_index(batch_op.f("ix_practitioners_specialty"))

    op.drop_table("practitioners")
    with op.batch_alter_table("facilities", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_facilities_updated_at"))
        batch_op.drop_index(batch_op.f("ix_facilities_type"))
        batch_op.drop_index(batch_op.f("ix_facilities_status"))
        batch_op.drop_index(batch_op.f("ix_facilities_ownership"))
        batch_op.drop_index("ix_facilities_latlon")
        batch_op.drop_index(batch_op.f("ix_facilities_governorate"))

    op.drop_table("facilities")
