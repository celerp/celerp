# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add connector_sources for the store each company's connector records came from.

Revision ID: j7e8f9a0b1c2
Revises: i6d7e8f9a0b1
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "j7e8f9a0b1c2"
down_revision = "i6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_sources",
        sa.Column("company_id", sa.String(64), primary_key=True),
        sa.Column("connector", sa.String(32), primary_key=True),
        sa.Column("store_handle", sa.Text(), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("connector_sources")
