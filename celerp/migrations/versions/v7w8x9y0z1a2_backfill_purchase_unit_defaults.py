# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Backfill purchase_unit and purchase_conversion_factor for existing items.

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-05-15

Context
-------
The purchase_unit paired cell feature (v1.x) added defaults at item creation
time, but existing items have NULL for these fields.

This migration backfills:
  - purchase_unit = sell_by  (if sell_by is set and purchase_unit is absent)
  - purchase_conversion_factor = 1  (if absent)

Items with no sell_by get purchase_unit left as NULL (intentional - it means
the purchase unit hasn't been configured yet, same as a new item with no
sell_by).
"""

from __future__ import annotations
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Backfill purchase_conversion_factor = 1 where missing
    conn.execute(sa.text("""
        UPDATE projections
        SET state = state::jsonb || '{"purchase_conversion_factor": 1}'::jsonb
        WHERE entity_type = 'item'
          AND (state::jsonb)->'purchase_conversion_factor' IS NULL
    """))
    # Backfill purchase_unit = sell_by where purchase_unit missing and sell_by present
    conn.execute(sa.text("""
        UPDATE projections
        SET state = state::jsonb || jsonb_build_object('purchase_unit', (state::jsonb)->>'sell_by')
        WHERE entity_type = 'item'
          AND (state::jsonb)->'purchase_unit' IS NULL
          AND (state::jsonb)->>'sell_by' IS NOT NULL
          AND (state::jsonb)->>'sell_by' != ''
    """))


def downgrade() -> None:
    pass
