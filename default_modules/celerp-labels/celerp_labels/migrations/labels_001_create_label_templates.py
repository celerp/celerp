"""Create label_templates table

Revision ID: labels_001
Revises:
Create Date: 2026-03-10

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "labels_001"
down_revision = None
branch_labels = ("celerp-labels",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "label_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("format", sa.String(50), nullable=False, server_default="40x30mm"),
        sa.Column("orientation", sa.String(20), nullable=False, server_default="portrait"),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("copies", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("label_templates")
