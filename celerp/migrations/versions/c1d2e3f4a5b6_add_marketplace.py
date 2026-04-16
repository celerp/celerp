"""add marketplace_configs table

Revision ID: c1d2e3f4a5b6
Revises: b2c3d4e5f6a7
Create Date: 2026-03-15

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "c1d2e3f4a5b6"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # marketplace_configs was included in the initial schema migration.
    # This migration is a no-op for fresh installs; it exists only to keep
    # the revision chain intact for instances that were created between the
    # initial schema and the explicit marketplace migration.
    bind = op.get_bind()
    if not inspect(bind).has_table("marketplace_configs"):
        op.create_table(
            "marketplace_configs",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("provider", sa.Text(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("company_id", "name", name="uq_marketplace_company_name"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("marketplace_configs"):
        op.drop_table("marketplace_configs")
