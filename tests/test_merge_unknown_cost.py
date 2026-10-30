# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An unknown cost or price never becomes a number, and a number the books carry is never dropped.

- Merged selling prices are the quantity-weighted average of the stocked sources' unit prices,
  rounded to the rate ceiling, never divided by a hand-set resulting quantity. With no stocked
  source, a price every source agrees on is kept; differing ones are left unset.
- A merged lot keeps the book value of its sources. Its goods cost is known only when every
  source holding stock or value has a known goods cost; a source with nothing in it neither
  makes it known nor unknown. Sources without any cost give a lot without cost.
- Freight carried by a source stays freight on the merged lot, keyed by its bill.
- Clearing the goods cost of a lot that carries freight leaves its goods cost unknown, not 0.
- A lot with value and no units shows no unit cost.
- Priced goods received into a lot holding stock of unknown cost leave its goods cost unknown;
  into a sold-out lot they make it known at the received cost.
- Whether a lot's goods cost is known is the app's own field: no create, edit or import sets it.
"""
from __future__ import annotations

import uuid

import pytest
from celerp_inventory.services import goods_cost_empty

from test_cost_restatement import _state
from test_helpers import merge_items
from test_landed_cost_pools import _po_into
from test_landed_cost_removals import _seed

MARK = "goods_cost_unknown"


async def _item(client, auth, *, qty: float, **fields) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"MUC-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "sell_by": "piece",
        "category": "Beads", "status": "available", **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, auth, ids: list[str], **extra) -> str:
    r = await merge_items(client, headers=auth["headers"],
                          json={"source_entity_ids": ids, "target_sku_from": ids[0], **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _read(client, auth, item_id: str) -> dict:
    r = await client.get(f"/items/{item_id}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ── F1: selling prices ───────────────────────────────────────────────────────

async def test_empty_sources_with_one_price_keep_it(client, session, auth):
    a = await _item(client, auth, qty=0, retail_price=10.0)
    b = await _item(client, auth, qty=0, retail_price=10.0)
    m = await _merge(client, auth, [a, b])
    assert (await _state(session, auth, m)).get("retail_price") == 10.0


async def test_empty_sources_with_differing_prices_leave_it_unset(client, session, auth):
    a = await _item(client, auth, qty=0, retail_price=10.0)
    b = await _item(client, auth, qty=0, retail_price=12.0)
    m = await _merge(client, auth, [a, b])
    assert "retail_price" not in await _state(session, auth, m)


@pytest.mark.parametrize("resulting", [None, 0.0, 10.0, 3.0])
async def test_unit_price_is_the_stocked_average_whatever_quantity_is_set(client, session, auth, resulting):
    a = await _item(client, auth, qty=2, retail_price=10.0)
    b = await _item(client, auth, qty=3, retail_price=20.0)
    extra = {} if resulting is None else {"resulting_quantity": resulting}
    m = await _merge(client, auth, [a, b], **extra)
    assert (await _state(session, auth, m))["retail_price"] == 16.0


async def test_hand_set_quantity_over_empty_sources_keeps_their_price(client, session, auth):
    a = await _item(client, auth, qty=0, retail_price=10.0)
    b = await _item(client, auth, qty=0, retail_price=10.0)
    m = await _merge(client, auth, [a, b], resulting_quantity=5)
    assert (await _state(session, auth, m))["retail_price"] == 10.0


async def test_merged_price_is_at_the_rate_ceiling(client, session, auth):
    from celerp.services.auto_je import company_currency
    from celerp.services.money import round_rate

    a = await _item(client, auth, qty=1, retail_price=10.0)
    b = await _item(client, auth, qty=2, retail_price=10.01)
    m = await _merge(client, auth, [a, b])
    currency = await company_currency(session, auth["company_id"])
    assert (await _state(session, auth, m))["retail_price"] == float(round_rate((10 + 20.02) / 3, currency))


async def test_a_source_without_stock_neither_sets_nor_blocks_a_price(client, session, auth):
    a = await _item(client, auth, qty=4, retail_price=10.0)
    b = await _item(client, auth, qty=0)
    m = await _merge(client, auth, [a, b])
    assert (await _state(session, auth, m))["retail_price"] == 10.0


async def test_a_stocked_source_without_the_price_leaves_it_unset(client, session, auth):
    a = await _item(client, auth, qty=4, retail_price=10.0)
    b = await _item(client, auth, qty=1)
    m = await _merge(client, auth, [a, b])
    assert "retail_price" not in await _state(session, auth, m)


# ── F2: cost ─────────────────────────────────────────────────────────────────

async def test_sources_without_cost_give_a_lot_without_cost(client, session, auth):
    a = await _item(client, auth, qty=2)
    b = await _item(client, auth, qty=3)
    m = await _merge(client, auth, [a, b])
    st = await _state(session, auth, m)
    assert not {"cost_total", "cost_base", "cost_price", MARK} & set(st), st
    flat = await _read(client, auth, m)
    assert flat.get("cost_price") is None and flat.get("cost_total") is None


async def test_known_and_unknown_sources_keep_the_value_with_an_unknown_goods_cost(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    m = await _merge(client, auth, [a, b])
    st = await _state(session, auth, m)
    assert st["cost_total"] == 100.0 and st[MARK] is True, st
    assert MARK not in (st.get("attributes") or {})
    flat = await _read(client, auth, m)
    assert flat["cost_total"] == 100.0 and flat.get("cost_price") is None


async def test_known_sources_give_a_known_cost(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3, cost_total=60.0)
    m = await _merge(client, auth, [a, b])
    st = await _state(session, auth, m)
    assert st["cost_total"] == 160.0 and MARK not in st


async def test_an_empty_source_does_not_make_the_cost_unknown(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=0)
    m = await _merge(client, auth, [a, b])
    st = await _state(session, auth, m)
    assert st["cost_total"] == 100.0 and MARK not in st


async def test_empty_sources_waiting_at_one_unit_cost_keep_it(client, session, auth):
    a = await _item(client, auth, qty=0, cost_price=5.0)
    b = await _item(client, auth, qty=0, cost_price=5.0)
    m = await _merge(client, auth, [a, b])
    st = await _state(session, auth, m)
    assert st.get("cost_price") == 5.0 and "cost_total" not in st and MARK not in st, st


async def test_empty_sources_waiting_at_different_unit_costs_give_no_cost(client, session, auth):
    a = await _item(client, auth, qty=0, cost_price=5.0)
    b = await _item(client, auth, qty=0, cost_price=6.0)
    m = await _merge(client, auth, [a, b])
    st = await _state(session, auth, m)
    assert not {"cost_total", "cost_base", "cost_price", MARK} & set(st), st


async def test_freight_stays_freight_on_the_merged_lot(client, session, auth):
    await _seed(session, auth)
    lot = await _item(client, auth, qty=1, cost_total=10.0)
    doc = await _po_into(client, auth, lot, 9, 10.0, shipping=30.0)
    other = await _item(client, auth, qty=5, cost_total=50.0)
    before = await _state(session, auth, lot)
    pools = before["landed_costs"]
    assert pools and all(k.startswith(f"{doc}::") for k in pools)
    m = await _merge(client, auth, [lot, other])
    st = await _state(session, auth, m)
    assert st["landed_costs"] == pools
    assert st["cost_total"] == round(float(before["cost_total"]) + 50.0, 2)
    assert st["cost_base"] == round(st["cost_total"] - sum(pools.values()), 2)


# ── F3, F6: clearing a goods cost, value without units ────────────────────────

async def test_clearing_goods_cost_with_freight_leaves_it_unknown(client, session, auth):
    await _seed(session, auth)
    lot = await _item(client, auth, qty=1, cost_total=10.0)
    await _po_into(client, auth, lot, 9, 10.0, shipping=30.0)
    r = await client.patch(f"/items/{lot}", headers=auth["headers"], json={"fields_changed": {"cost_total": {"new": None}}})
    assert r.status_code == 200, r.text
    st = await _state(session, auth, lot)
    assert st[MARK] is True and st["cost_total"] == sum(st["landed_costs"].values()), st
    assert (await _read(client, auth, lot)).get("cost_price") is None


async def test_value_without_units_shows_no_unit_cost(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3, cost_total=60.0)
    m = await _merge(client, auth, [a, b], resulting_quantity=0)
    flat = await _read(client, auth, m)
    assert flat["cost_total"] == 160.0 and flat.get("cost_price") is None


# ── F7, F12: receipts into a lot on hand ─────────────────────────────────────

async def test_priced_goods_into_stock_of_unknown_cost_leave_it_unknown(client, session, auth):
    await _seed(session, auth)
    lot = await _item(client, auth, qty=3)
    await _po_into(client, auth, lot, 2, 10.0)
    st = await _state(session, auth, lot)
    assert st["cost_total"] == 20.0 and st[MARK] is True, st


async def test_priced_goods_into_a_sold_out_marked_lot_make_it_known(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    c = await _item(client, auth, qty=3)
    m = await _merge(client, auth, [a, c])
    assert (await _state(session, auth, m))[MARK] is True
    r = await client.post(f"/items/{m}/adjust", headers=auth["headers"], json={"new_qty": 0})
    assert r.status_code == 200, r.text
    assert goods_cost_empty(await _state(session, auth, m))
    await _po_into(client, auth, m, 2, 10.0)
    st = await _state(session, auth, m)
    assert st["cost_total"] == 20.0 and MARK not in st, st


# ── P0: the app's own field ──────────────────────────────────────────────────

@pytest.mark.parametrize("where", ["top", "attributes"])
async def test_no_create_sets_whether_the_cost_is_known(client, session, auth, where):
    body = {MARK: True} if where == "top" else {"attributes": {MARK: True}}
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"MUC-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 1, "sell_by": "piece", **body})
    assert r.status_code == 422, r.text


async def test_no_edit_sets_whether_the_cost_is_known(client, session, auth):
    a = await _item(client, auth, qty=1, cost_total=10.0)
    r = await client.patch(f"/items/{a}", headers=auth["headers"], json={"fields_changed": {MARK: {"new": True}}})
    assert r.status_code == 422, r.text
