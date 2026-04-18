# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""add sync_runs table

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2026-04-18 08:30:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "l6m7n8o9p0q1"
down_revision = "k5l6m7n8o9p0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(64), nullable=False),
        sa.Column("connector", sa.String(32), nullable=False),
        sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_runs_company_id", "sync_runs", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_sync_runs_company_id", table_name="sync_runs")
    op.drop_table("sync_runs")
