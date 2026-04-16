"""add import_batches table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-09

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("import_batches"):
        op.create_table(
            "import_batches",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("entity_type", sa.String(64), nullable=False),
            sa.Column("filename", sa.Text(), nullable=True),
            sa.Column("row_count", sa.Integer(), nullable=False),
            sa.Column("entity_ids", sa.JSON(), nullable=False),
            sa.Column("idempotency_keys", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column(
                "imported_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("undone_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        )
        op.create_index("idx_import_batch_company", "import_batches", ["company_id"])


def downgrade() -> None:
    op.drop_index("idx_import_batch_company", table_name="import_batches")
    op.drop_table("import_batches")
