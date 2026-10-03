# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Keep the refunds of an online payment that cannot be applied yet with the
unmatched payments.

Revision ID: s6h7c8d9e0f1
Revises: r5g6b7c8d9e0
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "s6h7c8d9e0f1"
down_revision = "r5g6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unmatched_refunds",
        sa.Column("refund_id", sa.String(255), primary_key=True),
        sa.Column("transition", sa.String(16), primary_key=True),
        sa.Column("reference", sa.String(255), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("former_company", sa.String(64), nullable=False),
        sa.Column("document", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context", sa.JSON, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_unmatched_refunds_reference", "unmatched_refunds", ["reference"])


def downgrade() -> None:
    op.drop_index("ix_unmatched_refunds_reference", table_name="unmatched_refunds")
    op.drop_table("unmatched_refunds")
