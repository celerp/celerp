# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add created_at column to projections table.

Revision ID: x7y8z9a0b1c2
Revises: w8x9y0z1a2b3
Create Date: 2026-05-27

Context
-------
created_at for all projection-backed entities (items, docs, contacts, lists, etc.)
is now authoritative from the Projection table, not from event data state blobs.

Changes:
1. Add created_at: TIMESTAMP WITH TIME ZONE, nullable (NULL = legacy row).
2. Backfill: set created_at = MIN(ledger.ts) per entity_id+company_id.
   LedgerEntry.ts is a DB-stamped column, so this is non-forgeable.
3. Any row with no ledger entry (shouldn't happen) keeps created_at = NULL;
   ProjectionEngine sets created_at = now() on first INSERT for new entities.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "x7y8z9a0b1c2"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projections",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill from the minimum ledger_entry timestamp per entity
    op.execute(
        """
        UPDATE projections
        SET created_at = (
            SELECT MIN(le.ts)
            FROM ledger le
            WHERE le.entity_id = projections.entity_id
              AND le.company_id = projections.company_id
        )
        """
    )


def downgrade() -> None:
    op.drop_column("projections", "created_at")
