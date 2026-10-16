# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company integration keys can be rotated and extended.

Revision ID: a1r2o3t4a5t6
Revises: x2p3q4r5s6t7
"""
from alembic import op
import sqlalchemy as sa

revision = "a1r2o3t4a5t6"
down_revision = "x2p3q4r5s6t7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_peer_grants", sa.Column("lifetime_days", sa.Integer(), nullable=True))
    op.add_column("company_peer_grants", sa.Column("replaced_by_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE company_peer_grants SET lifetime_days = LEAST(90, GREATEST(1, "
        "CAST(ROUND(EXTRACT(EPOCH FROM (expires_at - created_at)) / 86400) AS INTEGER)))"
    )
    op.alter_column("company_peer_grants", "lifetime_days", nullable=False)


def downgrade() -> None:
    op.drop_column("company_peer_grants", "replaced_by_id")
    op.drop_column("company_peer_grants", "lifetime_days")
