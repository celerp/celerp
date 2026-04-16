"""add doc_share_tokens table

Revision ID: a1b2c3d4e5f6
Revises: fd5de461e14e
Create Date: 2026-03-05

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "a1b2c3d4e5f6"
down_revision = "fd5de461e14e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("doc_share_tokens"):
        op.create_table(
            "doc_share_tokens",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("token", sa.String(length=64), nullable=False, unique=True),
            sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("entity_id", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index("ix_doc_share_token", "doc_share_tokens", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_doc_share_token", table_name="doc_share_tokens")
    op.drop_table("doc_share_tokens")
