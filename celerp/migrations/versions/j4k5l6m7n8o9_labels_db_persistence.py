# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""labels: DB persistence, custom dims, barcode/QR field types

Revision ID: j4k5l6m7n8o9
Revises: h2i3j4k5l6m7
Create Date: 2026-03-21 04:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "j4k5l6m7n8o9"
down_revision = "h2i3j4k5l6m7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "label_templates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("format", sa.String(50), nullable=False, server_default="40x30mm"),
        sa.Column("orientation", sa.String(20), nullable=False, server_default="portrait"),
        sa.Column("width_mm", sa.Float, nullable=True),
        sa.Column("height_mm", sa.Float, nullable=True),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("copies", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_label_template_company", "label_templates", ["company_id"])


def downgrade() -> None:
    op.drop_index("idx_label_template_company", table_name="label_templates")
    op.drop_table("label_templates")
