# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One rule for an item a connector creates from an accounting system.

Without a cost there is nothing to book, so it arrives available: it records the
opening inventory account and books no money, and a store order can sell it straight
away. With a cost it arrives as a draft with nothing booked, and making it available
books its value as opening stock.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.models.projections import Projection
from stock_books import assert_books_carry_stock
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_posting_roles_ingress import _FIELD, _items_by_sku, _opening_entries, connector_session  # noqa: F401
from test_posting_roles_older_stock import _make_available
from test_services.test_connector_upsert_integration import _seed_company, use_test_session  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _from_the_books(company_id, sku: str, cost: float | None, **fields) -> str:
    import celerp.connectors.upsert as connector
    from celerp_inventory.routes import ItemCreate

    item = {"sku": sku, "name": "From the books", "sell_by": "piece", "quantity": 3, "cost_price": cost,
            "sale_price": None, "idempotency_key": f"quickbooks:{sku}", **fields}
    return await connector.upsert_item(str(company_id), ItemCreate(**item))


@pytest.mark.parametrize("cost", [None, 0.0])
async def test_an_item_without_a_cost_arrives_available_with_nothing_booked(connector_session, auth, cost):
    session, cid = connector_session, auth["company_id"]
    assert await _from_the_books(cid, "QB-NC", cost) == "created"
    [lot] = await _items_by_sku(session, cid, "QB-NC")
    assert (lot.state["status"], lot.state.get(_FIELD)) == ("available", "1130-OB")
    assert await _opening_entries(session, cid) == []
    await assert_books_carry_stock(session, cid)

    assert await _from_the_books(cid, "QB-NC", cost, name="Renamed") == "updated"
    [lot] = await _items_by_sku(session, cid, "QB-NC")
    assert (lot.state["status"], lot.state["name"], lot.state.get(_FIELD)) == ("available", "Renamed", "1130-OB")
    assert await _opening_entries(session, cid) == []


async def test_an_item_with_a_cost_arrives_as_a_draft_and_is_booked_when_made_available(
        connector_session, client, auth):
    session, cid = connector_session, auth["company_id"]
    assert await _from_the_books(cid, "QB-C", 20.0) == "created"
    [lot] = await _items_by_sku(session, cid, "QB-C")
    assert (lot.state["status"], lot.state.get(_FIELD)) == ("draft", None)
    assert await _opening_entries(session, cid) == []
    await assert_books_carry_stock(session, cid)

    await _make_available(client, auth, lot.entity_id)
    [lot] = await _items_by_sku(session, cid, "QB-C")
    assert (lot.state["status"], lot.state.get(_FIELD)) == ("available", "1130-OB")
    [entry] = await _opening_entries(session, cid)
    assert {e["account"]: e.get("debit", 0) for e in entry["entries"]}["1130-OB"] == 60
    assert await assert_books_carry_stock(session, cid) == {"1130-P": 0, "1130-OB": 60}


async def _statuses(session, cid, sku: str) -> list[tuple[str, float]]:
    return sorted((lot.state["status"], lot.state["quantity"]) for lot in await _items_by_sku(session, cid, sku))


def _woo_order(order_id: int, sku: str, status: str) -> dict:
    return {"id": order_id, "number": str(order_id), "status": status, "currency": "USD",
            "total": "15.00", "total_tax": "0",
            "line_items": [{"product_id": order_id + 1, "variation_id": 0, "sku": sku, "name": "From the books",
                            "quantity": 1, "total": "15.00", "total_tax": "0"}],
            "shipping_lines": [], "fee_lines": []}


async def test_a_store_order_sells_an_item_without_a_cost_from_the_books(use_test_session):
    """WooCommerce binds the order to the item, reserves it, finalizes the invoice, and
    on completion fulfills it; nothing is booked as opening stock along the way."""
    import celerp.connectors.upsert as connector

    session = use_test_session
    cid = await _seed_company(session, "WooBooks")
    sku = f"QB-W-{uuid.uuid4().hex[:6]}"
    await _from_the_books(cid, sku, None)

    assert await connector.upsert_order_from_woocommerce(str(cid), _woo_order(801, sku, "processing")) == "created"
    session.expire_all()
    doc = await session.get(Projection, {"company_id": cid, "entity_id": "doc:woocommerce:order:801"})
    assert doc.state["finalized"] is True
    [line] = doc.state["line_items"]
    assert await _statuses(session, cid, sku) == [("available", 2), ("reserved", 1)]
    assert (await session.get(Projection, {"company_id": cid, "entity_id": line["item_id"]})).state["sku"] == sku

    assert await connector.upsert_order_from_woocommerce(str(cid), _woo_order(801, sku, "completed")) == "updated"
    assert await _statuses(session, cid, sku) == [("available", 2), ("sold", 1)]
    assert await _opening_entries(session, cid) == []
    await assert_books_carry_stock(session, cid)


async def test_a_shopify_order_for_an_item_without_a_cost_syncs_and_finalizes(connector_session, client, auth):
    import celerp.connectors.upsert as connector

    session, cid = connector_session, auth["company_id"]
    await _from_the_books(cid, "QB-S", None)
    order = {"id": 9301, "name": "#9301", "financial_status": "pending", "currency": "USD",
             "line_items": [{"title": "From the books", "quantity": 1, "price": "15.00"}], "total_price": "15.00"}
    assert await connector.upsert_order_from_shopify(str(cid), order) == "created"
    await session.commit()

    r = await client.post("/docs/doc:shopify:order:9301/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    r = await client.get("/docs/doc:shopify:order:9301", headers=auth["headers"])
    assert r.json()["finalized"] is True, r.text
    [lot] = await _items_by_sku(session, cid, "QB-S")
    assert lot.state["status"] == "available"
