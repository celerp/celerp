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


# ── P2: the preview shows what the merge writes ─────────────────────────────

async def _preview(client, headers, ids: list[str], **extra) -> dict:
    r = await client.post("/items/merge/preview", headers=headers,
                          json={"source_entity_ids": ids, "target_sku_from": ids[0], **extra})
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def test_preview_shows_the_merged_prices_value_and_unknown_cost(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0, retail_price=10.0)
    b = await _item(client, auth, qty=3, retail_price=20.0)
    shown = await _preview(client, auth["headers"], [a, b], resulting_quantity=6)
    # Every stored list is named, None where the merged item will have no price.
    assert shown == {"retail_price": 16.0, "wholesale_price": None, "cost_total": 100.0, "cost_price": None,
                     "goods_cost_known": False}, shown
    flat = await _read(client, auth, await _merge(client, auth, [a, b], resulting_quantity=6))
    assert flat["retail_price"] == 16.0 and flat["cost_total"] == 100.0 and flat.get("cost_price") is None


async def test_preview_shows_a_known_unit_cost(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3, cost_total=50.0)
    shown = await _preview(client, auth["headers"], [a, b], resulting_quantity=6)
    assert shown["cost_total"] == 150.0 and shown["cost_price"] == 25.0 and shown["goods_cost_known"] is True


async def test_preview_hides_cost_from_a_role_that_cannot_see_it(client, session, auth):
    from test_company_settings_owners import _user_with_role
    a = await _item(client, auth, qty=2, cost_total=100.0, retail_price=10.0)
    b = await _item(client, auth, qty=3, cost_total=50.0, retail_price=10.0)
    operator = await _user_with_role(client, session, auth["headers"], "operator")
    assert await _preview(client, operator, [a, b]) == {"retail_price": 10.0, "wholesale_price": None}


# ── P3: correcting a part's cost through a merge ────────────────────────────

async def _set_cost(client, auth, item_id: str, cost: float | None) -> None:
    r = await client.patch(f"/items/{item_id}", headers=auth["headers"],
                           json={"fields_changed": {"cost_total": {"new": cost}}})
    assert r.status_code == 200, r.text


