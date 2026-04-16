"""add consignment_flag to projections

Revision ID: g1h2i3j4k5l6
Revises: f1a2b3c4d5e6
Create Date: 2026-03-20

"""

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "g1h2i3j4k5l6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("projections")]
    if "consignment_flag" not in cols:
        op.add_column("projections", sa.Column("consignment_flag", sa.String(16), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("projections")]
    if "consignment_flag" in cols:
        op.drop_column("projections", "consignment_flag")
