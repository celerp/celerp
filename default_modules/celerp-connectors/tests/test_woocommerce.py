# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""WooCommerce connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from datetime import datetime, timezone
import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from celerp.connectors.woocommerce import WooCommerceConnector, _base_url, _auth
from celerp.connectors.base import ConnectorContext, SyncEntity


@pytest.fixture
def woo():
    return WooCommerceConnector()


@pytest.fixture
def ctx():
    return ConnectorContext(
        company_id="test-co",
        access_token="ck_testkey:cs_testsecret",
        store_handle="https://store.example.com",
    )


@pytest.fixture
def mock_upsert_item():
    with patch("celerp_inventory.services.upsert_external_product", new_callable=AsyncMock, return_value=("created", "item:resolved")) as m:
        yield m


@pytest.fixture
def mock_upsert_order():
    with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_contact():
    with patch("celerp.connectors.upsert.upsert_contact_from_woocommerce", new_callable=AsyncMock, return_value="created") as m:
        yield m


# -- Auth helpers --

def test_auth_parses_key_secret(ctx):
    assert _auth(ctx) == ("ck_testkey", "cs_testsecret")


def test_auth_missing_raises(ctx):
    ctx.access_token = "nocohere"
    with pytest.raises(ValueError, match="consumer_key:consumer_secret"):
        _auth(ctx)


def test_base_url(ctx):
    assert _base_url(ctx) == "https://store.example.com/wp-json/wc/v3"


def test_base_url_strips_trailing_slash(ctx):
    ctx.store_handle = "https://store.example.com/"
    assert _base_url(ctx) == "https://store.example.com/wp-json/wc/v3"


def test_base_url_missing_handle_raises():
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle=None)
    with pytest.raises(ValueError):
        _base_url(ctx)


# -- Pagination --

@pytest.mark.asyncio
async def test_pagination_multiple_pages(woo, ctx, mock_upsert_item):
    """X-WP-TotalPages=2 should trigger a second request."""
    page1 = [{"id": 1, "name": "P1", "sku": "SKU-1", "regular_price": "10.00"}]
    page2 = [{"id": 2, "name": "P2", "sku": "SKU-2", "regular_price": "20.00"}]
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products")
        route.side_effect = [
            httpx.Response(200, json=page1, headers={"X-WP-TotalPages": "2"}),
            httpx.Response(200, json=page2, headers={"X-WP-TotalPages": "2"}),
        ]
        result = await woo.sync_products(ctx)
    assert result.created == 2
    assert len(route.calls) == 2


@pytest.mark.asyncio
async def test_pagination_single_page(woo, ctx, mock_upsert_item):
    """Single page (no header or TotalPages=1) makes exactly one request."""
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 1, "name": "P1", "sku": "SKU-1", "regular_price": "5.00"}
            ])  # no X-WP-TotalPages header -> defaults to 1
        )
        result = await woo.sync_products(ctx)
    assert result.created == 1
    assert len(route.calls) == 1


# -- sync_products --

@pytest.mark.asyncio
async def test_sync_products_creates_items(woo, ctx, mock_upsert_item):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 10, "name": "Widget", "sku": "WDG-001", "regular_price": "9.99"}
            ])
        )
        result = await woo.sync_products(ctx)
    assert result.created == 1
    assert result.entity == SyncEntity.PRODUCTS
    mock_upsert_item.assert_called_once()


