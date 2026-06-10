# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Backfill payment_date on payment JE projections and payment ledger events.

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2026-04-30

Context
-------
Bug A (pre-fix): ``create_for_doc_payment()`` never passed ``ts`` to
``_emit_auto_posted_je()``, so payment JE projections had no ``state["ts"]``.
``_build_balances()`` fell back to ``""`` for ``ts``, which is always less than
``date_from``, silently excluding every payment JE from date-filtered P&L/TB
reports.

This migration repairs the historical data for existing deployments:

1. **Ledger events** (``doc.payment.received``): set ``data.payment_date`` to
   ``DATE(ledger.ts)`` where ``payment_date`` is missing.

2. **JE projections** (``je:auto:*:pay:*``): set ``state.ts`` to
   ``DATE(ledger.ts)`` of the matching ``acc.journal_entry.created`` event where
   ``state.ts`` is missing.  We use the JE's own creation ledger event (which
   always has a real wall-clock ``ts``) rather than trying to cross-join to the
   payment event, which is simpler and equally correct.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# Revision metadata
# ---------------------------------------------------------------------------

revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    conn = op.get_bind()

    # 1. Backfill doc.payment.received ledger events missing payment_date:
    #    set data->>'payment_date' = DATE(ledger.ts) where the field is absent.
    conn.execute(sa.text("""
        UPDATE ledger
        SET data = jsonb_set(
            data::jsonb,
            '{payment_date}',
            to_jsonb(TO_CHAR(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD')),
            true
        )
        WHERE event_type = 'doc.payment.received'
          AND (
            data->>'payment_date' IS NULL
            OR data->>'payment_date' = ''
          )
    """))

    # 2. Backfill JE projections for payment JEs (je:auto:*:pay:*) missing
    #    state.ts, using DATE(l.ts) from the acc.journal_entry.created event.
    conn.execute(sa.text("""
        UPDATE projections AS p
        SET state = jsonb_set(
            p.state::jsonb,
            '{ts}',
            to_jsonb(TO_CHAR(l.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD')),
            true
        )
        FROM ledger l
        WHERE p.entity_id LIKE 'je:auto:%:pay:%'
          AND (
            p.state->>'ts' IS NULL
            OR p.state->>'ts' = ''
          )
          AND l.entity_id = p.entity_id
          AND l.event_type = 'acc.journal_entry.created'
          AND l.company_id = p.company_id
    """))


# ---------------------------------------------------------------------------
# Downgrade (no-op - data backfill is not reversible without original state)
# ---------------------------------------------------------------------------

def downgrade() -> None:
    pass
