# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only search over subscription-template projections.

Subscription templates are docs with doc_type subscription_invoice or
subscription_po. The list route and the global-search provider call the one
service here so the direction/status/q filtering and ordering never diverge.
"""
from __future__ import annotations

from sqlalchemy import func as _func, or_ as _or, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection

SUBSCRIPTION_DOC_TYPES = frozenset({"subscription_invoice", "subscription_po"})
_SEARCH_FIELDS = ("name", "doc_number", "ref_id", "contact_name")


def _base_where(company_id, direction: str | None) -> list:
    if direction == "sales":
        return [
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
            Projection.state["doc_type"].as_string() == "subscription_invoice",
        ]
    if direction == "purchasing":
        return [
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
            Projection.state["doc_type"].as_string() == "subscription_po",
        ]
    return [
        Projection.company_id == company_id,
        Projection.entity_type == "doc",
        _or(*(Projection.state["doc_type"].as_string() == dt for dt in SUBSCRIPTION_DOC_TYPES)),
    ]


async def search_subscription_templates(
    session: AsyncSession,
    company_id,
    *,
    direction: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (items, total) for subscription templates matching the filters.

    The q search is pushed into the WHERE so it runs BEFORE the COUNT and LIMIT;
    a Python post-filter would run after pagination truncated the rows. A
    template stores ref_id at creation and only gains doc_number on a later
    renumber, so both are matched; name is the human template label.
    """
    where = _base_where(company_id, direction)
    if status:
        where.append(Projection.state["status"].as_string() == status)
    if q and q.strip():
        terms = [t.strip().lower() for t in q.split(",") if t.strip()]
        where.append(_or(*[
            _func.lower(Projection.state[f].as_string()).like(f"%{term}%")
            for term in terms for f in _SEARCH_FIELDS
        ]))
    total = (await session.execute(
        select(_func.count()).select_from(Projection).where(*where)
    )).scalar_one()
    rows = (await session.execute(
        select(Projection).where(*where).order_by(Projection.entity_id.desc()).offset(offset).limit(limit)
    )).scalars().all()
    items = [r.state | {"id": r.entity_id} for r in rows]
    return items, total


async def global_search(
    session: AsyncSession,
    company_id,
    role: str,
    q: str,
    limit: int,
) -> dict:
    """Global-search provider: newest matching templates, capped to limit."""
    items, _total = await search_subscription_templates(session, company_id, q=q, limit=limit)
    return {"items": items}