async def test_costing_the_unknown_part_makes_the_merged_cost_known(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    x = await _merge(client, auth, [a, b])
    await _set_cost(client, auth, b, 60.0)
    st = await _state(session, auth, x)
    assert st["cost_total"] == 160.0 and MARK not in st, st


async def test_correcting_the_known_part_moves_the_value_and_keeps_it_unknown(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    x = await _merge(client, auth, [a, b])
    await _set_cost(client, auth, a, 120.0)
    st = await _state(session, auth, x)
    assert st["cost_total"] == 120.0 and st[MARK] is True, st


async def test_a_correction_reaches_the_second_generation(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    c = await _item(client, auth, qty=1, cost_total=10.0)
    x = await _merge(client, auth, [a, b])
    y = await _merge(client, auth, [x, c])
    assert (await _state(session, auth, y))[MARK] is True
    await _set_cost(client, auth, b, 60.0)
    st = await _state(session, auth, y)
    assert st["cost_total"] == 170.0 and MARK not in st, st


async def test_a_cost_set_on_the_result_stands_through_a_part_correction(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    x = await _merge(client, auth, [a, b])
    await _set_cost(client, auth, x, 150.0)
    await _set_cost(client, auth, a, 110.0)
    st = await _state(session, auth, x)
    assert st["cost_total"] == 160.0 and MARK not in st, st


# ── P4: lots made from a lot keep whether its cost is known ─────────────────

async def test_a_split_of_an_unknown_cost_keeps_it_unknown(client, session, auth):
    from test_landed_cost_removals import _split
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    x = await _merge(client, auth, [a, b])
    [child] = await _split(client, auth, x, 2)
    for lot in (x, child):
        st = await _state(session, auth, lot)
        assert st[MARK] is True and MARK not in (st.get("attributes") or {}), st
    assert (await _state(session, auth, child))["cost_total"] == 40.0


async def test_a_transform_keeping_an_unknown_cost_keeps_it_unknown(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    x = await _merge(client, auth, [a, b])
    r = await client.post(f"/items/{x}/transform", headers=auth["headers"],
                          json={"child_sku": f"T-{uuid.uuid4().hex[:6]}", "child_quantity": 1,
                                "child_category": "Beads", "child_sell_by": "piece"})
    assert r.status_code == 200, r.text
    st = await _state(session, auth, r.json()["child_id"])
    assert st[MARK] is True and st["cost_total"] == 100.0, st


async def _made_from(client, session, auth, component: str) -> dict:
    """The lot a one-tap build of a product made from one unit of ``component`` produces."""
    from test_mfg_creation_contract import _build, _recipe
    made = await _item(client, auth, qty=0)
    assert (await _recipe(client, auth, made, [(component, 1)])).status_code == 200
    r = await _build(client, auth, made, 1, uuid.uuid4().hex, complete_it=True)
    assert r.status_code == 200, r.text
    run = await _state(session, auth, r.json()["id"])
    return await _state(session, auth, run["receipts"][0]["lot_item_id"])


async def test_goods_made_from_an_unknown_cost_are_of_unknown_cost(client, session, auth):
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    lot = await _made_from(client, session, auth, await _merge(client, auth, [a, b]))
    assert lot[MARK] is True and lot["cost_total"] == 20.0, lot


async def test_goods_made_from_a_known_cost_are_of_known_cost(client, session, auth):
    await _seed(session, auth)
    lot = await _made_from(client, session, auth, await _item(client, auth, qty=5, cost_total=100.0))
    assert MARK not in lot and lot["cost_total"] == 20.0, lot


async def test_a_recipe_with_an_unknown_component_has_no_cost(client, session, auth):
    from test_mfg_creation_contract import _recipe
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    known = await _item(client, auth, qty=5, cost_total=100.0)
    made = await _item(client, auth, qty=0)
    assert (await _recipe(client, auth, made, [(await _merge(client, auth, [a, b]), 1), (known, 1)])).status_code == 200
    recipe = (await _state(session, auth, made))["recipe"]
    assert recipe["unit_cost"] is None and recipe["materials_cost"] is None, recipe
    assert [c["unit_cost"] for c in recipe["components"]] == [None, 20.0]


@pytest.mark.parametrize("known", [True, False])
async def test_a_copy_keeps_a_known_cost_and_never_an_unknown_one(client, session, auth, known):
    from ui.routes.inventory import _duplicate_payload
    await _seed(session, auth)
    a = await _item(client, auth, qty=2, cost_total=100.0)
    source = a if known else await _merge(client, auth, [a, await _item(client, auth, qty=3)])
    payload = _duplicate_payload(await _read(client, auth, source), f"CP-{uuid.uuid4().hex[:6]}", can_set_prices=True)
    assert not {"cost_total", "cost_price", MARK} & set(payload.get("attributes") or {}), payload
    r = await client.post("/items", headers=auth["headers"], json=payload)
    assert r.status_code == 200, r.text
    copy = await _state(session, auth, r.json()["id"])
    if known:
        assert copy["cost_total"] == 100.0 and MARK not in copy, copy
    else:
        assert not {"cost_total", "cost_price", "cost_base", MARK} & set(copy), copy


# ── P5: a cost list prices a line only from a known goods cost ──────────────

async def _doc(client, auth, doc_type: str, lines: list[dict] | None = None) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": doc_type, "line_items": lines or []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _scanned(client, auth, doc_type: str, item_id: str, price_list: str) -> dict:
    doc = await _doc(client, auth, doc_type)
    sku = (await _read(client, auth, item_id))["sku"]
    r = await client.post(f"/docs/{doc}/scan", headers=auth["headers"],
                          json={"barcode": sku, "price_list": price_list, "run_key": uuid.uuid4().hex[:8]})
    assert r.status_code == 200 and r.json()["scanned"] == 1, r.text
    [line] = (await client.get(f"/docs/{doc}", headers=auth["headers"])).json()["line_items"]
    return line


async def _repriced(client, auth, doc_type: str, item_id: str, price_list: str) -> tuple[dict, dict]:
    doc = await _doc(client, auth, doc_type, [{"item_id": item_id, "description": "Beads", "quantity": 1,
                                               "unit_price": 7.0}])
    version = (await client.get(f"/docs/{doc}", headers=auth["headers"])).json()["version"]
    r = await client.post(f"/docs/{doc}/reprice", headers=auth["headers"],
                          json={"price_list": price_list, "expected_version": version})
    assert r.status_code == 200, r.text
    [line] = (await client.get(f"/docs/{doc}", headers=auth["headers"])).json()["line_items"]
    return r.json(), line


async def test_a_sold_out_lot_keeps_its_unit_cost_on_a_cost_list(client, session, auth):
    await _seed(session, auth)
    lot = await _item(client, auth, qty=0, cost_price=5.0)
    assert (await _read(client, auth, lot))["cost_price"] == 5.0
    assert (await _scanned(client, auth, "purchase_order", lot, "Cost"))["unit_price"] == 5.0


async def test_a_cost_list_leaves_an_unknown_cost_unpriced(client, session, auth):
    await _seed(session, auth)
    x = await _merge(client, auth, [await _item(client, auth, qty=2, cost_total=100.0),
                                    await _item(client, auth, qty=3)])
    assert "unit_price" not in await _scanned(client, auth, "purchase_order", x, "Cost")
    result, line = await _repriced(client, auth, "purchase_order", x, "Cost")
    assert {"item_id": x, "reason": "unpriced"} in result["skipped"] and line["unit_price"] == 7.0, result


async def test_a_sales_list_still_prices_an_unpriced_item_at_zero(client, session, auth):
    await _seed(session, auth)
    _result, line = await _repriced(client, auth, "invoice", await _item(client, auth, qty=2), "Retail")
    assert line["unit_price"] == 0.0


# ── F4, F8, F13 (D9): cost cells in the app ───────────────────────────────────

from celerp.models.ledger import LedgerEntry  # noqa: E402
from celerp.models.projections import Projection  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from ui.i18n import t  # noqa: E402

from test_list_old_rule_total_ui import _browser, _token  # noqa: E402


async def _counts(session, auth, lot: str) -> tuple[int, int]:
    """The lot's ledger events and the company's journal entries."""
    session.expire_all()
    cid = auth["company_id"]
    events = await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.entity_id == lot))
    jes = await session.scalar(select(func.count()).select_from(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "journal_entry"))
    return events, jes


async def _cost_column(session, auth) -> None:
    """The company keeps the system's price lists, Cost included, so its list shows the cost columns."""
    from celerp.services.pricing import PRICE_LISTS_FALLBACK
    from test_import_invariants import _set_company_settings
    await _set_company_settings(session, auth["company_id"], price_lists=PRICE_LISTS_FALLBACK)


async def _marked(client, auth) -> str:
    a = await _item(client, auth, qty=2, cost_total=100.0)
    b = await _item(client, auth, qty=3)
    return await _merge(client, auth, [a, b])


async def _with_freight(client, session, auth) -> str:
    """A lot of goods cost 100 carrying freight 30."""
    await _seed(session, auth)
    lot = await _item(client, auth, qty=1, cost_total=10.0)
    await _po_into(client, auth, lot, 9, 10.0, shipping=30.0)
    st = await _state(session, auth, lot)
    assert st["cost_base"] == 100.0 and sum(st["landed_costs"].values()) == 30.0, st
    return lot


def _cell(lot: str) -> str:
    return f"/api/items/{lot}/field/cost_price_total"


async def test_the_cost_total_cell_shows_value_known_zero_and_no_cost(client, session, auth):
    await _cost_column(session, auth)
    marked = await _marked(client, auth)
    zero = await _item(client, auth, qty=2, cost_total=0.0)
    none = await _item(client, auth, qty=2)
    async with _browser(_token(auth)) as ui:
        shown = {lot: (await ui.get(f"{_cell(lot)}/display")).text for lot in (marked, zero, none)}
    assert "100.00" in shown[marked] and t("inventory.goods_cost_unknown") in shown[marked]
    assert "0.00" in shown[zero] and t("inventory.goods_cost_unknown") not in shown[zero]
    assert "<span>--</span>" in shown[none] and t("inventory.goods_cost_unknown") not in shown[none]


async def test_an_unknown_cost_cell_opens_empty_and_clears_only_on_purpose(client, session, auth):
    await _cost_column(session, auth)
    lot = await _marked(client, auth)
    before = await _counts(session, auth, lot)
    async with _browser(_token(auth)) as ui:
        editor = (await ui.get(f"{_cell(lot)}/edit")).text
        assert 'value=""' in editor and t("inventory.clear_cost") in editor, editor
        assert (await ui.patch(_cell(lot), data={"value": ""})).status_code == 200
        assert await _counts(session, auth, lot) == before
        assert (await ui.patch(_cell(lot), data={"value": "", "clear": "1"})).status_code == 200
    st = await _state(session, auth, lot)
    assert st.get("cost_total") is None and MARK not in st, st


async def test_a_written_back_total_keeps_freight_separate(client, session, auth):
    lot = await _with_freight(client, session, auth)
    async with _browser(_token(auth)) as ui:
        before = await _counts(session, auth, lot)
        await ui.patch(_cell(lot), data={"value": "130"})
        assert await _counts(session, auth, lot) == before
        await ui.patch(_cell(lot), data={"value": "160"})
        st = await _state(session, auth, lot)
        assert st["cost_base"] == 130.0 and st["cost_total"] == 160.0, st
        refused = (await ui.patch(_cell(lot), data={"value": "20"})).text
    assert "30.00" in refused, refused
    assert (await _state(session, auth, lot))["cost_base"] == 130.0


async def test_confirming_the_carried_value_of_an_unknown_cost_books_nothing(client, session, auth):
    await _seed(session, auth)
    lot = await _marked(client, auth)
    events, jes = await _counts(session, auth, lot)
    async with _browser(_token(auth)) as ui:
        await ui.patch(_cell(lot), data={"value": "100"})
    st = await _state(session, auth, lot)
    assert MARK not in st and st["cost_total"] == 100.0, st
    assert await _counts(session, auth, lot) == (events + 1, jes)


@pytest.mark.parametrize("cost_list", [True, False])
async def test_an_exported_file_imported_again_keeps_cost_and_its_known_ness(client, session, auth, cost_list):
    """With the Cost list the file carries the lot's cost total, without it the unit cost:
    either one brought back unchanged changes no cost."""
    import csv
    import io
    if cost_list:
        await _cost_column(session, auth)
    marked = await _marked(client, auth)
    freight = await _with_freight(client, session, auth)
    h = auth["headers"]
    skus = {(await _read(client, auth, x))["sku"]: x for x in (marked, freight)}
    r = await client.get("/items/export/csv", headers=h)
    assert r.status_code == 200, r.text
    # The columns the app keeps for itself are left out of a file brought back in.
    own = {"id", "status", "created_at", "updated_at"}
    rows = [{k: v for k, v in row.items() if k not in own and v not in ("", None)}
            for row in csv.DictReader(io.StringIO(r.text)) if row.get("sku") in skus]
    assert len(rows) == 2, rows
    for row in rows:
        row["name"] = "Renamed"
    r = await client.post("/items/import/rows", headers=h, json={
        "rows": rows,
        "upsert": True, "idempotency_key": f"rt-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200 and not r.json().get("errors"), r.text
    m, f = await _state(session, auth, marked), await _state(session, auth, freight)
    assert m["name"] == "Renamed" and m[MARK] is True and m["cost_total"] == 100.0, m
    assert f["name"] == "Renamed" and f["cost_total"] == 130.0 and f["cost_base"] == 100.0, f
    below = next(row for row in rows if skus[row["sku"]] == freight)
    below = {k: v for k, v in below.items() if not k.startswith("cost_price")}
    below.update({"cost_price_total": "20"} if cost_list else {"cost_price": "2"})
    r = await client.post("/items/import/rows", headers=h, json={
        "rows": [below],
        "upsert": True, "idempotency_key": f"rt-{uuid.uuid4().hex[:6]}"})
    assert "30.00" in r.text, r.text
    assert (await _state(session, auth, freight))["cost_total"] == 130.0


async def test_the_transform_page_offers_no_number_for_an_unknown_cost(client, session, auth):
    lot = await _marked(client, auth)
    async with _browser(_token(auth)) as ui:
        page = (await ui.get("/api/items/bulk/transform-preview", params={"entity_id": lot})).text
    import re
    [field] = re.findall(r'<input[^>]*name="child_cost_total"[^>]*>', page)
    assert 'value=""' in field or "value=" not in field, field


async def test_the_merge_note_states_prices_value_and_unknown_cost(client, session, auth):
    a = await _item(client, auth, qty=2, cost_total=100.0, retail_price=10.0)
    b = await _item(client, auth, qty=3, retail_price=20.0)
    async with _browser(_token(auth)) as ui:
        r = await ui.post("/api/items/merge/preview", data={"selected": [a, b], "target_sku_from": a})
    message = r.json()["message"]
    assert "16.00" in message and "100.00" in message and t("inventory.goods_cost_unknown") in message, message


async def test_the_unit_cost_cell_saved_as_shown_keeps_freight_separate(client, session, auth):
    await _cost_column(session, auth)
    lot = await _with_freight(client, session, auth)
    before = await _counts(session, auth, lot)
    async with _browser(_token(auth)) as ui:
        await ui.patch(f"/api/items/{lot}/field/cost_price", data={"value": "13"})
        assert await _counts(session, auth, lot) == before
        await ui.patch(f"/api/items/{lot}/field/cost_price", data={"value": "16"})
    st = await _state(session, auth, lot)
    assert st["cost_base"] == 130.0 and st["cost_total"] == 160.0, st
