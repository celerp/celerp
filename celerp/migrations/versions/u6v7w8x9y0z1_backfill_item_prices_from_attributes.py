# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
import sqlalchemy as sa


revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Promote every key ending in _price from attributes to top-level,
    # only when the top-level key doesn't already exist.
    conn.execute(sa.text("""
        UPDATE projections
        SET state = (
            SELECT jsonb_object_agg(key, value)
            FROM (
                -- Start with all existing top-level keys
                SELECT key, value FROM jsonb_each(state::jsonb)
                UNION ALL
                -- Add any _price keys from attributes that aren't already top-level
                SELECT attr_key, attr_val
                FROM jsonb_each((state::jsonb)->'attributes') AS t(attr_key, attr_val)
                WHERE attr_key LIKE '%_price'
                  AND (state::jsonb)->attr_key IS NULL
            ) AS merged
        )
        WHERE entity_type = 'item'
          AND state->'attributes' IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM jsonb_each((state::jsonb)->'attributes') AS t(k, v)
              WHERE k LIKE '%_price'
                AND (state::jsonb)->k IS NULL
          )
    """))


def downgrade() -> None:
    # Non-reversible: cannot distinguish promoted vs organically set prices.
    pass
