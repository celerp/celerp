# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Two-way accountant sync: the revision of each record sent to a connected company with a
key, and each record from it that was declined, so neither is repeated until it changes.

Revision ID: a5t6u7v8w9x0
Revises: z4r5s6t7u8v9
"""
from alembic import op
import sqlalchemy as sa

revision = "a5t6u7v8w9x0"
down_revision = "z4r5s6t7u8v9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_peer_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("refused", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "grant_id", "entity", "entity_id",
                            name="uq_company_peer_delivery_record"),
    )


def downgrade() -> None:
    op.drop_table("company_peer_deliveries")
