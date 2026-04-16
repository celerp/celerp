# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
# Target: PostgreSQL. Do not run against SQLite.
"""initial schema

Revision ID: fd5de461e14e
Revises:
Create Date: 2026-02-23 01:20:00.000000

Hand-written from model definitions. Covers all tables for the initial PostgreSQL deploy.
SQLite dev environment uses create_all() — Alembic is PostgreSQL-only.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fd5de461e14e"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- companies ---
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_company_slug", "companies", ["slug"], unique=True)

    # --- locations ---
    op.create_table(
        "locations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("address", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "name", name="uq_location_company_name"),
    )

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("auth_hash", sa.Text(), nullable=True),
        sa.Column("api_key", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "email", name="uq_user_company_email"),
    )
    op.create_index("uq_user_api_key", "users", ["api_key"], unique=True)

    # --- ledger ---
    op.create_table(
        "ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("location_id", sa.Uuid(as_uuid=True), sa.ForeignKey("locations.id"), nullable=True),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("uq_ledger_idempotency", "ledger", ["idempotency_key"], unique=True)
    op.create_index("idx_ledger_company_entity", "ledger", ["company_id", "entity_id"])
    op.create_index("idx_ledger_company_event_type", "ledger", ["company_id", "event_type"])
    op.create_index("idx_ledger_company_ts", "ledger", ["company_id", "ts"])
    op.create_index("idx_ledger_entity_type", "ledger", ["entity_type"])
    op.create_index("idx_ledger_ts", "ledger", ["ts"])

    # --- projections ---
    op.create_table(
        "projections",
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("location_id", sa.Uuid(as_uuid=True), sa.ForeignKey("locations.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=True),
        sa.Column("is_on_memo", sa.Boolean(), nullable=True),
        sa.Column("is_on_marketplace", sa.Boolean(), nullable=True),
        sa.Column("is_in_production", sa.Boolean(), nullable=True),
        sa.Column("is_expired", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("company_id", "entity_id"),
    )

    # --- user_companies ---
    op.create_table(
        "user_companies",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("user_id", "company_id", name="uq_user_company"),
    )


def downgrade() -> None:
    op.drop_table("user_companies")
    op.drop_table("projections")
    op.drop_table("ledger")
    op.drop_table("users")
    op.drop_table("locations")
    op.drop_table("companies")
