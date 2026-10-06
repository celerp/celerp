# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Keep the invoice a person recorded an unmatched online payment on, so every later
delivery of the payment, its refunds and its release goes there.

Revision ID: w0l1m2n3o4p5
Revises: v9k0l1m2n3o4
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa

revision = "w0l1m2n3o4p5"
down_revision = "v9k0l1m2n3o4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("unmatched_payments", sa.Column("recorded_company", sa.String(64), nullable=True))
    op.add_column("unmatched_payments", sa.Column("recorded_document", sa.String(255), nullable=True))
    op.create_index("ix_unmatched_payments_recorded_company", "unmatched_payments", ["recorded_company"])


def downgrade() -> None:
    op.drop_index("ix_unmatched_payments_recorded_company", table_name="unmatched_payments")
    op.drop_column("unmatched_payments", "recorded_document")
    op.drop_column("unmatched_payments", "recorded_company")
