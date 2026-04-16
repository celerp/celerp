"""add is_active to companies

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-03-15

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("companies")}
    if "is_active" not in cols:
        op.add_column(
            "companies",
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.create_index("ix_companies_is_active", "companies", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_companies_is_active", table_name="companies")
    op.drop_column("companies", "is_active")
