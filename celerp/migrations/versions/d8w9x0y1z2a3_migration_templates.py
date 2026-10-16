# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The posting accounts a person chose on their last migration from each source.

Revision ID: d8w9x0y1z2a3
Revises: c7v8w9x0y1z2
"""
from alembic import op
import sqlalchemy as sa

revision = "d8w9x0y1z2a3"
down_revision = "c7v8w9x0y1z2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "source_system", name="uq_migration_templates_user_source"),
    )


def downgrade() -> None:
    op.drop_table("migration_templates")
