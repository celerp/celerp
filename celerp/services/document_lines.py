# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Document-line identity and the physical-item uniqueness invariant.

A non-splittable physical inventory item must appear at most once on any
document. The rule is enforced once, at the event boundary, so every current
and future document writer inherits it. Splittable items and unlinked /
free-text lines may repeat.
"""
from __future__ import annotations

from collections import Counter

from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.line_measures import splitting_allowed

# Physical-item uniqueness is an OUTBOUND customer-stock rule, not a universal document
# invariant: a customer-facing invoice or memo must not list the same non-splittable
# physical lot twice. Inbound and internal docs (bill, consignment_in, novel types) may.
# This is the single authoritative source; it mirrors the two existing synced outbound
# copies (ui.routes.documents._FULFILLABLE_DOC_TYPES and
# celerp_docs.doc_constants.FULFILLABLE_STATUSES) and must stay in lockstep with them.
OUTBOUND_LINE_UNIQUENESS_DOC_TYPES: frozenset[str] = frozenset({"invoice", "memo"})


def line_item_id(line: dict) -> str | None:
    """The single authoritative identity of a document line.

    A line's identity is its linked item/entity id only, never the SKU or
    description (two distinct lots can share a SKU; a free-text line has none).
    Returns None for an unlinked / free-text line, which may repeat freely.
    """
    return line.get("item_id") or line.get("entity_id")


async def assert_document_item_uniqueness(session, company_id, line_items) -> None:
    """Reject a document line set that repeats a non-splittable physical item.

    Fast path: if no linked id repeats, return without touching the database.
    Otherwise resolve the repeated ids in one company-scoped query:
      - a repeated id with no ``item`` projection -> 422 invalid reference;
      - a repeated id that resolves to a non-splittable item -> 409 duplicate;
      - a repeated id that resolves to a splittable item -> allowed.
    """
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
        if item is None:
            # A repeated id that resolves to no item projection cannot be
            # reasoned about - reject as an invalid reference rather than
            # silently persist a corrupt document.
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_reference",
                    "message": f"Line references an unknown item: {ident}",
                    "item_id": ident,
                },
            )
        if splitting_allowed(item.state) is False:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_document_item",
                    "message": "This item is already on the document.",
                    "item_id": ident,
                },
            )


async def assert_outbound_stock_uniqueness(
    session, company_id, entity_id, doc_type, line_items
) -> None:
    """Enforce physical-item uniqueness only for OUTBOUND docs (invoice, memo).

    The doc.updated event shape carries no ``doc_type``, so when ``doc_type`` is None it is
    resolved from the persisted projection by ``(company_id, entity_id)``. This keeps the
    doc-type scope and its resolution beside the invariant, so the event engine stays thin
    and gains no projection-reading responsibility.

    Fail-open on scope: an unresolvable doc_type (no projection, or absent field) is skipped.
    This is provably safe rather than permissive - every outbound doc is created through a
    required ``doc_type`` and writes its projection at ``doc.created`` before any ``doc.updated``
    can fire, so the set of docs with an unresolvable doc_type is disjoint from the invoice/memo
    set this guard protects, and that set was already validated at ``doc.created``.
    """
    if doc_type is None:
        row = await session.get(Projection, (company_id, entity_id))
        doc_type = row.state.get("doc_type") if row else None
    if doc_type not in OUTBOUND_LINE_UNIQUENESS_DOC_TYPES:
        return
    await assert_document_item_uniqueness(session, company_id, line_items)
