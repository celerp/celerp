# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backfill item projection prices buried inside the attributes dict.

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-05-15

Context
-------
The original demo seeder (before v1.0.9) stored prices inside the item's
``attributes`` dict (e.g. ``attributes.cost_price = 4200.0``).  Valuation
and the KPI endpoint both read top-level ``state.cost_price``, so all
demo-seeded items appeared to have zero cost/retail values.

Items received via PO or updated after the fix landed store prices correctly
at the top level, which is why e.g. a PO-received Ruby showed up correctly
while all other demo items did not.

This migration promotes any _price fields found inside ``state.attributes``
to the top level of ``state``, only when the top-level key is absent.

Only affects item projections where:
  - ``state.attributes`` contains a key ending in ``_price``, AND
  - the same key is absent at the top level of ``state``.
"""

from __future__ import annotations

from alembic import op

# In-Python edits avoid json->jsonb casts that fail on SQL_ASCII clusters.
# See https://github.com/celerp/celerp/issues/189
from celerp.migrations._json_compat import update_projection_state


revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Promote every key ending in _price from attributes to top-level,
    # only when the top-level key doesn't already exist.
    def _promote_attr_prices(state, row):
        attrs = state.get("attributes")
        if not isinstance(attrs, dict):
            return False
        changed = False
        for key, value in attrs.items():
            if key.endswith("_price") and state.get(key) is None:
                state[key] = value
                changed = True
        return changed

    update_projection_state(conn, _promote_attr_prices, where="entity_type = 'item'")


def downgrade() -> None:
    # Non-reversible: cannot distinguish promoted vs organically set prices.
    pass
