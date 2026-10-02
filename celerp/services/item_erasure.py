# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Removing items without a trace: deleting a draft that was a mistake, undoing an
import, clearing sample items nobody used.

An item may only vanish when nothing else depends on it. ``mentioned_elsewhere`` is
the one test of that (a document or list line, a journal entry, a movement or note in
another record), and ``erase_items`` the one way of removing an item and its history.
Callers check what their own operation allows first, then erase, in one transaction."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

_CHUNK = 200


async def mentioned_elsewhere(session: AsyncSession, company_id, needles: dict[str, list[str]], *,
                              besides=()) -> set[str]:
    """The ids in ``needles`` that another record mentions: any of an id's needles
    appears in the state or id of a record, or in the data of a ledger row, outside the
    ids themselves and the records in ``besides``."""
    own = set(needles) | set(besides)
    pairs = [(eid, n) for eid, ns in needles.items() for n in ns if n]
    found: set[str] = set()
    for start in range(0, len(pairs), _CHUNK):
        chunk = pairs[start:start + _CHUNK]
        terms = sorted({n for _, n in chunk})
        state_text = sa.cast(Projection.state, sa.Text)
        data_text = sa.cast(LedgerEntry.data, sa.Text)
        texts = [f"{eid}\n{text}" for eid, text in (await session.execute(
            sa.select(Projection.entity_id, state_text).where(
                Projection.company_id == company_id,
                sa.or_(*(state_text.contains(t) for t in terms), *(Projection.entity_id.contains(t) for t in terms)),
            ))).all() if eid not in own]
        texts += [text for eid, text in (await session.execute(
            sa.select(LedgerEntry.entity_id, data_text).where(
                LedgerEntry.company_id == company_id, sa.or_(*(data_text.contains(t) for t in terms)),
            ))).all() if eid not in own]
        found |= {eid for eid, n in chunk if any(n in text for text in texts)}
    return found


async def erase_items(session: AsyncSession, company_id, entity_ids) -> None:
    """Remove the items and every ledger row of theirs. Runs inside the caller's
    transaction and does not commit; the caller has already checked they may go."""
    ids = sorted(set(entity_ids))
    if not ids:
        return
    await session.execute(sa.delete(Projection).where(
        Projection.company_id == company_id, Projection.entity_id.in_(ids)))
    await session.execute(sa.delete(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(ids)))
