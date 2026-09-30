# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add migration_runs.source_lock_date, the source's accounting lock date installed when
the migration finishes, and migration_cleanup_tasks.attachment, the one stored file an
attachment cleanup task names.

Revision ID: n1c2d3e4f5a6
Revises: m0b1c2d3e4f5
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "n1c2d3e4f5a6"
down_revision = "m0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("migration_runs", sa.Column("source_lock_date", sa.Date(), nullable=True))
    op.add_column("migration_cleanup_tasks", sa.Column("attachment", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("migration_cleanup_tasks", "attachment")
    op.drop_column("migration_runs", "source_lock_date")
