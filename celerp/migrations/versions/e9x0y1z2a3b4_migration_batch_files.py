# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The files of a person's Move several companies uploads.

Revision ID: e9x0y1z2a3b4
Revises: d8w9x0y1z2a3
"""
from alembic import op
import sqlalchemy as sa

revision = "e9x0y1z2a3b4"
down_revision = "d8w9x0y1z2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_batch_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("prepared_by", sa.String(200), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=True),
        sa.Column("scan_claim_sha256", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_migration_batch_files_user_id", "migration_batch_files", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_migration_batch_files_user_id", table_name="migration_batch_files")
    op.drop_table("migration_batch_files")
