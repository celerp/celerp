# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The per-item outbound sync flag (ERP -> Shopify).

Covers the projection handler in isolation and the full event chain: a
shop.sync.enabled event on an item sets is_sync_to_shopify on the item's projection
(routed by the 'shop.sync.' prefix to the handler, then copied to the denormalized
column by the engine) while preserving the rest of the item state.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.projections.handlers.shopify import apply_shop_sync_event


def test_handler_toggles_flag_and_preserves_state():
    base = {"sku": "ABC", "name": "Widget", "entity_type": "item"}
    on = apply_shop_sync_event(base, "shop.sync.enabled", {})
    assert on["is_sync_to_shopify"] is True
    assert on["sku"] == "ABC" and on["entity_type"] == "item"  # other state preserved
    off = apply_shop_sync_event(base, "shop.sync.disabled", {})
    assert off["is_sync_to_shopify"] is False
    with pytest.raises(ValueError):
        apply_shop_sync_event(base, "shop.sync.bogus", {})


async def _emit(session, cid, eid, event_type, data):
    await emit_event(
        session, company_id=cid, entity_id=eid, entity_type="item",
        event_type=event_type, data=data, actor_id=None, location_id=None,
        source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


@pytest.mark.asyncio
async def test_shop_sync_event_sets_projection_column(session):
    cid = uuid.uuid4()
    session.add(Company(id=cid, name="SyncCo", slug=f"syncco-{cid.hex[:8]}"))
    await session.flush()
    eid = "item:sync-1"
    await _emit(session, cid, eid, "item.created", {"sku": "S", "name": "A", "quantity": 1})
    await _emit(session, cid, eid, "shop.sync.enabled", {})
    await ProjectionEngine.rebuild(session)

    proj = (await session.execute(select(Projection).where(Projection.entity_id == eid))).scalar_one()
    assert proj.is_sync_to_shopify is True          # denormalized column set by the engine
    assert proj.state.get("sku") == "S"             # item state preserved across the toggle


@pytest.mark.asyncio
async def test_shop_sync_disabled_clears_flag(session):
    cid = uuid.uuid4()
    session.add(Company(id=cid, name="SyncCo2", slug=f"syncco2-{cid.hex[:8]}"))
    await session.flush()
    eid = "item:sync-2"
    await _emit(session, cid, eid, "item.created", {"sku": "S2", "name": "B", "quantity": 1})
    await _emit(session, cid, eid, "shop.sync.enabled", {})
    await _emit(session, cid, eid, "shop.sync.disabled", {})
    await ProjectionEngine.rebuild(session)

    proj = (await session.execute(select(Projection).where(Projection.entity_id == eid))).scalar_one()
    assert proj.is_sync_to_shopify is False


@pytest.mark.asyncio
async def test_bulk_shopify_sync_endpoint_sets_flag(session):
    """The bulk endpoint emits shop.sync.enabled per item, which sets the projection flag."""
    from types import SimpleNamespace

    from celerp_inventory.routes import BulkShopifySyncBody, bulk_shopify_sync

    cid = uuid.uuid4()
    session.add(Company(id=cid, name="BulkCo", slug=f"bulkco-{cid.hex[:8]}"))
    await session.flush()
    eid = "item:bulk-1"
    await _emit(session, cid, eid, "item.created", {"sku": "B", "name": "Bulk", "quantity": 1})

    user = SimpleNamespace(id=None)  # actor_id is nullable; avoids needing a real users row
    res = await bulk_shopify_sync(
        BulkShopifySyncBody(entity_ids=[eid], enable=True),
        company_id=cid, _=None, user=user, session=session,
    )
    assert res["updated"] == 1 and res["enabled"] is True
    await ProjectionEngine.rebuild(session)
    proj = (await session.execute(select(Projection).where(Projection.entity_id == eid))).scalar_one()
    assert proj.is_sync_to_shopify is True


@pytest.mark.asyncio
async def test_list_items_source_filter(session):
    """?source=shopify returns only items linked to that platform (idempotency-key prefix);
    powers the connector detail 'View synced products' deep-link."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from celerp.models.projections import Projection
    from celerp_inventory.routes import list_items

    cid = uuid.uuid4()
    session.add(Company(id=cid, name="SrcCo", slug=f"srcco-{cid.hex[:8]}"))
    await session.flush()
    now = datetime.now(timezone.utc)

    def _proj(eid, idem):
        return Projection(
            company_id=cid, entity_id=eid, entity_type="item",
            state={"sku": eid, "name": eid, "status": "available", "idempotency_key": idem},
            version=1, created_at=now, updated_at=now,
        )

    session.add_all([_proj("s1", "shopify:1:1"), _proj("w1", "woocommerce:2"), _proj("m1", "manual-x")])
    await session.flush()

    req = SimpleNamespace(query_params=SimpleNamespace(multi_items=lambda: []))
    resp = await list_items(req, company_id=cid, session=session, role="owner", source="shopify")
    assert {i["sku"] for i in resp["items"]} == {"s1"}
    resp_all = await list_items(req, company_id=cid, session=session, role="owner")
    assert {i["sku"] for i in resp_all["items"]} == {"s1", "w1", "m1"}
