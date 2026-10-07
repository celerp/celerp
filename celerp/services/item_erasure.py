# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Removing items without a trace: deleting a draft that was a mistake, undoing an
import, clearing sample items nobody used.

An item may only vanish when nothing else depends on it. ``depended_on`` is the one
test of that (a document or list line, a journal entry, a movement or note in another
record, a file kept on the item), ``referrers`` names the records that mention an item,
and ``erase_items`` is the one way of removing an item and its history. Callers check
what their own operation allows first, then erase, in one transaction. An import never
holds an item: ``release_from_imports`` takes a deleted item out of the imports that
list it, so undoing the rest still works.

An erased item that came from a connector stays erased: its connector identity is kept
in the company settings, and a later sync of that record writes nothing."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.import_batch import ImportBatch
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company

_CHUNK = 200

# The connector identities (state "idempotency_key") of erased items, sorted.
ERASED_CONNECTOR_ITEMS = "erased_connector_items"


_FILE_KEYS = ("files", "attachments", "preview_image_id")

# The state fields that name a record to a person, most specific first.
_LABEL_KEYS = ("doc_number", "sku", "name")


async def depended_on(session: AsyncSession, company_id, needles: dict[str, list[str]], *,
                      besides=()) -> set[str]:
    """The ids in ``needles`` something else depends on: another record mentions them
    (outside the records in ``besides``), or a file is kept on them."""
    return set(await _mentions(session, company_id, needles, besides=besides)) | await holding_files(
        session, company_id, needles)


async def holding_files(session: AsyncSession, company_id, entity_ids) -> set[str]:
    """The given ids that keep a file."""
    ordered = sorted(set(entity_ids))
    found: set[str] = set()
    for start in range(0, len(ordered), _CHUNK):
        states = await session.execute(sa.select(Projection.entity_id, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_id.in_(ordered[start:start + _CHUNK])))
        found |= {eid for eid, state in states if any((state or {}).get(k) for k in _FILE_KEYS)}
    return found


async def referrers(session: AsyncSession, company_id, entity_ids) -> dict[str, list[str]]:
    """For each given id another record mentions, the sorted labels of those records:
    a document's number, an item's SKU, else the record's name or id."""
    mentions = await _mentions(session, company_id, {e: [e] for e in entity_ids})
    named = sorted(set().union(*mentions.values())) if mentions else []
    labels: dict[str, str] = {}
    for start in range(0, len(named), _CHUNK):
        for eid, state in (await session.execute(sa.select(Projection.entity_id, Projection.state).where(
                Projection.company_id == company_id, Projection.entity_id.in_(named[start:start + _CHUNK])))).all():
            labels[eid] = next((str(state[k]) for k in _LABEL_KEYS if (state or {}).get(k)), eid)
    return {eid: sorted({labels.get(r, r) for r in found}) for eid, found in mentions.items()}


async def _mentions(session: AsyncSession, company_id, needles: dict[str, list[str]], *,
                    besides=()) -> dict[str, set[str]]:
    """For each id in ``needles`` that another record mentions, the ids of those
    records: any of the id's needles appears in the state or id of a record, or in the
    data of a ledger row, outside the ids themselves and the records in ``besides``."""
    own = set(needles) | set(besides)
    pairs = [(eid, n) for eid, ns in needles.items() for n in ns if n]
    found: dict[str, set[str]] = {}
    for start in range(0, len(pairs), _CHUNK):
        chunk = pairs[start:start + _CHUNK]
        terms = sorted({n for _, n in chunk})
        state_text = sa.cast(Projection.state, sa.Text)
        data_text = sa.cast(LedgerEntry.data, sa.Text)
        texts = [(eid, f"{eid}\n{text}") for eid, text in (await session.execute(
            sa.select(Projection.entity_id, state_text).where(
                Projection.company_id == company_id,
                sa.or_(*(state_text.contains(t) for t in terms), *(Projection.entity_id.contains(t) for t in terms)),
            ))).all() if eid not in own]
        texts += [(eid, text) for eid, text in (await session.execute(
            sa.select(LedgerEntry.entity_id, data_text).where(
                LedgerEntry.company_id == company_id, sa.or_(*(data_text.contains(t) for t in terms)),
            ))).all() if eid not in own]
        for eid, n in chunk:
            hits = {other for other, text in texts if n in text}
            if hits:
                found.setdefault(eid, set()).update(hits)
    return found


async def release_from_imports(session: AsyncSession, company_id, entity_ids) -> None:
    """Take the given items out of every active import that lists them, so undoing the
    rest of the import still works. Runs inside the caller's transaction, which holds the
    company lock, and does not commit."""
    ids = set(entity_ids)
    if not ids:
        return
    batch_text = sa.cast(ImportBatch.entity_ids, sa.Text)
    for batch in (await session.execute(sa.select(ImportBatch).where(
            ImportBatch.company_id == company_id, ImportBatch.status == "active",
            sa.or_(*(batch_text.contains(f'"{e}"') for e in sorted(ids))),
    ).with_for_update().execution_options(populate_existing=True))).scalars().all():
        kept = [e for e in batch.entity_ids or [] if e not in ids]
        if len(kept) != len(batch.entity_ids or []):
            batch.entity_ids = kept


async def erase_items(session: AsyncSession, company_id, entity_ids) -> None:
    """Remove the items and every ledger row of theirs. Runs inside the caller's
    transaction and does not commit; the caller has already checked they may go."""
    ids = sorted(set(entity_ids))
    if not ids:
        return
    keys = {(state or {}).get("idempotency_key") for (state,) in (await session.execute(
        sa.select(Projection.state).where(Projection.company_id == company_id, Projection.entity_id.in_(ids))))}
    keys.discard(None)
    if keys:
        company = await locked_company(session, company_id)
        settings = dict(company.settings or {})
        settings[ERASED_CONNECTOR_ITEMS] = sorted(keys | set(settings.get(ERASED_CONNECTOR_ITEMS, [])))
        company.settings = settings
    await session.execute(sa.delete(Projection).where(
        Projection.company_id == company_id, Projection.entity_id.in_(ids)))
    await session.execute(sa.delete(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(ids)))


def erased_from_connector(settings: dict, idem_key: str) -> bool:
    """True when the item a connector knows by ``idem_key`` was erased here."""
    return idem_key in settings.get(ERASED_CONNECTOR_ITEMS, ())
