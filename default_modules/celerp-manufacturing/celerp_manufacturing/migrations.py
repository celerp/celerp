# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""One-time migration: convert legacy standalone BOM projections into item recipes.

The standalone ``bom`` entity was removed (recipes now live on the inventory item). This
converts any surviving ``bom`` projection that names an ``output_item_id`` into an
``item.recipe.set`` on that item. Defensive and idempotent: a no-op when there are no BOMs,
and re-running just re-emits the same recipe (full-replace). This file legitimately references
the retired ``bom`` shape, so it is excluded from the no-bom grep gate.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.models.projections import Projection


async def migrate_boms_to_recipes(session: AsyncSession, company_id, actor_id) -> int:
    """Convert this company's legacy BOM projections to item recipes. Returns count migrated."""
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "bom")
    )).scalars().all()
    migrated = 0
    for row in rows:
        state = row.state or {}
        output_item_id = state.get("output_item_id")
        if state.get("deleted") or not output_item_id:
            continue
        recipe = {
            "output_qty": state.get("output_qty", 1) or 1,
            "components": [
                {
                    "item_id": c.get("item_id"),
                    "sku": c.get("sku"),
                    "quantity": c.get("quantity", c.get("qty", 0)),
                    "unit": c.get("unit"),
                }
                for c in state.get("components", []) if c.get("item_id")
            ],
            "labor": [],
            "overhead": [],
        }
        await emit_event(
            session, company_id=company_id, entity_id=output_item_id, entity_type="item",
            event_type="item.recipe.set", data={"recipe": recipe}, actor_id=actor_id,
            location_id=None, source="migration", idempotency_key=f"bom-migrate:{row.entity_id}", metadata_={},
        )
        migrated += 1
    return migrated
