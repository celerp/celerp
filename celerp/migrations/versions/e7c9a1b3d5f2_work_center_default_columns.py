# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""work center hours_per_day + is_default columns and the one-default index

Revision ID: e7c9a1b3d5f2
Revises: d5e6f7a8b9c0
Create Date: 2026-08-06

Adds hours_per_day and is_default to work_centers and the partial unique index
that enforces one default center per company. Verifiable DDL only (op.*), so
extract_signatures detects it and the auto-stamp walker never false-stamps a
create_all/restore database past these columns. The data move (seeding a
Default center, the notice, stripping the old setting) lives in the child
data-backfill migration f8a9b0c1d2e3. Forward-only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7c9a1b3d5f2"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_centers", sa.Column("hours_per_day", sa.Float(), nullable=True))
    # NOT NULL with a transient server default so existing rows backfill to false,
    # then drop the default for parity with the model (celerp/models/company.py
    # declares none). is_default is added before the index that references it.
    op.add_column(
        "work_centers",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "uq_work_center_one_default",
        "work_centers",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.alter_column("work_centers", "is_default", server_default=None)


def downgrade() -> None:
    op.drop_index("uq_work_center_one_default", table_name="work_centers")
    op.drop_column("work_centers", "is_default")
    op.drop_column("work_centers", "hours_per_day")
