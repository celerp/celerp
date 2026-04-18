# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""add connector_configs and outbound_queue tables

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
Create Date: 2026-04-18 10:10:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "m7n8o9p0q1r2"
down_revision = "l6m7n8o9p0q1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(64), nullable=False),
        sa.Column("connector", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False, server_default="both"),
        sa.Column("sync_frequency", sa.String(16), nullable=False, server_default="realtime"),
        sa.Column("daily_sync_hour", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("webhook_ids_json", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.String(128), nullable=True),
        sa.Column("last_daily_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "connector", name="uq_connector_config"),
    )
    op.create_index("ix_connector_configs_company_id", "connector_configs", ["company_id"])

    op.create_table(
        "outbound_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(64), nullable=False),
        sa.Column("connector", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbound_queue_company_id", "outbound_queue", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_outbound_queue_company_id", table_name="outbound_queue")
    op.drop_table("outbound_queue")
    op.drop_index("ix_connector_configs_company_id", table_name="connector_configs")
    op.drop_table("connector_configs")
