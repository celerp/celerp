# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backfill item projection state for legacy price_type/new_price format.

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2026-05-01

Context
-------
The demo seeder in versions before v1.0.10 stored prices inside the
``item.created`` event payload as ``price_type`` and ``new_price`` fields
(e.g. ``{"price_type": "cost_price", "new_price": 8500.0}``).

The ``item.created`` projection handler does ``current.update(data)``, so
these two fields landed verbatim in the item's projection state. The dashboard
valuation query reads ``state->>'cost_price'`` (top-level), which was never
set, so ``total_value_cost`` and ``total_value_retail`` always returned 0.

The new seeder (v1.0.10+) emits separate ``item.pricing.set`` events which
write the price to the correct top-level key. This migration repairs existing
item projections that are in the old format by promoting ``new_price`` to the
key named by ``price_type`` when that key is absent.

Only affects items where:
  - ``state`` contains a ``price_type`` string field, AND
  - ``state`` contains a ``new_price`` numeric field, AND
  - ``state`` does NOT yet have the key named by ``price_type`` set.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# Revision metadata
# ---------------------------------------------------------------------------

revision = "o9p0q1r2s3t4"
down_revision = "n8o9p0q1r2s3"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    conn = op.get_bind()

    # For each affected item projection, set state[price_type] = new_price.
    # price_type is a string like "cost_price"; new_price is a numeric JSON value.
    # Cast new_price to jsonb explicitly - jsonb_set requires jsonb for the value arg.
    conn.execute(sa.text("""
        UPDATE projections
        SET state = jsonb_set(
            state::jsonb,
            ARRAY[state->>'price_type'],
            (state->'new_price')::jsonb,
            true
        )
        WHERE entity_type = 'item'
          AND state->>'price_type' IS NOT NULL
          AND state->>'new_price' IS NOT NULL
          AND (state::jsonb)->(state->>'price_type') IS NULL
    """))


def downgrade() -> None:
    # Not reversible: we cannot distinguish a promoted price from one set by
    # item.pricing.set events. No-op downgrade is safe - projections can be
    # rebuilt from ledger events if needed.
    pass
