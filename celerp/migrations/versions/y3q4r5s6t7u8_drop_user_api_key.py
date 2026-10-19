# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Remove per-user API keys; companies connect through expiring peer keys.

Revision ID: y3q4r5s6t7u8
Revises: x2p3q4r5s6t7
"""
from alembic import op
import sqlalchemy as sa

revision = "y3q4r5s6t7u8"
down_revision = "x2p3q4r5s6t7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dropping the column drops its unique index or constraint, whichever name the
    # database was given (migrated chains name it uq_user_api_key, created schemas do not).
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS api_key")


def downgrade() -> None:
    # The keys themselves are not restored: none was ever accepted for sign-in.
    op.add_column("users", sa.Column("api_key", sa.String(255), nullable=True))
    op.create_index("uq_user_api_key", "users", ["api_key"], unique=True)
