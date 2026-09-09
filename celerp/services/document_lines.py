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
