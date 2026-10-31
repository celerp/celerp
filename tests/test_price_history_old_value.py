# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A price change records the value it replaced, and the activity feed shows old and new.

The server reads the replaced value from the locked item in the unit of the new price: the
goods basis for cost_total (landed cost is not part of it), the goods unit cost for
cost_price, the item's own key for a cost list, else the item's price on that list. Cost
amounts of every kind, old and new, stay out of the feeds of a role that cannot see costs.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from celerp.events.schemas import ItemPricingSet
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.activity_redaction import redact_event_costs
from test_helpers import merge_items, perm_setup
from test_landed_cost_pools import _goods, _received
from ui.components.activity import detail_from_entry


async def _item(client, headers, **over) -> str:
    body = {"sku": f"PH-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
            "status": "available", **over}
    r = await client.post("/items", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _price(client, headers, item_id: str, price_type: str, new_price: float, **extra) -> None:
    r = await client.post(f"/items/{item_id}/price", headers=headers,
                          json={"price_type": price_type, "new_price": new_price, **extra})
    assert r.status_code == 200, r.text


async def _pricing_rows(session, item_id: str) -> list[dict]:
    session.expire_all()
    rows = (await session.execute(
        select(LedgerEntry.data).where(LedgerEntry.entity_id == item_id,
                                       LedgerEntry.event_type == "item.pricing.set")
        .order_by(LedgerEntry.id))).scalars().all()
    return list(rows)


async def test_a_sell_price_change_records_and_shows_the_price_it_replaced(client, session, auth):
    item = await _item(client, auth["headers"])
    await _price(client, auth["headers"], item, "retail_price", 3000)
    await _price(client, auth["headers"], item, "retail_price", 3500)
    first, second = await _pricing_rows(session, item)
    assert first["old_price"] is None
    assert second["old_price"] == 3000
    assert detail_from_entry(first, "item.pricing.set", "USD") == "Retail: none → $3,000.00"
    assert detail_from_entry(second, "item.pricing.set", "USD") == "Retail: $3,000.00 → $3,500.00"


def test_a_change_recorded_before_old_values_reads_as_it_always_did():
    assert detail_from_entry({"price_type": "retail_price", "new_price": 3500.0}, "item.pricing.set", "USD") \
        == "Retail → $3,500.00"


async def test_the_replaced_value_is_the_servers_not_the_clients(client, session, auth):
    item = await _item(client, auth["headers"])
    await _price(client, auth["headers"], item, "retail_price", 3000)
    await _price(client, auth["headers"], item, "retail_price", 3500, old_price=1)
    assert (await _pricing_rows(session, item))[-1]["old_price"] == 3000


async def test_a_cost_total_change_replaces_the_goods_basis_not_the_landed_total(client, session, auth):
    _doc, [lot] = await _received(client, session, auth, [_goods(100.0, 10)], from_order=False, shipping=100.0)
    await _price(client, auth["headers"], lot, "cost_total", 1200)
    assert (await _pricing_rows(session, lot))[-1]["old_price"] == 1000


async def test_a_unit_cost_change_on_a_stocked_lot_replaces_its_goods_unit_cost(client, session, auth):
    item = await _item(client, auth["headers"], quantity=10, cost_total=500)
    await _price(client, auth["headers"], item, "cost_price", 60)
    assert (await _pricing_rows(session, item))[-1]["old_price"] == 50


@pytest.mark.parametrize("price_type", ["cost_price", "cost_total"])
async def test_a_goods_cost_change_on_a_lot_of_unknown_goods_cost_replaced_no_cost(client, session, auth, price_type):
    # A merge of a costed lot and an uncosted one keeps the value but no known goods cost.
    a = await _item(client, auth["headers"], quantity=2, cost_total=100)
    b = await _item(client, auth["headers"], quantity=3)
    r = await merge_items(client, headers=auth["headers"], json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    await _price(client, auth["headers"], lot, price_type, 30)
    assert (await _pricing_rows(session, lot))[-1]["old_price"] is None


async def test_a_garbage_stored_price_reads_as_no_previous_price(client, session, auth):
    item = await _item(client, auth["headers"])
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item})
    await session.execute(update(Projection)
                          .where(Projection.company_id == auth["company_id"], Projection.entity_id == item)
                          .values(state={**row.state, "retail_price": "abc"}))
    await session.commit()
    await _price(client, auth["headers"], item, "retail_price", 3500)
    assert (await _pricing_rows(session, item))[-1]["old_price"] is None


_AMOUNTS = sorted(set(ItemPricingSet.model_fields) - {"price_type"})


@pytest.mark.parametrize("price_type", ["cost_price", "cost_total", "Cost", "landed_price", "Landed", "Landed Cost"])
def test_every_amount_of_a_cost_pricing_event_is_redacted(price_type):
    data = {"price_type": price_type, **{k: 1.0 for k in _AMOUNTS}}
    out = redact_event_costs("item.pricing.set", data)
    assert out["price_type"] == price_type
    assert not set(_AMOUNTS) & set(out), out


def test_a_sell_pricing_event_keeps_its_amounts():
    data = {"price_type": "retail_price", **{k: 1.0 for k in _AMOUNTS}}
    assert redact_event_costs("item.pricing.set", data) == data


async def test_a_role_without_cost_access_reads_no_cost_list_amount(client, session):
    ctx = await perm_setup(client, session)
    item = await _item(client, ctx["admin_h"], quantity=0, cost_price=40)
    await _price(client, ctx["admin_h"], item, "Cost", 45)
    ledger = (await client.get("/ledger", params={"entity_id": item}, headers=ctx["operator_h"])).json()["items"]
    activity = (await client.get("/dashboard/activity", params={"limit": 100}, headers=ctx["operator_h"])).json()
    rows = [e["data"] for e in [*ledger, *activity["activities"]]
            if e["event_type"] == "item.pricing.set" and e["data"].get("price_type") == "Cost"]
    assert len(rows) == 2
    for data in rows:
        assert not set(_AMOUNTS) & set(data), data
