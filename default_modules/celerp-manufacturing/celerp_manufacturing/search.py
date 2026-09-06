# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only search over manufacturing-order projections.

The list route and the global-search provider share this one filter/sort so the
q-grammar and ordering never drift. The route has no result cap; the provider
applies its limit AFTER the identical filter and sort.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection

_INCOMPLETE_STATUSES = frozenset({"planned", "in_progress", "on_hold"})


def _haystack(order: dict) -> str:
    return " ".join([
        str(order.get("id", "")),
        str(order.get("description", "")),
        str(order.get("source_doc_id", "")),
        " ".join(str(x.get("sku", "")) for x in order.get("expected_outputs", [])),
    ]).lower()


async def search_orders(
    session: AsyncSession,
    company_id: str,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Return every matching manufacturing order, newest first. No result cap."""
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "mfg_order",
        )
    )).scalars().all()
    items = [
        r.state | {"id": r.entity_id,
                   "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]
    if status:
        s = status.lower().strip()
        if s == "incomplete":
            items = [o for o in items if str(o.get("status") or "").lower() in _INCOMPLETE_STATUSES]
        else:
            items = [o for o in items if str(o.get("status") or "").lower() == s]
    if q:
        ql = q.lower().strip().strip(",")
        items = [o for o in items if ql in _haystack(o)]
    if date_from:
        items = [o for o in items if (o.get("created_at") or "")[:10] >= date_from]
    if date_to:
        items = [o for o in items if (o.get("created_at") or "")[:10] <= date_to]
    items.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return items


async def global_search(
    session: AsyncSession,
    company_id: str,
    role: str,
    q: str,
    limit: int,
) -> dict:
    """Global-search provider: newest matching orders, capped to limit."""
    items = await search_orders(session, company_id, q=q)
    return {"items": items[:limit]}
