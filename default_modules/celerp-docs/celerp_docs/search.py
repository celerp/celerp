# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only search over document projections.

The q-grammar (comma-separated OR terms across doc_number/contact_name/
contact_id/ref) lives here so the list route and the global-search provider
share one source of truth for what a search term matches.
"""
from __future__ import annotations

import sqlalchemy as _sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection

_SEARCH_FIELDS = ("doc_number", "contact_name", "contact_id", "ref")


def doc_q_clause(q: str | None):
    """Build the OR-of-terms WHERE clause for a document search string, or None.

    Comma is an OR operand (matches the inventory grammar and the search box's
    Enter-appends-comma behaviour), so "2816," and "2816, 2817" both match rather
    than LIKE-ing the whole string with its trailing comma.
    """
    if not q:
        return None
    terms = [t.strip().lower() for t in q.split(",") if t.strip()]
    if not terms:
        return None
    term_clauses = [
        _sa.or_(*[
            _sa.func.lower(Projection.state[f].as_string()).like(f"%{term}%")
            for f in _SEARCH_FIELDS
        ])
        for term in terms
    ]
    return _sa.or_(*term_clauses)


async def global_search(
    session: AsyncSession,
    company_id: str,
    role: str,
    q: str,
    limit: int,
) -> dict:
    """Global-search provider: newest documents matching q, capped to limit."""
    where = [
        Projection.company_id == company_id,
        Projection.entity_type == "doc",
    ]
    clause = doc_q_clause(q)
    if clause is not None:
        where.append(clause)
    list_q = (
        select(Projection)
        .where(*where)
        .order_by(
            Projection.state["issue_date"].as_string().desc(),
            Projection.entity_id.desc(),
        )
        .limit(limit)
    )
    rows = (await session.execute(list_q)).scalars().all()
    out = [r.state | {"id": r.entity_id} for r in rows]
    return {"items": out}
