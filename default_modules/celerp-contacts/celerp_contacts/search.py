# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only search over contact projections.

The module-level query logic lives here so the list route and the global-search
provider run the exact same matching and ordering, with no second copy to drift.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp_contacts.references import CONTACT_TYPE_FILTER

# The contact fields, read from the projection state in SQL (``state ->> 'name'`` and so on).
_STATE = Projection.state


def _text(field: str):
    return sa.func.coalesce(_STATE[field].as_string(), "")


def _not_deleted():
    """``not state.get("deleted")``: missing, null, false, 0, "", [] or {}."""
    deleted = _STATE["deleted"]
    kind = sa.func.json_typeof(deleted)
    value = deleted.as_string()
    return sa.case(
        (deleted.is_(None), True),
        (kind == "null", True),
        (kind == "boolean", value == "false"),
        (kind == "number", sa.cast(value, sa.Numeric) == 0),
        (kind == "string", value == ""),
        (kind == "array", sa.func.json_array_length(deleted) == 0),
        (kind == "object", value == "{}"),
        else_=False,
    )


def _matches(q: str):
    """The query in name, email, phone, company name or any tag, ignoring case."""
    needle = q.lower()

    def like(expr):
        return sa.func.lower(expr).contains(needle, autoescape=True)

    tag = sa.func.json_array_elements_text(_STATE["tags"]).table_valued("value").alias("tag")
    tags = sa.case(
        (sa.func.json_typeof(_STATE["tags"]) == "array",
         select(sa.literal(1)).select_from(tag).where(like(tag.c.value)).exists()),
        else_=False,
    )
    return sa.or_(*(like(_text(f)) for f in ("name", "email", "phone", "company_name")), tags)


def _contact_filter(company_id, q: str, include_deleted: bool, contact_type: str | None) -> list:
    where = [Projection.company_id == company_id, Projection.entity_type == "contact"]
    if not include_deleted:
        where.append(_not_deleted())
    if q:
        where.append(_matches(q))
    if contact_type and contact_type in CONTACT_TYPE_FILTER:
        # An untyped contact is a customer (references.contact_accepts).
        kind = sa.func.coalesce(sa.func.nullif(_STATE["contact_type"].as_string(), ""), "customer")
        where.append(kind.in_(CONTACT_TYPE_FILTER[contact_type]))
    return where


async def search_contacts(
    session: AsyncSession,
    company_id: str,
    q: str,
    *,
    include_deleted: bool = False,
    contact_type: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Matching contacts ordered by (lowercased name, id), filtered, ordered and paged in SQL.

    The order is the code-point order of the lowercased name (COLLATE "C"), the order a Python
    sort of the same strings gives, so a page never depends on the database's collation.
    """
    stmt = (
        select(Projection.entity_id, Projection.state)
        .where(*_contact_filter(company_id, q, include_deleted, contact_type))
        .order_by(sa.collate(sa.func.lower(_text("name")), "C"), sa.collate(Projection.entity_id, "C"))
        .offset(max(offset, 0))
    )
    if limit is not None:
        stmt = stmt.limit(max(limit, 0))
    rows = (await session.execute(stmt)).all()
    return [state | {"id": entity_id} for entity_id, state in rows]


async def count_contacts(
    session: AsyncSession,
    company_id: str,
    q: str,
    *,
    include_deleted: bool = False,
    contact_type: str | None = None,
) -> int:
    """How many contacts search_contacts matches without a limit."""
    stmt = select(sa.func.count()).select_from(Projection).where(
        *_contact_filter(company_id, q, include_deleted, contact_type))
    return (await session.execute(stmt)).scalar_one()


async def global_search(
    session: AsyncSession,
    company_id: str,
    role: str,
    q: str,
    limit: int,
) -> dict:
    """Global-search provider: matching contacts, newest ordering, capped to limit."""
    return {"items": await search_contacts(session, company_id, q, limit=limit)}
