# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only search over document projections.

The q-grammar (comma-separated OR terms across doc_number/ref_id/contact_name/
contact_id/ref and the contact name a document shows) lives here so the list
route and the global-search provider share one source of truth for what a
search term matches.
"""
from __future__ import annotations

import sqlalchemy as _sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp_contacts.references import party_name_sql

_SEARCH_FIELDS = ("doc_number", "ref_id", "contact_name", "contact_id", "ref")


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
    # The name the list shows (party_name_sql) as well as the stored fields, so a
    # document that holds only its contact's id is found by that contact's name. Each
    # term is matched as typed: a % or _ in it is that character, never a wildcard.
    searched = [Projection.state[f].as_string() for f in _SEARCH_FIELDS] + [party_name_sql()]
    term_clauses = [
        _sa.or_(*[_sa.func.lower(field).contains(term, autoescape=True) for field in searched])
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
    """Global-search provider: newest documents and Lists matching q, capped to limit.

    A List is a document the user finds the same way (by its number or its customer), so it is
    searched with them; each row says which it is under ``kind``. ``view_documents`` gates both,
    as it does the document and List pages."""
    where = [
        Projection.company_id == company_id,
        Projection.entity_type.in_(("doc", "list")),
    ]
    clause = doc_q_clause(q)
    if clause is not None:
        where.append(clause)
    list_q = (
        select(Projection)
        .where(*where)
        .order_by(
            # A List has no issue date; it must not float above every dated document.
            Projection.state["issue_date"].as_string().desc().nulls_last(),
            Projection.entity_id.desc(),
        )
        .limit(limit)
    )
    rows = (await session.execute(list_q)).scalars().all()
    out = [r.state | {"id": r.entity_id, "kind": r.entity_type} for r in rows]
    return {"items": out}
