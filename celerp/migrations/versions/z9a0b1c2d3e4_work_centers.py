# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Create the work_centers table (manufacturing operational stations).

Revision ID: z9a0b1c2d3e4
Revises: y8z9a0b1c2d3
Create Date: 2026-06-14

A work centre is master data like a location (Bench, Polishing, Oven). It optionally carries a WIP
location, an hourly labour rate and a capacity (capacity reserved for later scheduling). Runs may
reference a work centre via work_center_id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "z9a0b1c2d3e4"
down_revision = "y8z9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_centers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("wip_location_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("labor_rate", sa.Float(), nullable=True),
        sa.Column("capacity", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "name", name="uq_work_center_company_name"),
    )


def downgrade() -> None:
    op.drop_table("work_centers")
