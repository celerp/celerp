# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only search over contact projections.

The module-level query logic lives here so the list route and the global-search
provider run the exact same matching and ordering, with no second copy to drift.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection

_CONTACT_TYPE_FILTER: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "both"),
    "vendor": ("vendor", "both"),
    "both": ("both",),
}


def _match(contact: dict, q_lower: str) -> bool:
    return (
        q_lower in (contact.get("name") or "").lower()
        or q_lower in (contact.get("email") or "").lower()
        or q_lower in (contact.get("phone") or "").lower()
        or q_lower in (contact.get("company_name") or "").lower()
        or any(q_lower in t.lower() for t in (contact.get("tags") or []))
    )


async def search_contacts(
    session: AsyncSession,
    company_id: str,
    q: str,
    *,
    include_deleted: bool = False,
    contact_type: str | None = None,
) -> list[dict]:
    """Return every matching contact, ordered by (name, id).

    The caller slices for its own limit/offset so ordering stays identical
    between the paginated list route and the truncated search provider.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "contact",
            )
        )
    ).scalars().all()
    results = [r.state | {"id": r.entity_id} for r in rows]
    if not include_deleted:
        results = [c for c in results if not c.get("deleted")]
    if q:
        q_lower = q.lower()
        results = [c for c in results if _match(c, q_lower)]
    if contact_type and contact_type in _CONTACT_TYPE_FILTER:
        allowed = _CONTACT_TYPE_FILTER[contact_type]
        results = [c for c in results if (c.get("contact_type") or "customer") in allowed]
    results.sort(key=lambda c: ((c.get("name") or "").lower(), c.get("id") or ""))
    return results


async def global_search(
    session: AsyncSession,
    company_id: str,
    role: str,
    q: str,
    limit: int,
) -> dict:
    """Global-search provider: matching contacts, newest ordering, capped to limit."""
    results = await search_contacts(session, company_id, q)
    return {"items": results[:limit]}
