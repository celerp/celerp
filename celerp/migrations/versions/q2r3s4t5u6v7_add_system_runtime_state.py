"""Add system_runtime_state table

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-05-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "q2r3s4t5u6v7"
down_revision = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_runtime_state",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    # Seed the singleton row
    op.execute("INSERT INTO system_runtime_state (id, value) VALUES (1, '{}')")


def downgrade() -> None:
    op.drop_table("system_runtime_state")
