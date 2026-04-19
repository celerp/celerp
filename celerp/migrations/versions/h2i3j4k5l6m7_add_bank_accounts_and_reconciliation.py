# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""add bank_accounts and reconciliation_sessions tables

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-03-20 22:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "h2i3j4k5l6m7"
down_revision = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_accounts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("chart_account_code", sa.String(32), nullable=False),
        sa.Column("bank_name", sa.String(128), nullable=False),
        sa.Column("account_number", sa.String(64), nullable=False),
        sa.Column("bank_type", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("opening_balance", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_bank_account_company", "bank_accounts", ["company_id"])

    op.create_table(
        "reconciliation_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(as_uuid=True), sa.ForeignKey("bank_accounts.id"), nullable=False),
        sa.Column("statement_date", sa.String(16), nullable=False),
        sa.Column("statement_balance", sa.Numeric(20, 4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("reconciled_je_ids", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_recon_bank_account", "reconciliation_sessions", ["bank_account_id"])


def downgrade() -> None:
    op.drop_index("idx_recon_bank_account", table_name="reconciliation_sessions")
    op.drop_table("reconciliation_sessions")
    op.drop_index("idx_bank_account_company", table_name="bank_accounts")
    op.drop_table("bank_accounts")
