# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Reading and recording the module's own entities. Nothing here posts to the ledger,
so code that must never post (the tax books) records through this and nothing else."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from celerp.models.projections import Projection


async def _emit(session, company_id, user_id, entity_id: str, entity_type: str, event_type: str, data: dict,
                key: str | None = None):
    from celerp.events.engine import emit_event

    return await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                            event_type=event_type, data=data, actor_id=user_id, location_id=None,
                            source="fixed_assets", idempotency_key=key or str(uuid.uuid4()))


async def _state(session, company_id, entity_id: str) -> dict | None:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                            populate_existing=True)
    return row.state if row is not None else None


async def _entities(session, company_id, entity_type: str) -> list[tuple[str, dict]]:
    return list((await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == entity_type))).all())
