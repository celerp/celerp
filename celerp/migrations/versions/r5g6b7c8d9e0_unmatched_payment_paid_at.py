# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record when an unmatched online payment was paid, and stop recording when a
restored backup started (Celerp Cloud now delivers every payment again).

Revision ID: r5g6b7c8d9e0
Revises: s4t5u6v7w8x9
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa

revision = "r5g6b7c8d9e0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("unmatched_payments", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("payment_recoveries", "payments_since")


def downgrade() -> None:
    op.add_column("payment_recoveries", sa.Column("payments_since", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("unmatched_payments", "paid_at")
