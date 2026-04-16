"""add password reset token columns to users

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-03-15

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("users")}
    if "reset_token" not in cols:
        op.add_column("users", sa.Column("reset_token", sa.String(64), nullable=True))
    if "reset_token_expires" not in cols:
        op.add_column("users", sa.Column("reset_token_expires", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "reset_token_expires")
    op.drop_column("users", "reset_token")
