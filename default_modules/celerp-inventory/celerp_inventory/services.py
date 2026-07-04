# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import uuid


from celerp.events.engine import emit_event


async def create_item(session, company_id: str, data: dict, actor_id: str | None = None):
    entity_id = data.get("entity_id", f"item:{uuid.uuid4()}")
    return await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data=data,
        actor_id=actor_id,
        location_id=data.get("location_id"),
        source="api",
        idempotency_key=data.get("idempotency_key", str(uuid.uuid4())),
        metadata_={},
    )


async def upsert_from_connector(company_id: str, item) -> str:
    """
    Create or update an item from a connector payload. Returns the write outcome:
    "created", "updated", or "noop" (this exact content was already applied).

    `item` must have: sku, name, idempotency_key (stable per external item).
    Optional: sale_price, quantity, cost_price, description.

    Uses a fresh DB session so the connector does not need to manage
    session lifecycle. Idempotency is enforced at the ledger level.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.events.engine import connector_upsert

    idem_key = item.idempotency_key
    if not idem_key:
        raise ValueError("idempotency_key required for connector upserts")

    data = {
        "sku": item.sku,
        "name": item.name,
    }
    if item.sale_price is not None:
        data["sale_price"] = item.sale_price
        data["retail_price"] = item.sale_price   # canonical selling-price field
    if item.quantity:
        data["quantity"] = item.quantity
    if getattr(item, "cost_price", None) is not None:
        data["cost_price"] = item.cost_price     # else margin/COGS/valuation read zero cost
    if getattr(item, "description", None):
        data["description"] = item.description

    async with AsyncSessionLocal() as session:
        outcome = await connector_upsert(
            session, company_id=company_id, entity_type="item",
            event_type="item.created", idem_key=idem_key, data=data,
        )
        await session.commit()
        return outcome
