# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # For each affected item projection, set state[price_type] = new_price.
        # price_type is a string like "cost_price"; new_price is a numeric JSON value.
        # We use jsonb_set with the price_type value as the path element.
        # The sub-select identifies rows where state->price_type is null/missing.
        conn.execute(sa.text("""
            UPDATE projections
            SET state = jsonb_set(
                state::jsonb,
                ARRAY[state->>'price_type'],
                (state->'new_price'),
                true
            )
            WHERE entity_type = 'item'
              AND state->>'price_type' IS NOT NULL
              AND state->>'new_price' IS NOT NULL
              AND state->(state->>'price_type') IS NULL
        """))
    else:
        # SQLite / test environments: Python-level update
        import json
        rows = conn.execute(sa.text("""
            SELECT company_id, entity_id, state
            FROM projections
            WHERE entity_type = 'item'
        """)).fetchall()
        for company_id, entity_id, raw_state in rows:
            state = json.loads(raw_state) if isinstance(raw_state, str) else raw_state
            if state is None:
                continue
            price_type = state.get("price_type")
            new_price = state.get("new_price")
            if price_type and new_price is not None and state.get(price_type) is None:
                state[price_type] = float(new_price)
                conn.execute(sa.text("""
                    UPDATE projections
                    SET state = :state
                    WHERE company_id = :company_id AND entity_id = :entity_id
                """), {"state": json.dumps(state), "company_id": company_id, "entity_id": entity_id})


def downgrade() -> None:
    # Not reversible: we cannot distinguish a promoted price from one set by
    # item.pricing.set events. No-op downgrade is safe - projections can be
    # rebuilt from ledger events if needed.
    pass
