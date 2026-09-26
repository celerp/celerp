# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add sync_runs.attention_json for records a sync leaves to a person.

Revision ID: i6d7e8f9a0b1
Revises: h5c6d7e8f9a0
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "i6d7e8f9a0b1"
down_revision = "h5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sync_runs", sa.Column("attention_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_runs", "attention_json")
