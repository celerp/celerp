# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record online payment pages Celerp Cloud cancelled unpaid.

Revision ID: s6h7c8d9e0f1
Revises: r5g6b7c8d9e0
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa

revision = "s6h7c8d9e0f1"
down_revision = "r5g6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expired_payments",
        sa.Column("reference", sa.String(255), primary_key=True),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("company", sa.String(64), nullable=False),
        sa.Column("document", sa.String(255), nullable=False),
        sa.Column("age_days", sa.Integer, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_expired_payments_company_document", "expired_payments", ["company", "document"])


def downgrade() -> None:
    op.drop_index("ix_expired_payments_company_document", table_name="expired_payments")
    op.drop_table("expired_payments")
