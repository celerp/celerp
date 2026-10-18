# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The journal entries as the ledger reads them.

An entry voided in place posts nothing. An entry voided out of a locked period stays
posted in its own period and its reversal posts on ``reversed_on``
(posting_dates.void_reversal), so every report and reconciliation sees the same lines.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection

# The id of the reversal row journal_rows adds for an entry voided out of a locked period.
REVERSAL_SUFFIX = ":reversal"


async def journal_rows(
    session: AsyncSession, company_id: uuid.UUID, *, include_void: bool = False
) -> list[tuple[str, dict, str]]:
    """All journal entry projections as (entity_id, state, date) tuples.

    date is the entry's effective ISO date (YYYY-MM-DD, "" when the entry has none).
    Posted entries only by default; include_void=True adds voided entries for views
    that must show the full record (the journal). Deliberately not date-filtered:
    the general ledger buckets one scan into opening (before date_from) and period
    ranges, so callers apply their own date filtering.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()
    out: list[tuple[str, dict, str]] = []
    for row in rows:
        state = row.state
        status = state.get("status")
        ts_raw = state.get("ts") or state.get("created_at") or ""
        day = str(ts_raw)[:10] if ts_raw else ""
        if status == "void" and state.get("reversed_on"):
            # Voided out of a locked period: the entry stays posted in its own period and
            # its reversal posts on reversed_on (posting_dates.void_reversal).
            out.append((row.entity_id, {**state, "status": "posted"}, day))
            out.append((f"{row.entity_id}{REVERSAL_SUFFIX}", reversal_state(row.entity_id, state), state["reversed_on"]))
            continue
        if status != "posted" and not (include_void and status == "void"):
            continue
        out.append((row.entity_id, state, day))
    return out


_SWAPPED = {"debit": "credit", "credit": "debit", "fx_debit": "fx_credit", "fx_credit": "fx_debit"}


def reversal_state(entity_id: str, state: dict) -> dict:
    """The reversal of a journal entry on its reversed_on date: every line through the
    same account and roles with debit and credit swapped."""
    memo = state.get("memo") or ""
    return {
        **state,
        "status": "posted",
        "ts": state["reversed_on"],
        "memo": f"Reversal of {memo}" if memo else "Reversal",
        "reverses": entity_id,
        "entries": [{_SWAPPED.get(k, k): v for k, v in line.items()} for line in state.get("entries") or []],
    }