@pytest.mark.asyncio
async def test_sync_products_imports_variations(woo, ctx, mock_upsert_item):
    """A variable product imports each variation as its own item, not the priceless parent."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[{"id": 20, "name": "Shirt", "type": "variable"}])
        )
        respx.get("https://store.example.com/wp-json/wc/v3/products/20/variations").mock(
            return_value=httpx.Response(200, json=[
                {"id": 201, "sku": "SHIRT-RED-L", "price": "19.99",
                 "attributes": [{"option": "Red"}, {"option": "L"}]},
                {"id": 202, "sku": "SHIRT-BLU-M", "price": "19.99",
                 "attributes": [{"option": "Blue"}, {"option": "M"}]},
            ])
        )
        result = await woo.sync_products(ctx)
    assert result.created == 2   # two variations; parent not imported as a sellable item
    identities = {(call.kwargs["product_id"], call.kwargs["variation_id"]) for call in mock_upsert_item.call_args_list}
    assert identities == {("20", "201"), ("20", "202")}


@pytest.mark.asyncio
async def test_virtual_product_is_service_only_when_woo_does_not_manage_stock(
    woo, ctx, mock_upsert_item
):
    products = [
        {
            "id": 50, "name": "Download", "sku": "VIRTUAL-NOSTOCK",
            "regular_price": "5.00", "virtual": True, "manage_stock": False,
        },
        {
            "id": 51, "name": "Virtual Stocked", "sku": "VIRTUAL-STOCK",
            "regular_price": "7.00", "virtual": True, "manage_stock": True,
            "stock_quantity": 3,
        },
    ]
    with patch.object(woo, "_pull_product_files", new=AsyncMock()), respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=products)
        )
        result = await woo.sync_products(ctx)

    assert result.created == 2
    calls = {
        call.kwargs["product_id"]: call.kwargs
        for call in mock_upsert_item.call_args_list
    }
    assert calls["50"]["inventory_type"] == "service"
    assert calls["50"]["sell_by"] == "service"
    assert calls["50"]["seed_quantity"] is False
    assert calls["51"]["inventory_type"] is None
    assert calls["51"]["sell_by"] is None
    assert calls["51"]["seed_quantity"] is True
    assert calls["51"]["quantity"] == 3.0


@pytest.mark.asyncio
async def test_sync_products_fallback_sku(woo, ctx, mock_upsert_item):
    """When sku is blank, fall back to WC-{id}."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 42, "name": "No SKU Product", "sku": "", "regular_price": "5.00"}
            ])
        )
        result = await woo.sync_products(ctx)
    assert result.created == 1
    assert mock_upsert_item.call_args.kwargs["sku"] == "WC-42"


@pytest.mark.asyncio
async def test_sync_products_uses_basic_auth(woo, ctx, mock_upsert_item):
    """Verify Basic Auth credentials are sent in request."""
    captured = {}
    with respx.mock:
        def capture(request):
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=[])
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(side_effect=capture)
        await woo.sync_products(ctx)
    import base64
    expected = base64.b64encode(b"ck_testkey:cs_testsecret").decode()
    assert captured["auth"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_sync_products_incremental(woo, ctx, mock_upsert_item):
    """Since parameter adds modified_after to request."""
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_products(ctx, since=since)
    assert "modified_after" in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_sync_products_reconcile_pulls_every_product(woo, ctx, mock_upsert_item):
    """The daily reconciliation pass is a full pull, so deleted products are found."""
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_products(ctx, since=since, reconcile=True)
    assert "modified_after" not in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_sync_products_api_error(woo, ctx):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(500)
        )
        result = await woo.sync_products(ctx)
    assert result.errors
    assert result.created == 0


# -- sync_orders --

@pytest.mark.asyncio
async def test_sync_orders_creates(woo, ctx, mock_upsert_order):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[{"id": 1001, "status": "processing"}])
        )
        result = await woo.sync_orders(ctx)
    assert result.created == 1
    assert result.entity == SyncEntity.ORDERS
    mock_upsert_order.assert_called_once()


