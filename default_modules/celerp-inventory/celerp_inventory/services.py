# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.inventory_codes import BarcodeConflictError
from celerp.models.company import Company
from celerp.models.projections import Projection

# Internally assigned SKUs/barcodes are short zero-padded sequences; imported
# EAN-13/GTIN-14 barcodes (13-14 digits) are excluded from the sequence scan so
# they are never re-used as the next internal code.
_MAX_SEQ_DIGITS = 9
_SEQ_WIDTH = 6


async def lock_item_code_namespace(session: AsyncSession, company_id) -> None:
    """Serialize SKU/barcode allocation for a company.

    Two concurrent creates each read the same max sequence and mint the same next
    code; the barcode unique index then rejects the loser with a 409. Taking a row
    lock on the company here makes the second allocator wait for the first to
    commit, so it reads the updated max and mints the next code instead of colliding.
    The lock is held until the caller's transaction commits or rolls back; every
    allocation and barcode check in that request must run after this call.
    """
    await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update()
    )


async def _next_seq(session: AsyncSession, company_id) -> int:
    """Return the next integer in the shared SKU/barcode sequence for a company.

    Scans integer-valued SKUs and barcodes together so the two namespaces never
    collide (a barcode assigned during a split is never re-used as a SKU on the
    next create). Only barcodes with <= _MAX_SEQ_DIGITS digits count, excluding
    imported EAN-13/GTIN-14 barcodes while covering every internally assigned one.
    """
    sku_vals = (await session.execute(
        select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    barcode_vals = (await session.execute(
        select(Projection.state["barcode"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    all_vals = list(sku_vals) + [v for v in barcode_vals if v and len(v) <= _MAX_SEQ_DIGITS]
    return max((int(v) for v in all_vals if v and str(v).isdigit()), default=0) + 1


async def allocate_internal_codes(session: AsyncSession, company_id, count: int = 1) -> list[str]:
    """Lock the company's code namespace and return ``count`` fresh sequential codes.

    The lock makes the scan-then-mint atomic against concurrent allocators. Codes
    are zero-padded to the standard internal width and are guaranteed distinct
    within the returned batch.
    """
    await lock_item_code_namespace(session, company_id)
    start = await _next_seq(session, company_id)
    return [str(start + i).zfill(_SEQ_WIDTH) for i in range(count)]


async def assert_barcode_available(session: AsyncSession, company_id, barcode) -> None:
    """Raise BarcodeConflictError if another item in the company already holds ``barcode``.

    An empty or absent barcode is always available. This is the application-side
    check that yields a clean 409; the DB unique index is the final backstop for
    writers that bypass it.
    """
    if not barcode:
        return
    existing = (await session.execute(
        select(Projection.entity_id).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.state["barcode"].as_string() == str(barcode),
        )
    )).first()
    if existing:
        raise BarcodeConflictError(barcode)


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
        # Derived price lists are computed from the base at read time; a store-synced price
        # must not be stored under a derived key (it would be masked on every read, then
        # resurface as a stale manual price if the factor is ever removed).
        from celerp.services.pricing import derived_price_keys, get_price_config
        derived = derived_price_keys((await get_price_config(session, company_id))[0])
        for key in derived:
            data.pop(key, None)
        outcome = await connector_upsert(
            session, company_id=company_id, entity_type="item",
            event_type="item.created", idem_key=idem_key, data=data,
        )
        await session.commit()
        return outcome
