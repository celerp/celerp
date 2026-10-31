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
from ui.i18n import t


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
async def test_product_without_sku_not_imported(woo, ctx, mock_upsert_item):
    """A store product with no SKU is not imported under a made-up one; it waits on a
    person to give it a SKU in WooCommerce."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 42, "name": "No SKU Product", "sku": "", "regular_price": "5.00"},
                {"id": 43, "name": "Shirt", "type": "variable", "sku": ""},
                {"id": 44, "name": "Has SKU", "sku": "HAS-1", "regular_price": "1.00"},
            ])
        )
        respx.get("https://store.example.com/wp-json/wc/v3/products/43/variations").mock(
            return_value=httpx.Response(200, json=[
                {"id": 7, "sku": " ", "attributes": [{"option": "Red"}]},
            ])
        )
        result = await woo.sync_products(ctx)
    assert [c.kwargs["sku"] for c in mock_upsert_item.call_args_list] == ["HAS-1"]
    assert result.created == 1
    no_sku = {e["id"]: e for e in result.attention}
    assert set(no_sku) == {"42", "43:7"}
    assert no_sku["42"]["label"] == "No SKU Product"
    assert no_sku["42"]["reason"] == t("connectors.attention_no_sku", service="WooCommerce")
    assert no_sku["43:7"]["label"] == "Shirt - Red"


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


# -- sync_inventory_out --

@pytest.mark.usefixtures("inside_execution")
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

@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_sync_products_out_uses_nested_variation_endpoint(woo, ctx):
    item = {
        "entity_id": "item:shirt-red",
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
            with patch.object(woo, "_save_push", new=AsyncMock()) as saved:
                result = await woo.sync_products_out(ctx)
    assert result.updated == 1
    saved.assert_awaited_once()
    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["sku"] == "SHIRT-RED"
    assert payload["regular_price"] == "19.99"
    assert payload["description"] == "Red shirt"
    assert "name" not in payload


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_sync_products_out_simple_product_includes_core_fields(woo, ctx):
    item = {
        "entity_id": "item:w-1",
        "sku": "W-1", "name": "Widget", "description": "Useful",
        "sale_price": 12.5, "files": [], "woocommerce_product_id": "10",
        "external_link": {"product_id": "10", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            with patch.object(woo, "_save_push", new=AsyncMock()):
                result = await woo.sync_products_out(ctx)
    assert result.updated == 1
    assert len(route.calls) == 1  # no images of its own: the store's are left alone
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


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_deregister_webhooks_removes_hooks_whose_ids_were_never_saved(woo, ctx):
    """A hook created while its response was lost is found by where it delivers."""
    hooks = [
        {"id": 21, "name": "Celerp order.created", "delivery_url": "https://relay.test/hook"},
        {"id": 22, "name": "Celerp order.created", "delivery_url": "https://other.test/hook"},
        {"id": 23, "name": "Shipping app", "delivery_url": "https://relay.test/hook"},
    ]
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/webhooks").mock(
            return_value=httpx.Response(200, json=hooks)
        )
        known = respx.delete("https://store.example.com/wp-json/wc/v3/webhooks/11").mock(
            return_value=httpx.Response(404)
        )
        lost = respx.delete("https://store.example.com/wp-json/wc/v3/webhooks/21").mock(
            return_value=httpx.Response(200, json={})
        )
        await woo.deregister_webhooks(ctx, "https://relay.test/hook", ["11"])
    assert known.called
    assert lost.called


@pytest.mark.asyncio
async def test_deregister_webhooks_fails_when_a_hook_cannot_be_removed(woo, ctx):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/webhooks").mock(
            return_value=httpx.Response(200, json=[
                {"id": 21, "name": "Celerp order.created", "delivery_url": "https://relay.test/hook"},
            ])
        )
        respx.delete("https://store.example.com/wp-json/wc/v3/webhooks/21").mock(
            return_value=httpx.Response(403)
        )
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await woo.deregister_webhooks(ctx, "https://relay.test/hook", [])


@pytest.mark.asyncio
async def test_deregister_webhooks_fails_when_the_store_cannot_list_hooks(woo, ctx):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/webhooks").mock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await woo.deregister_webhooks(ctx, "https://relay.test/hook", ["11"])


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_sync_inventory_identity_out_pushes_only_selected_product(woo, ctx):
    items = [
        {
            "quantity": 2, "woocommerce_product_id": "10",
            "external_link": {"product_id": "10", "manage_stock": True},
        },
    ]
    with patch(
        "celerp.connectors.upsert.list_item_for_external_identity",
        new_callable=AsyncMock, return_value=items,
    ) as lookup:
        with respx.mock:
            selected = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            other = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/11"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_inventory_identity_out(ctx, "10")
    lookup.assert_awaited_once_with(ctx.company_id, "woocommerce", "10", None)
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


# -- Per-record directions --

from connector_test_support import add_item, connect, connector_session, seed_company  # noqa: E402,F401

_WIDEST = {"products": "both", "stock": "outbound", "orders": "inbound", "customers": "off"}


@pytest.mark.asyncio
@respx.mock
async def test_publish_refused_when_products_inbound_only(woo, connector_session):
    session = connector_session
    cid = await seed_company(session, "InboundOnly")
    await add_item(session, cid, "item:pub", "PUB-1")
    await connect(session, cid, "woocommerce", {**_WIDEST, "products": "inbound"})
    ctx = ConnectorContext(company_id=str(cid), access_token="ck:cs",
                           store_handle="https://store.example.com")
    respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(200, json=[]))
    create = respx.post("https://store.example.com/wp-json/wc/v3/products")

    with pytest.raises(ValueError, match="Products are set to WooCommerce to Celerp"):
        await woo.ensure_product_link(ctx, "item:pub")
    assert not create.called


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_publish_allowed_when_products_go_out(woo, connector_session):
    session = connector_session
    cid = await seed_company(session, "OutboundPublish")
    await add_item(session, cid, "item:pub", "PUB-2")
    await connect(session, cid, "woocommerce", {**_WIDEST, "products": "outbound"})
    ctx = ConnectorContext(company_id=str(cid), access_token="ck:cs",
                           store_handle="https://store.example.com")
    respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(200, json=[]))
    respx.post("https://store.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(201, json={"id": 501, "type": "simple", "manage_stock": True}))

    assert await woo.ensure_product_link(ctx, "item:pub") == "item:pub"


def test_customers_not_bulk_imported(woo):
    from celerp.connectors.sync_runner import implements, sync_plan

    assert not implements(woo, "contacts")
    assert [d.value for d in woo.entity_flows["customers"].allowed()] == ["off"]
    assert "contacts" not in sync_plan(woo, _WIDEST)
    assert not [t for t in woo.webhook_topics(_WIDEST) if t.startswith("customer.")]


# -- Store errors --

@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_error_keeps_response_message(woo, ctx):
    long_message = "Error getting remote image https://example.invalid/a.jpg. " + "x" * 400
    respx.put("https://store.example.com/wp-json/wc/v3/products/7").mock(return_value=httpx.Response(
        400, json={"code": "woocommerce_product_invalid_sku", "message": long_message}))
    items = [{"woocommerce_product_id": "7", "sku": "S-7", "name": "Seven", "files": []}]
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync",
               new=AsyncMock(return_value=items)):
        result = await woo.sync_products_out(ctx)
    error = result.errors[0]
    assert "woocommerce_product_invalid_sku" in error and "Error getting remote image" in error
    assert "400" in error
    assert len(error) < 400
    assert "ck_testkey" not in error and "cs_testsecret" not in error


@pytest.mark.asyncio
@respx.mock
async def test_list_error_keeps_response_message(woo, ctx):
    respx.get("https://store.example.com/wp-json/wc/v3/products").mock(return_value=httpx.Response(
        401, json={"code": "woocommerce_rest_cannot_view", "message": "Sorry, you cannot list resources."}))
    result = await woo.sync_products(ctx)
    assert "woocommerce_rest_cannot_view" in result.errors[0]
    assert "Sorry, you cannot list resources." in result.errors[0]


# -- First sync --

@pytest.mark.asyncio
async def test_first_sync_orders_respect_start_date(woo, ctx, mock_upsert_order):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[]))
        await woo.sync_orders(ctx, orders_from=start)
        await woo.sync_orders(ctx, since=datetime(2026, 10, 1, tzinfo=timezone.utc),
                              orders_from=start)
    for call in route.calls:
        assert call.request.url.params["after"] == start.isoformat()


# -- First sync preview --

def _store_with_catalog_and_orders():
    respx.get("https://store.example.com/wp-json/wc/v3/products").mock(return_value=httpx.Response(200, json=[
        {"id": 1, "sku": "MATCH-1", "name": "Store shirt", "regular_price": "30.00",
         "description": "Store words", "date_modified_gmt": "2026-09-02T10:00:00"},
        {"id": 2, "sku": "NEW-1", "name": "New mug", "regular_price": "8.00",
         "date_modified_gmt": "2026-09-05T10:00:00"},
        {"id": 3, "sku": "", "name": "Untitled", "date_modified_gmt": "2026-09-01T10:00:00"},
    ]))
    respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[
        {"id": 10, "number": "1010", "status": "processing", "date_created_gmt": "2026-09-03T08:00:00",
         "total": "40.00", "currency": "USD"},
        {"id": 11, "number": "1011", "status": "completed", "date_created_gmt": "2026-09-04T08:00:00",
         "total": "12.00", "currency": "USD"},
        {"id": 12, "number": "1012", "status": "pending", "date_created_gmt": "2026-09-06T08:00:00",
         "total": "5.00", "currency": "USD"},
        {"id": 13, "number": "1013", "status": "cancelled", "date_created_gmt": "2026-09-02T08:00:00",
         "total": "9.00", "currency": "USD"},
    ]))


@pytest.mark.asyncio
@respx.mock
async def test_preview_writes_nothing(woo, connector_session):
    from sqlalchemy import func, select

    from celerp.connectors.first_sync import build_preview
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection

    session = connector_session
    cid = await seed_company(session, "PreviewOnly")
    await add_item(session, cid, "item:match", "MATCH-1", name="Celerp shirt", sale_price=25.0)
    await add_item(session, cid, "item:only", "ONLY-1", name="Celerp only")
    await connect(session, cid, "woocommerce", _WIDEST)
    ctx = ConnectorContext(company_id=str(cid), access_token="ck:cs", store_handle="https://store.example.com")
    _store_with_catalog_and_orders()

    async def counts():
        return (await session.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == cid)),
                await session.scalar(select(func.max(Projection.version)).where(Projection.company_id == cid)))

    before = await counts()
    preview = await build_preview(woo, ctx, _WIDEST, datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert await counts() == before
    assert {call.request.method for call in respx.calls} == {"GET"}

    assert [(m["sku"], m["entity_id"]) for m in preview.matches] == [("MATCH-1", "item:match")]
    assert preview.matches[0]["differences"] == {
        "name": ("Celerp shirt", "Store shirt"), "description": (None, "Store words"),
        "sale_price": (25.0, 30.0)}
    assert [p["sku"] for p in preview.new] == ["NEW-1"]
    assert [p["name"] for p in preview.no_sku] == ["Untitled"]
    assert [p["sku"] for p in preview.to_publish] == ["ONLY-1"]


@pytest.mark.asyncio
@respx.mock
async def test_preview_counts_orders_and_accounting_effect(woo, connector_session):
    from celerp.connectors.first_sync import build_preview

    session = connector_session
    cid = await seed_company(session, "PreviewOrders")
    await connect(session, cid, "woocommerce", _WIDEST)
    ctx = ConnectorContext(company_id=str(cid), access_token="ck:cs", store_handle="https://store.example.com")
    _store_with_catalog_and_orders()
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)

    preview = await build_preview(woo, ctx, _WIDEST, start)

    order_call = next(c for c in respx.calls if c.request.url.path.endswith("/orders"))
    assert order_call.request.url.params["after"] == start.isoformat()
    assert preview.orders_by_status == {"processing": 1, "completed": 1, "pending": 1, "cancelled": 1}
    assert preview.posting_orders == 2
    assert preview.order_effect == t("connectors.preview_orders_post", n=2, service="WooCommerce")
    # Newest first.
    assert [o["number"] for o in preview.orders] == ["1012", "1011", "1010", "1013"]


# -- Publishing --

_OUT = {**_WIDEST, "products": "outbound"}
_STORE = "https://store.example.com/wp-json/wc/v3"


async def _publishing(session, company: str, sku: str, **item):
    cid = await seed_company(session, company)
    await add_item(session, cid, f"item:{sku.lower()}", sku, **item)
    await connect(session, cid, "woocommerce", _OUT)
    return cid, ConnectorContext(company_id=str(cid), access_token="ck:cs",
                                 store_handle="https://store.example.com")


@pytest.mark.asyncio
@respx.mock
async def test_sku_match_needs_confirmation(woo, connector_session):
    from celerp.connectors.woocommerce import LinkNeedsConfirmation
    from celerp.connectors.product_links import external_link_for_state
    from connector_test_support import item_state

    session = connector_session
    cid, ctx = await _publishing(session, "SkuConsent", "SM-1")
    respx.get(f"{_STORE}/products").mock(return_value=httpx.Response(
        200, json=[{"id": 88, "sku": "SM-1", "name": "Store mug", "type": "simple", "status": "publish"}]))
    create = respx.post(f"{_STORE}/products")

    with pytest.raises(LinkNeedsConfirmation) as asked:
        await woo.ensure_product_link(ctx, "item:sm-1")
    assert asked.value.remote_name == "Store mug"
    assert str(asked.value) == t("connectors.link_confirm", name="Store mug", service="WooCommerce")
    assert not external_link_for_state(await item_state(session, cid, "item:sm-1"), "woocommerce")

    await woo.ensure_product_link(ctx, "item:sm-1", confirm_link=True)
    assert external_link_for_state(await item_state(session, cid, "item:sm-1"), "woocommerce")["product_id"] == "88"
    assert not create.called


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_unpublish_sets_draft_keeps_link(woo, connector_session):
    import json as _json

    from celerp.connectors.product_links import external_link_for_state
    from connector_test_support import item_state

    session = connector_session
    cid, ctx = await _publishing(session, "Unpublish", "UP-1")
    respx.get(f"{_STORE}/products").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{_STORE}/products").mock(return_value=httpx.Response(
        201, json={"id": 501, "type": "simple", "status": "publish", "manage_stock": True}))
    await woo.ensure_product_link(ctx, "item:up-1")

    put = respx.put(f"{_STORE}/products/501").mock(side_effect=lambda request: httpx.Response(
        200, json={"id": 501, "type": "simple", "status": _json.loads(request.content)["status"]}))
    await woo.set_product_status(ctx, "item:up-1", "draft")
    link = external_link_for_state(await item_state(session, cid, "item:up-1"), "woocommerce")
    assert _json.loads(put.calls.last.request.content) == {"status": "draft"}
    assert link["product_id"] == "501" and link["remote_status"] == "draft" and link["sync_enabled"]

    respx.get(f"{_STORE}/products/501").mock(return_value=httpx.Response(
        200, json={"id": 501, "type": "simple", "status": "draft", "manage_stock": True}))
    await woo.ensure_product_link(ctx, "item:up-1")
    assert _json.loads(put.calls.last.request.content) == {"status": "publish"}
    link = external_link_for_state(await item_state(session, cid, "item:up-1"), "woocommerce")
    assert link["remote_status"] == "publish"


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_link_save_failure_names_created_product(woo, connector_session, monkeypatch):
    session = connector_session
    cid, ctx = await _publishing(session, "LinkSave", "LS-1", name="Blue lamp")
    respx.get(f"{_STORE}/products").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{_STORE}/products").mock(return_value=httpx.Response(
        201, json={"id": 77, "name": "Blue lamp", "type": "simple", "status": "publish"}))
    monkeypatch.setattr("celerp_inventory.services.set_external_link",
                        AsyncMock(side_effect=RuntimeError("database busy")))

    with pytest.raises(ValueError) as failed:
        await woo.ensure_product_link(ctx, "item:ls-1")
    assert str(failed.value) == t("connectors.err_link_save_failed", name="Blue lamp",
                                  service="WooCommerce", reason="database busy")