@pytest.mark.asyncio
async def test_sync_orders_failures_become_attention_not_errors(woo, ctx):
    """An order the import cannot complete is handed to a person, not counted as a
    sync error: the run succeeds so the watermark can advance, and every failed
    order appears on the attention list with its reason."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[
                {"id": 1, "number": "1001"}, {"id": 2, "number": "1002"}, {"id": 3}
            ])
        )
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, side_effect=ValueError("no stock")):
            result = await woo.sync_orders(ctx)
    assert not result.errors
    assert [a["id"] for a in result.attention] == ["1", "2", "3"]
    assert result.attention[0]["label"] == "Order 1001"
    assert result.attention[2]["label"] == "Order 3"
    assert all(a["reason"] == "no stock" for a in result.attention)


@pytest.mark.asyncio
async def test_sync_orders_attention_carries_the_reconciliation_signature(woo, ctx):
    """An order change only a person can reconcile carries the signature they
    mark; other reasons carry none, and a clean run returns an empty list, which
    clears the previous one."""
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    async def _upsert(company_id, order):
        if order["id"] == 1:
            raise WooCommerceReconciliationRequired("has a refund", "sig-1")
        raise ValueError("no stock")

    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        )
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce",
                   new_callable=AsyncMock, side_effect=_upsert):
            result = await woo.sync_orders(ctx)
    assert result.attention[0]["signature"] == "sig-1"
    assert "signature" not in result.attention[1]

    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[])
        )
        clean = await woo.sync_orders(ctx)
    assert clean.attention == []


def _orders_by_params(pages: dict):
    """respx side effect: answer the carried-id fetch and the incremental fetch
    separately, keyed on whether the request carries an ``include`` list."""
    def _respond(request):
        params = dict(request.url.params)
        key = "include" if "include" in params else "list"
        return httpx.Response(200, json=pages.get(key, []))
    return _respond


@pytest.mark.asyncio
async def test_sync_orders_retries_carried_attention_by_id(woo, ctx):
    """Entries carried from the previous run are re-fetched by id first. One that
    imports drops off, one that still fails keeps its reason, one WooCommerce no
    longer returns is held for a person when it was imported (a mark on its
    earlier state does not cover the deletion) and dropped when it never was,
    and an order fetched by id is not imported twice when the incremental page
    returns it as well."""
    carried = [
        {"id": "7", "label": "Order 7", "reason": "old reason"},
        {"id": "8", "label": "Order 8", "reason": "old reason"},
        {"id": "9", "label": "Order 9", "reason": "gone"},
        {"id": "11", "label": "Order 11", "reason": "refund", "signature": "s11",
         "reconciled": False},
        {"id": "12", "label": "Order 12", "reason": "refund", "signature": "s12",
         "reconciled": True},
    ]

    async def _upsert(company_id, order):
        if order["id"] == 8:
            raise ValueError("still no stock")
        return "created"

    async def _hold(company_id, order_id):
        return None if order_id == "9" else {"id": order_id, "signature": "gone"}

    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            side_effect=_orders_by_params({
                "include": [{"id": 7}, {"id": 8}],
                "list": [{"id": 7}, {"id": 10}],
            })
        )
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, side_effect=_upsert) as up, \
             patch("celerp.connectors.upsert.hold_missing_woocommerce_order", new_callable=AsyncMock, side_effect=_hold) as hold:
            result = await woo.sync_orders(ctx, attention=carried)

    assert route.calls[0].request.url.params["include"] == "7,8,9,11,12"
    assert not result.errors
    assert result.created == 2  # 7 (retried) and 10; 7 is not imported a second time
    assert [a["id"] for a in result.attention] == ["8", "11", "12"]
    assert result.attention[0]["reason"] == "still no stock"
    assert result.attention[1:] == [
        {"id": "11", "signature": "gone"}, {"id": "12", "signature": "gone"},
    ]
    assert [c.args[1] for c in hold.await_args_list] == ["9", "11", "12"]
    assert sorted(c.args[1]["id"] for c in up.await_args_list) == [7, 8, 10]


@pytest.mark.asyncio
async def test_sync_orders_reconcile_holds_imported_orders_missing_from_the_store(woo, ctx):
    """The reconciliation pass checks, by id only, that every imported order
    not already seen this run still exists; each missing one is held for a
    person and nothing is imported or voided for it."""
    async def _hold(company_id, order_id):
        return {"id": order_id, "signature": "gone"}

    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            side_effect=_orders_by_params({
                "include": [{"id": 31}],
                "list": [{"id": 30}],
            })
        )
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, return_value="noop") as up, \
             patch("celerp.connectors.upsert.list_imported_woocommerce_order_ids", new_callable=AsyncMock, return_value=["30", "31", "32"]), \
             patch("celerp.connectors.upsert.hold_missing_woocommerce_order", new_callable=AsyncMock, side_effect=_hold) as hold:
            result = await woo.sync_orders(ctx, reconcile=True)

    check = route.calls[1].request.url.params
    assert check["include"] == "31,32"
    assert check["_fields"] == "id"
    assert not result.errors
    assert result.attention == [{"id": "32", "signature": "gone"}]
    assert [c.args[1] for c in hold.await_args_list] == ["32"]
    assert [c.args[1]["id"] for c in up.await_args_list] == [30]


@pytest.mark.asyncio
async def test_sync_orders_without_reconcile_does_not_check_imported_orders(woo, ctx):
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[])
        )
        with patch("celerp.connectors.upsert.list_imported_woocommerce_order_ids", new_callable=AsyncMock) as imported:
            await woo.sync_orders(ctx)
    imported.assert_not_awaited()
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_sync_orders_keeps_a_reconciled_entry_until_the_order_changes(woo, ctx):
    """An order a person marked reconciled stays on the list, still marked and
    with its Undo, for as long as WooCommerce returns the state they reviewed.
    Once the order changes the mark goes: a change that imports drops off and
    one that needs a person again comes back as a new open entry."""
    from celerp_docs.doc_service import (
        WooCommerceReconciliationRequired,
        woocommerce_reconciliation_signature,
    )

    orders = {
        21: {"id": 21, "status": "refunded", "total": "5.00"},
        22: {"id": 22, "status": "completed", "total": "6.00"},
        23: {"id": 23, "status": "refunded", "total": "7.00",
             "refunds": [{"id": 1, "total": "-7.00"}]},
    }
    reviewed = {
        21: woocommerce_reconciliation_signature(orders[21]),
        22: "reviewed-while-refunded",
        23: "reviewed-before-the-second-refund",
    }
    carried = [
        {"id": str(order_id), "label": f"Order {order_id}", "reason": "refund",
         "signature": signature, "reconciled": True}
        for order_id, signature in reviewed.items()
    ]

    async def _upsert(company_id, order):
        if order["id"] == 23:
            raise WooCommerceReconciliationRequired("has another refund", signature="new")
        return "noop"

    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            side_effect=_orders_by_params({
                "include": list(orders.values()),
                "list": [],
            })
        )
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, side_effect=_upsert):
            result = await woo.sync_orders(ctx, attention=carried)

    assert not result.errors
    assert result.attention == [
        carried[0],
        {"id": "23", "label": "Order 23", "reason": "has another refund", "signature": "new"},
    ]


@pytest.mark.asyncio
async def test_sync_orders_api_error_on_carried_fetch_keeps_attention(woo, ctx, mock_upsert_order):
    """A failed run must not lose the attention list it was carrying."""
    carried = [{"id": "7", "label": "Order 7", "reason": "no stock"}]
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(500, json={"message": "down"})
        )
        result = await woo.sync_orders(ctx, attention=carried)
    assert result.errors and "API error" in result.errors[0]
    assert result.attention == carried
    mock_upsert_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_orders_carried_fetch_failing_midway_keeps_retried_outcomes(woo, ctx, monkeypatch):
    """When a later carried page fails, orders already retried keep their new
    outcome (resolved ones drop off, a changed one carries its new reason and
    signature) and only the unfetched ones stay as carried."""
    from celerp.connectors import woocommerce as woo_mod
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    monkeypatch.setattr(woo_mod, "_PER_PAGE", 1)
    carried = [
        {"id": "6", "label": "Order 6", "reason": "no stock"},
        {"id": "7", "label": "Order 7", "reason": "has a refund", "signature": "old"},
        {"id": "8", "label": "Order 8", "reason": "no stock"},
    ]

    def _respond(request):
        order_id = int(request.url.params["include"])
        if order_id == 8:
            return httpx.Response(500, json={"message": "down"})
        first_page = request.url.params.get("page", "1") == "1"
        return httpx.Response(200, json=[{"id": order_id}] if first_page else [])

    async def _upsert(company_id, order):
        if order["id"] == 7:
            raise WooCommerceReconciliationRequired("has another refund", signature="new")
        return "updated"

    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(side_effect=_respond)
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, side_effect=_upsert):
            result = await woo.sync_orders(ctx, attention=carried)

    assert result.errors and "API error" in result.errors[0]
    assert [a["id"] for a in result.attention] == ["7", "8"]
    assert result.attention[0]["signature"] == "new"
    assert result.attention[0]["reason"] == "has another refund"
    assert result.attention[1] == carried[2]


@pytest.mark.asyncio
async def test_sync_orders_incremental(woo, ctx, mock_upsert_order):
    since = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_orders(ctx, since=since)
    assert "modified_after" in str(route.calls[0].request.url)


# -- sync_contacts --

@pytest.mark.asyncio
async def test_sync_contacts_creates(woo, ctx, mock_upsert_contact):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/customers").mock(
            return_value=httpx.Response(200, json=[
                {"id": 5, "email": "test@example.com", "first_name": "Alice", "last_name": "Smith"}
            ])
        )
        result = await woo.sync_contacts(ctx)
    assert result.created == 1
    assert result.entity == SyncEntity.CONTACTS


@pytest.mark.asyncio
async def test_sync_contacts_incremental(woo, ctx, mock_upsert_contact):
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/customers").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_contacts(ctx, since=since)
    assert "modified_after" in str(route.calls[0].request.url)



# -- sync_inventory_out --

@pytest.mark.asyncio
async def test_sync_inventory_out_uses_variation_endpoint(woo, ctx):
    item = {
        "sku": "V-1", "quantity": 7,
        "woocommerce_product_id": "20", "woocommerce_variation_id": "201",
        "external_link": {"product_id": "20", "variation_id": "201", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/20/variations/201"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_inventory_out(ctx)
    assert result.updated == 1
    assert route.calls[0].request.content == b'{"stock_quantity":7}'


@pytest.mark.asyncio
async def test_sync_inventory_out_refuses_fractional_without_write(woo, ctx):
    item = {
        "sku": "F-1", "quantity": 1.5, "woocommerce_product_id": "10",
        "external_link": {"product_id": "10", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put("https://store.example.com/wp-json/wc/v3/products/10")
            result = await woo.sync_inventory_out(ctx)
    assert result.updated == 0
    assert result.errors and "fractional stock" in result.errors[0]
    assert len(route.calls) == 0


@pytest.mark.asyncio
async def test_sync_inventory_out_refuses_parent_managed_variation(woo, ctx):
    item = {
        "sku": "V-1", "quantity": 3,
        "woocommerce_product_id": "20", "woocommerce_variation_id": "201",
        "external_link": {"product_id": "20", "variation_id": "201", "manage_stock": "parent"},
    }
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=[item]):
        result = await woo.sync_inventory_out(ctx)
    assert result.updated == 0
    assert result.errors and "managed by its parent" in result.errors[0]


# -- sync_products_out --

@pytest.mark.asyncio
async def test_sync_products_out_uses_nested_variation_endpoint(woo, ctx):
    item = {
        "sku": "SHIRT-RED", "name": "Shirt - Red", "description": "Red shirt",
        "sale_price": 19.99, "files": [],
        "woocommerce_product_id": "20", "woocommerce_variation_id": "201",
        "external_link": {"product_id": "20", "variation_id": "201", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/20/variations/201"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_products_out(ctx)
    assert result.updated == 1
    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["sku"] == "SHIRT-RED"
    assert payload["regular_price"] == "19.99"
    assert payload["description"] == "Red shirt"
    assert "name" not in payload


@pytest.mark.asyncio
async def test_sync_products_out_simple_product_includes_core_fields(woo, ctx):
    item = {
        "sku": "W-1", "name": "Widget", "description": "Useful",
        "sale_price": 12.5, "files": [], "woocommerce_product_id": "10",
        "external_link": {"product_id": "10", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_products_out(ctx)
    assert result.updated == 1
    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload == {
        "sku": "W-1", "description": "Useful",
        "regular_price": "12.5", "name": "Widget",
    }



def test_woocommerce_commercial_fingerprint_ignores_status_only_changes():
    from celerp_docs.doc_service import _woocommerce_commercial_fingerprint
    base = {
        "currency": "USD", "status": "processing", "total": "12.00", "total_tax": "2.00",
        "line_items": [{"product_id": 1, "variation_id": 0, "sku": "A", "quantity": 1, "total": "10.00", "total_tax": "2.00"}],
        "shipping_lines": [], "fee_lines": [],
    }
    changed_status = {**base, "status": "completed"}
    assert _woocommerce_commercial_fingerprint(base) == _woocommerce_commercial_fingerprint(changed_status)


def test_woocommerce_commercial_fingerprint_detects_financial_change():
    from celerp_docs.doc_service import _woocommerce_commercial_fingerprint
    base = {
        "currency": "USD", "total": "10.00", "total_tax": "0",
        "line_items": [{"product_id": 1, "variation_id": 0, "sku": "A", "quantity": 1, "total": "10.00"}],
        "shipping_lines": [], "fee_lines": [],
    }
    changed = {**base, "total": "11.00"}
    assert _woocommerce_commercial_fingerprint(base) != _woocommerce_commercial_fingerprint(changed)


@pytest.mark.asyncio
async def test_register_webhooks_rolls_back_partial_creation(woo, ctx):
    with respx.mock:
        first = respx.post("https://store.example.com/wp-json/wc/v3/webhooks").mock(
            side_effect=[
                httpx.Response(201, json={"id": 11}),
                httpx.Response(403, json={"message": "read only"}),
            ]
        )
        cleanup = respx.delete(
            "https://store.example.com/wp-json/wc/v3/webhooks/11"
        ).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(httpx.HTTPStatusError):
            await woo.register_webhooks(ctx, "https://relay.test/hook", secret="s")
    assert len(first.calls) == 2
    assert len(cleanup.calls) == 1


@pytest.mark.asyncio
async def test_deregister_webhooks_tolerates_missing_hook(woo, ctx):
    with respx.mock:
        respx.delete("https://store.example.com/wp-json/wc/v3/webhooks/11").mock(
            return_value=httpx.Response(404)
        )
        await woo.deregister_webhooks(ctx, ["11"])


@pytest.mark.asyncio
async def test_sync_inventory_identity_out_pushes_only_selected_product(woo, ctx):
    items = [
        {
            "quantity": 2, "woocommerce_product_id": "10",
            "external_link": {"product_id": "10", "manage_stock": True},
        },
        {
            "quantity": 9, "woocommerce_product_id": "11",
            "external_link": {"product_id": "11", "manage_stock": True},
        },
    ]
    with patch(
        "celerp.connectors.upsert.list_items_with_external_id",
        new_callable=AsyncMock, return_value=items,
    ):
        with respx.mock:
            selected = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            other = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/11"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_inventory_identity_out(ctx, "10")
    assert result.updated == 1
    assert len(selected.calls) == 1
    assert len(other.calls) == 0


@pytest.mark.asyncio
async def test_full_product_sync_runs_missing_link_reconciliation(woo, ctx):
    reconcile = AsyncMock(return_value=2)
    with patch.object(woo, "_reconcile_missing_product_links", new=reconcile), respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await woo.sync_products(ctx, since=None)

    reconcile.assert_awaited_once_with(ctx, set(), set())
    assert result.updated == 2


@pytest.mark.asyncio
async def test_incremental_product_sync_does_not_infer_remote_deletions(woo, ctx):
    reconcile = AsyncMock(return_value=0)
    with patch.object(woo, "_reconcile_missing_product_links", new=reconcile), respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_products(ctx, since=datetime.now(timezone.utc))

    reconcile.assert_not_awaited()
