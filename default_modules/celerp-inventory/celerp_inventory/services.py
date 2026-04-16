# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import uuid

from sqlalchemy import text

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


async def upsert_from_connector(company_id: str, item) -> bool:
    """
    Create an item from a connector payload. Returns True if newly created.

    `item` must have: sku, name, idempotency_key (required for dedup).
    Optional: sale_price, quantity, description.

    Uses a fresh DB session so the connector does not need to manage
    session lifecycle. Idempotency is enforced at the ledger level.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal

    idem_key = item.idempotency_key
    if not idem_key:
        raise ValueError("idempotency_key required for connector upserts")

    async with AsyncSessionLocal() as session:
        # Check before emit to distinguish created vs skipped
        existing = (
            await session.execute(
                text("SELECT id FROM ledger WHERE idempotency_key=:k"),
                {"k": idem_key},
            )
        ).first()
        if existing:
            return False

        entity_id = f"item:{uuid.uuid4()}"
        data = {
            "sku": item.sku,
            "name": item.name,
            "idempotency_key": idem_key,
        }
        if item.sale_price is not None:
            data["sale_price"] = item.sale_price
        if item.quantity:
            data["quantity"] = item.quantity

        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.created",
            data=data,
            actor_id=None,
            location_id=None,
            source="connector",
            idempotency_key=idem_key,
            metadata_={},
        )
        await session.commit()
        return True
