# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The files of a Xero migration's documents, fetched from Xero after the import.

Revision ID: c7v8w9x0y1z2
Revises: b6u7v8w9x0y1
"""
from alembic import op
import sqlalchemy as sa

revision = "c7v8w9x0y1z2"
down_revision = "b6u7v8w9x0y1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_attachment_fetches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("migration_run_id", sa.Uuid(), sa.ForeignKey("migration_runs.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("started_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("cloud_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("organisation", sa.String(200), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("bound_tenant_id", sa.String(64), nullable=True),
        sa.Column("bound_organisation", sa.String(200), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("migration_attachment_fetches")
