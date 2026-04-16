# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Cross-module entity reference resolution.

Modules use this to resolve entity IDs from other modules without
importing each other's routers directly.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection

log = logging.getLogger(__name__)


async def resolve_entity(
    entity_id: str, company_id, session: AsyncSession
) -> dict | None:
    """Attempt to resolve an entity_id to its current projection state.

    Returns the projection state dict if found, None otherwise.
    Never raises — callers handle None gracefully.
    """
    try:
        row = await session.get(
            Projection,
            {"company_id": company_id, "entity_id": entity_id},
        )
        return dict(row.state) if row else None
    except Exception as exc:
        log.debug("resolve_entity(%r): %s", entity_id, exc)
        return None
