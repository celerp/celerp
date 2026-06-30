# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

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


def _external_ids(platform: str, idem_key: str) -> dict:
    """Recover the platform's external ids from a connector item's idempotency key.

    Inbound upserts encode the platform id in the idempotency key, so outbound
    sync recovers it from there rather than storing duplicate columns:
      shopify:{product_id}:{variant_id} -> shopify_product_id, shopify_variant_id
      woocommerce:{product_id}          -> woocommerce_product_id
    (Shopify location-level inventory needs a location id that is not captured on
    import; those items are skipped by the connector's inventory push.)
    """
    parts = (idem_key or "").split(":")
    if platform == "shopify" and len(parts) >= 3:
        return {"shopify_product_id": parts[1], "shopify_variant_id": parts[2]}
    if platform == "woocommerce" and len(parts) >= 2:
        return {"woocommerce_product_id": parts[1]}
    return {}


async def _items_with_external_id(company_id: str, platform: str, require_sync_flag: bool = False) -> list[dict]:
    """All item projections linked to `platform`, as outbound-ready dicts.

    When ``require_sync_flag`` is set, only items the user has opted into outbound sync
    (is_sync_to_shopify=True) are returned - so the catalog is never mass-pushed back to
    the store; the merchant explicitly enables each item."""
    import uuid as _uuid
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.models.projections import Projection
    from sqlalchemy import select

    cid = _uuid.UUID(str(company_id))
    out: list[dict] = []
    async with AsyncSessionLocal() as session:
        query = select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
            Projection.state["idempotency_key"].as_string().like(f"{platform}:%"),
        )
        if require_sync_flag:
            query = query.where(Projection.is_sync_to_shopify.is_(True))
        rows = (await session.execute(query)).scalars().all()
        for r in rows:
            st = r.state or {}
            out.append({
                "sku": st.get("sku"),
                "name": st.get("name"),
                "description": st.get("description"),
                "sale_price": st.get("sale_price"),
                "quantity": st.get("quantity", 0),
                "files": st.get("files") or [],
                **_external_ids(platform, st.get("idempotency_key", "")),
            })
    return out


async def list_items_with_external_id(company_id: str, platform: str) -> list[dict]:
    """Items linked to a platform (have an external id), for outbound inventory push.
    Shopify outbound is opt-in per item (is_sync_to_shopify); other platforms push all
    linked items (a per-platform flag is a follow-up)."""
    return await _items_with_external_id(company_id, platform, require_sync_flag=(platform == "shopify"))


async def list_items_modified_since_last_sync(company_id: str, platform: str) -> list[dict]:
    """Items linked to a platform, for outbound product push. Shopify pushes only items
    the user opted in (is_sync_to_shopify); other platforms push all linked items.
    Outbound PUTs are idempotent and failed items re-push on the next run, so a per-item
    modified watermark is a follow-up rather than launch work."""
    return await _items_with_external_id(company_id, platform, require_sync_flag=(platform == "shopify"))


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
                text("SELECT id FROM ledger WHERE company_id = CAST(:cid AS uuid) AND idempotency_key=:k"),
                {"cid": str(company_id), "k": idem_key},
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
