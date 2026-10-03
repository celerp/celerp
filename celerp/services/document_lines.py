# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Document-line identity, the linked-item reference rule, and the physical-item
uniqueness invariant.

A line linked to an item must link to a real item of the same company: a stale form
or an import can carry the id of an item that was removed (an undone import), and a
document must never keep a reference to it. A non-splittable physical inventory item must appear at most once on an outbound
customer-stock document (invoice, memo). The rule is enforced once, at the event
boundary, so every current and future outbound document writer inherits it.
Inbound and internal documents (bill, consignment_in, novel types), splittable
items, and unlinked / free-text lines may repeat.
"""
from __future__ import annotations

from collections import Counter

from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.company_lock import lock_company
from celerp.services.line_measures import splitting_allowed

# The uniqueness invariant is an OUTBOUND customer-stock rule: a customer-facing
# invoice or memo must not list the same non-splittable physical lot twice.
# Inbound and internal documents (bill, consignment_in, novel types) legitimately
# may, so they are not governed. This mirrors the outbound set the codebase already
# names in celerp_docs.doc_constants.FULFILLABLE_STATUSES and must stay in lockstep
# with it.
DOCUMENT_ITEM_UNIQUE_DOC_TYPES: frozenset[str] = frozenset({"invoice", "memo"})


def line_item_id(line: dict) -> str | None:
    """The single authoritative identity of a document line.

    A line's identity is its linked item/entity id only, never the SKU or
    description (two distinct lots can share a SKU; a free-text line has none).
    Returns None for an unlinked / free-text line, which may repeat freely.
    """
    return line.get("item_id") or line.get("entity_id")


async def linked_items(session, company_id, line_items, *, known=frozenset()) -> dict[str, Projection]:
    """Resolve every linked line to its item, refusing a line whose item does not exist.

    The one rule for every document and List line writer: each supplied item_id /
    entity_id must resolve to an item projection of ``company_id``, else 422
    ``invalid_reference`` naming the line (1-based) and the id. ``known`` holds ids
    already accepted on the stored record, which a save may carry forward without
    re-proving them. Free-text lines (no id) are not checked. One company-scoped query.

    Returns the resolved projections keyed by id. A line that adds an item takes the
    company lock before the check (a no-op for a writer that already holds it), so a
    removal that holds the lock (Undo) either commits first and the add is refused, or
    waits until the add has committed and then sees it.
    """
    linked = [(n, line, line_item_id(line)) for n, line in enumerate(line_items or [], 1)
              if isinstance(line, dict) and line_item_id(line)]
    ids = {ident for _, _, ident in linked}
    if not ids:
        return {}
    if ids - known:
        await lock_company(session, company_id)
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.entity_id.in_(ids),
        )
    )).scalars().all()
    items = {row.entity_id: row for row in rows}
    for n, line, ident in linked:
        if ident not in items and ident not in known:
            label = line.get("name") or line.get("sku")
            where = f"Line {n} ({label})" if label else f"Line {n}"
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_reference",
                    "message": f"{where} references an item that does not exist: {ident}",
                    "item_id": ident,
                    "line": n,
                },
            )
    return items


async def assert_document_item_uniqueness(session, company_id, doc_type, line_items) -> None:
    """Reject an OUTBOUND document line set that repeats a non-splittable physical item.

    Only invoice and memo are governed (``DOCUMENT_ITEM_UNIQUE_DOC_TYPES``); any other
    ``doc_type`` returns early, so inbound/internal and novel document types may repeat
    the same physical item freely.

    Fast path: if no linked id repeats, return without touching the database.
    Otherwise resolve the repeated ids in one company-scoped query:
      - a repeated id that resolves to a non-splittable item -> 409 duplicate;
      - a repeated id that resolves to a splittable item or to none -> allowed here
        (``linked_items`` has already refused a newly added id with no item).
    """
    if doc_type not in DOCUMENT_ITEM_UNIQUE_DOC_TYPES:
        return
    if not line_items:
        return

    counts = Counter()
    for line in line_items:
        if not isinstance(line, dict):
            continue
        ident = line_item_id(line)
        if ident:
            counts[ident] += 1

    repeated = [ident for ident, n in counts.items() if n > 1]
    if not repeated:
        return  # fast path: no linked id repeats, no DB read

    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.entity_id.in_(repeated),
        )
    )).scalars().all()
    items = {row.entity_id: row for row in rows}

    for ident in repeated:
        item = items.get(ident)
        if item is not None and splitting_allowed(item.state) is False:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_document_item",
                    "message": "This item is already on the document.",
                    "item_id": ident,
                },
            )
