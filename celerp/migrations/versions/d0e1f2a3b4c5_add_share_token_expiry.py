"""add expires_at to doc_share_tokens

Revision ID: d0e1f2a3b4c5
Revises: e4f5a6b7c8d9
Create Date: 2026-07-12

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "d0e1f2a3b4c5"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("doc_share_tokens")]
    if "expires_at" not in cols:
        op.add_column("doc_share_tokens", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("doc_share_tokens")]
    if "expires_at" in cols:
        op.drop_column("doc_share_tokens", "expires_at")
