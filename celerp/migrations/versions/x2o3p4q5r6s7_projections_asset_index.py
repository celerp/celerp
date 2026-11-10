# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""An index on the asset a projection belongs to, so an asset's history (its movements)
is read without scanning every projection of its type.

A start of this version before the upgrade ran has already created the index from the
models, so it is created only when missing.

Revision ID: x2o3p4q5r6s7
Revises: w1n2o3p4q5r6
Create Date: 2026-10-16
"""

from alembic import op

revision = "x2o3p4q5r6s7"
down_revision = "w1n2o3p4q5r6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_projections_asset "
               "ON projections (company_id, entity_type, (state ->> 'asset_id'))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_projections_asset")
