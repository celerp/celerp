# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

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

# In-Python edits avoid json->jsonb casts that fail on SQL_ASCII clusters.
# See https://github.com/celerp/celerp/issues/189
from celerp.migrations._json_compat import update_projection_state


# revision identifiers, used by Alembic.
revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Backfill purchase_conversion_factor = 1 where missing, and
    # purchase_unit = sell_by where purchase_unit missing and sell_by present.
    def _backfill(state, row):
        changed = False
        if state.get("purchase_conversion_factor") is None:
            state["purchase_conversion_factor"] = 1
            changed = True
        if state.get("purchase_unit") is None:
            sell_by = state.get("sell_by")
            if sell_by not in (None, ""):
                state["purchase_unit"] = sell_by
                changed = True
        return changed

    update_projection_state(conn, _backfill, where="entity_type = 'item'")


def downgrade() -> None:
    pass
