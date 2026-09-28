# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time move of earlier imports into Received.

Imports used to land as documents of our own (``doc.shared_import``), which put
another business's invoice among ours. An import nobody has touched since is
moved: its single ledger event is rewritten as a ``received_doc.imported``
event and its projection is replaced, so a rebuild from the ledger gives the
same result. An import that anything else in the ledger depends on is part of
our books by now and stays a document: any later event on it (edited,
converted, paid, voided, a note or a file added), any journal entry posted for
it, and any record that names it in its data.

Earlier imports carry no sender identity, so each becomes its own received
record keyed by its old document id. Runs in the lifespan and is gated by a
marker so it runs once per database.
"""

from __future__ import annotations

import logging

from collections.abc import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

LEGACY_RECEIVED_KEY = "received_legacy_imports"
LEGACY_INSTALLATION = "legacy"
_LEGACY_ONLY_FIELDS = ("source_share_token", "source_origin", "status")


def legacy_received_data(data: dict, old_id: str, received_at: str | None) -> dict:
    """The received_doc.imported payload for an earlier doc.shared_import."""
    from celerp_docs.received import document_digest

    document = {k: v for k, v in (data or {}).items() if k not in _LEGACY_ONLY_FIELDS}
    token, origin = data.get("source_share_token"), data.get("source_origin")
    return {
        "source_installation": LEGACY_INSTALLATION,
        "source_document": old_id,
        "source_revision": None,
        "source_link": f"{origin.rstrip('/')}/share/{token}" if token and origin else None,
        "digest": document_digest(document),
        "received_at": received_at,
        "document": document,
    }


def _strings(value) -> Iterator[str]:
    """Every string held anywhere in a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def depended_on(rows: Iterable[tuple], candidates: set[str]) -> set[str]:
    """The candidate documents that any of ``rows`` depends on.

    Rows are (entity_id, idempotency_key, data, metadata) of ledger entries. A
    ledger entry depends on a document when it is an event on the document,
    a journal entry posted for it (its entity id or idempotency key is scoped
    to the document), or when any field of its data or metadata holds the
    document id. Fields are matched by exact value, so an id that only appears
    inside longer text does not count."""
    found: set[str] = set()
    for entity_id, key, data, metadata in rows:
        if entity_id in candidates:
            found.add(entity_id)
        for scoped, prefix in ((entity_id, "je:auto:"), (key or "", "je:")):
            if scoped.startswith(prefix):
                rest = scoped[len(prefix):]
                found.update(c for c in candidates if rest.startswith(c + ":"))
        for value in (*_strings(data), *_strings(metadata)):
            if value in candidates:
                found.add(value)
    return found


async def move_legacy_imports(session: AsyncSession) -> dict:
    """Move untouched earlier imports into Received. Caller owns the transaction."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp_docs.received import ENTITY_TYPE, received_id, revision_key

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, LEGACY_RECEIVED_KEY)):
        return {"changed": False, "moved": 0}

    imported = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.event_type == "doc.shared_import")
    )).scalars().all()
    by_company: dict = {}
    for entry in imported:
        by_company.setdefault(entry.company_id, []).append(entry)
    import_ids = {entry.id for entry in imported}
    kept: set[tuple] = set()
    for company_id, entries in by_company.items():
        candidates = {e.entity_id for e in entries}
        rows = await session.stream(
            select(LedgerEntry.entity_id, LedgerEntry.idempotency_key, LedgerEntry.data, LedgerEntry.metadata_)
            .where(LedgerEntry.company_id == company_id, LedgerEntry.id.not_in(import_ids))
        )
        async for part in rows.partitions(1000):
            kept.update((company_id, doc) for doc in depended_on(part, candidates))

    moved = 0
    for entry in imported:
        if (entry.company_id, entry.entity_id) in kept:
            continue
        old_id = entry.entity_id
        rid = received_id(LEGACY_INSTALLATION, "", old_id)
        data = legacy_received_data(entry.data, old_id, entry.ts.isoformat() if entry.ts else None)
        entry.entity_id = rid
        entry.entity_type = ENTITY_TYPE
        entry.event_type = "received_doc.imported"
        entry.data = data
        entry.idempotency_key = revision_key(rid, data["digest"], data.get("source_link"), entry.company_id)
        old = await session.get(Projection, (entry.company_id, old_id))
        if old is not None:
            await session.delete(old)
        await session.flush()
        await ProjectionEngine.apply_event(session, entry)
        moved += 1

    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, "done"))
    if moved:
        log.info("Moved %d earlier import(s) into Received", moved)
    return {"changed": True, "moved": moved}


async def move_legacy_imports_hook(session: AsyncSession) -> None:
    await move_legacy_imports(session)
