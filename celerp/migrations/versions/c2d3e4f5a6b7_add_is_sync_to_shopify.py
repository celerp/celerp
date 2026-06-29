"""add is_sync_to_shopify to projections

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-29

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("projections")]
    if "is_sync_to_shopify" not in cols:
        op.add_column("projections", sa.Column("is_sync_to_shopify", sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("projections")]
    if "is_sync_to_shopify" in cols:
        op.drop_column("projections", "is_sync_to_shopify")
