# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Remember each unsettled request to close a company's online payments, each
System Recovery restore Celerp Cloud must learn of, and each online payment that
could not be recorded on its invoice.

Revision ID: q4f5a6b7c8d9
Revises: p3e4f5a6b7c8
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "q4f5a6b7c8d9"
down_revision = "p3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_closures",
        sa.Column("operation_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("target_company", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payment_closures_target_company", "payment_closures", ["target_company"])
    op.create_table(
        "payment_recoveries",
        sa.Column("recovery_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_ids", sa.JSON, nullable=False),
        sa.Column("payments_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "unmatched_payments",
        sa.Column("reference", sa.String(255), primary_key=True),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("former_company", sa.String(64), nullable=False),
        sa.Column("document", sa.String(255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("unmatched_payments")
    op.drop_table("payment_recoveries")
    op.drop_index("ix_payment_closures_target_company", table_name="payment_closures")
    op.drop_table("payment_closures")
