# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Two-way accountant sync: each account of a connected company linked to the account it
posts to here, and the fix a held record needs.

Revision ID: b6u7v8w9x0y1
Revises: a5t6u7v8w9x0
"""
from alembic import op
import sqlalchemy as sa

revision = "b6u7v8w9x0y1"
down_revision = "a5t6u7v8w9x0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_peer_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_company_id", sa.Uuid(), nullable=False),
        sa.Column("other_account_id", sa.Text(), nullable=False),
        sa.Column("other_code", sa.Text(), nullable=False),
        sa.Column("other_name", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("how", sa.String(16), nullable=False),
        sa.Column("added_from", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "source_company_id", "other_account_id",
                            name="uq_company_peer_account_link"),
    )
    op.create_index("ix_company_peer_accounts_company", "company_peer_accounts", ["company_id"])
    op.add_column("company_peer_attention", sa.Column("fix", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("company_peer_attention", "fix")
    op.drop_index("ix_company_peer_accounts_company", table_name="company_peer_accounts")
    op.drop_table("company_peer_accounts")
