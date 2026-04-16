# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Tests for Shopee and Lazada connectors."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest.mock as mock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.connectors.base import ConnectorContext, SyncDirection, SyncEntity
from celerp.connectors.lazada import LazadaConnector, _sign as lazada_sign, _upsert_contact as lazada_upsert_contact, _upsert_order as lazada_upsert_order
from celerp.connectors.shopee import ShopeeConnector, _sign as shopee_sign, _upsert_contact as shopee_upsert_contact, _upsert_order as shopee_upsert_order
from celerp.connectors.registry import all_connectors, get as registry_get


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _shopee_ctx(**kwargs) -> ConnectorContext:
    defaults = dict(
        company_id="co-1",
        access_token="tok-123",
        store_handle="99887766",
        extra={"partner_id": 12345, "partner_key": "secret-partner-key"},
    )
    defaults.update(kwargs)
    return ConnectorContext(**defaults)


def _lazada_ctx(**kwargs) -> ConnectorContext:
    defaults = dict(
        company_id="co-2",
        access_token="laz-tok-abc",
        store_handle="sg",
        extra={"app_key": "10000001", "app_secret": "laz-secret"},
    )
    defaults.update(kwargs)
    return ConnectorContext(**defaults)


# ── Registry ─────────────────────────────────────────────────────────────────

def test_registry_has_shopee():
    c = registry_get("shopee")
    assert c.name == "shopee"


def test_registry_has_lazada():
    c = registry_get("lazada")
    assert c.name == "lazada"


def test_all_connectors_includes_both():
    names = {c.name for c in all_connectors()}
    assert "shopee" in names
    assert "lazada" in names


# ── Shopee: signing ───────────────────────────────────────────────────────────

def test_shopee_sign_produces_hmac():
    partner_id, path, ts = 12345, "/product/get_item_list", 1700000000
    access_token, shop_id, key = "tok", 99887766, "secret"
    base = f"{partner_id}{path}{ts}{access_token}{shop_id}"
    expected = hmac.new(key.encode(), base.encode(), hashlib.sha256).hexdigest()
    assert shopee_sign(partner_id, path, ts, access_token, shop_id, key) == expected


def test_shopee_connector_metadata():
    c = ShopeeConnector()
    assert c.direction == SyncDirection.INBOUND
    assert SyncEntity.PRODUCTS in c.supported_entities
    assert SyncEntity.ORDERS in c.supported_entities
    assert SyncEntity.CONTACTS in c.supported_entities


# ── Shopee: validation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shopee_sync_products_missing_ctx_fails():
    ctx = ConnectorContext(company_id="co", access_token="tok", store_handle=None)
    result = await ShopeeConnector().sync_products(ctx)
    assert result.errors
    assert "store_handle" in result.errors[0]


@pytest.mark.asyncio
async def test_shopee_sync_products_missing_partner_key_fails():
    ctx = ConnectorContext(company_id="co", access_token="tok",
                           store_handle="123", extra={})
    result = await ShopeeConnector().sync_products(ctx)
    assert result.errors
    assert "partner_id" in result.errors[0]


# ── Shopee: sync_products (mocked HTTP) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_shopee_sync_products_empty():
    ctx = _shopee_ctx()
    connector = ShopeeConnector()
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "response": {"item": [], "has_next_page": False}
        }
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await connector.sync_products(ctx)
    assert result.created == 0
    assert result.errors is None


@pytest.mark.asyncio
async def test_shopee_sync_products_creates_items():
    ctx = _shopee_ctx()
    connector = ShopeeConnector()

    list_resp = MagicMock()
    list_resp.raise_for_status = MagicMock()
    list_resp.json.return_value = {
        "response": {
            "item": [{"item_id": 111}, {"item_id": 222}],
            "has_next_page": False,
        }
    }
    info_resp = MagicMock()
    info_resp.raise_for_status = MagicMock()
    info_resp.json.return_value = {
        "response": {
            "item_list": [
                {
                    "item_id": 111,
                    "item_sku": "SKU-A",
                    "item_name": "Widget A",
                    "item_status": "NORMAL",
                    "price_info": [{"current_price": "9.99"}],
                    "stock_info_v2": {"summary_info": {"total_available_stock": 50}},
                },
                {
                    "item_id": 222,
                    "item_sku": "SKU-B",
                    "item_name": "Widget B",
                    "item_status": "NORMAL",
                    "price_info": [],
                    "stock_info_v2": {},
                },
            ]
        }
    }

    call_count = 0

    async def _get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "get_item_list" in url:
            return list_resp
        return info_resp

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = _get

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("celerp_inventory.services.upsert_from_connector", new_callable=AsyncMock,
               return_value=True) as mock_upsert:
        result = await connector.sync_products(ctx)

    assert result.created == 2
    assert result.errors is None
    assert mock_upsert.call_count == 2


@pytest.mark.asyncio
async def test_shopee_sync_products_skips_non_normal():
    ctx = _shopee_ctx()
    connector = ShopeeConnector()

    list_resp = MagicMock()
    list_resp.raise_for_status = MagicMock()
    list_resp.json.return_value = {
        "response": {"item": [{"item_id": 333}], "has_next_page": False}
    }
    info_resp = MagicMock()
    info_resp.raise_for_status = MagicMock()
    info_resp.json.return_value = {
        "response": {
            "item_list": [
                {"item_id": 333, "item_sku": "BANNED", "item_status": "BANNED"}
            ]
        }
    }

    async def _get(url, **kwargs):
        if "get_item_list" in url:
            return list_resp
        return info_resp

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = _get

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await connector.sync_products(ctx)

    assert result.created == 0
    assert result.skipped == 1


# ── Shopee: _upsert_order / _upsert_contact ────────────────────────────────────

@pytest.mark.asyncio
async def test_shopee_upsert_order_idempotent():
    order = {
        "order_sn": "ORD-001",
        "order_status": "COMPLETED",
        "total_amount": 150.0,
        "item_list": [{"item_name": "X", "model_quantity_purchased": 1,
                       "model_discounted_price": 150.0}],
    }
    with patch("celerp.db.SessionLocal") as mock_sl:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = ("existing-id",)
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        result = await shopee_upsert_order("co-1", order)
    assert result is False


@pytest.mark.asyncio
async def test_shopee_upsert_order_creates():
    order = {
        "order_sn": "ORD-NEW",
        "order_status": "READY_TO_SHIP",
        "total_amount": 99.0,
        "item_list": [{"item_name": "Y", "model_quantity_purchased": 2,
                       "model_discounted_price": 49.5}],
    }
    with patch("celerp.db.SessionLocal") as mock_sl, \
         patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        result = await shopee_upsert_order("co-1", order)
    assert result is True
    mock_emit.assert_called_once()
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["event_type"] == "doc.created"
    assert call_kwargs["source"] == "connector"


@pytest.mark.asyncio
async def test_shopee_upsert_contact_no_buyer_id():
    result = await shopee_upsert_contact("co-1", {})
    assert result is False


@pytest.mark.asyncio
async def test_shopee_upsert_contact_creates():
    order = {
        "buyer_user_id": 555,
        "buyer_username": "testbuyer",
        "recipient_address": {"phone": "+66812345678", "city": "Bangkok"},
    }
    with patch("celerp.db.SessionLocal") as mock_sl, \
         patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        result = await shopee_upsert_contact("co-1", order)
    assert result is True
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["event_type"] == "crm.contact.created"
    data = call_kwargs["data"]
    assert data["name"] == "testbuyer"


# ── Lazada: signing ───────────────────────────────────────────────────────────

def test_lazada_sign_is_uppercase_hmac():
    app_secret = "laz-secret"
    path = "/products/get"
    params = {"app_key": "10000001", "timestamp": 1700000000}
    sorted_str = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    message = path + sorted_str
    expected = hmac.new(app_secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()
    assert lazada_sign(app_secret, path, params) == expected


def test_lazada_connector_metadata():
    c = LazadaConnector()
    assert c.direction == SyncDirection.INBOUND
    assert SyncEntity.PRODUCTS in c.supported_entities


# ── Lazada: validation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lazada_sync_products_missing_keys_fails():
    ctx = ConnectorContext(company_id="co", access_token="tok",
                           store_handle="sg", extra={})
    result = await LazadaConnector().sync_products(ctx)
    assert result.errors
    assert "app_key" in result.errors[0]


# ── Lazada: sync_products (mocked HTTP) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_lazada_sync_products_empty():
    ctx = _lazada_ctx()
    connector = LazadaConnector()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"data": {"products": [], "total_products": 0}}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await connector.sync_products(ctx)

    assert result.created == 0
    assert result.errors is None


@pytest.mark.asyncio
async def test_lazada_sync_products_creates_items():
    ctx = _lazada_ctx()
    connector = LazadaConnector()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "data": {
            "products": [
                {
                    "item_id": "LAZ-1",
                    "attributes": {"name": "Lazada Widget"},
                    "skus": [{"ShopSku": "LW-001", "SkuId": "5001", "price": "25.00",
                               "quantity": 10}],
                }
            ],
            "total_products": 1,
        }
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("celerp_inventory.services.upsert_from_connector", new_callable=AsyncMock,
               return_value=True) as mock_upsert:
        result = await connector.sync_products(ctx)

    assert result.created == 1
    assert result.errors is None


# ── Lazada: _upsert_order / _upsert_contact ────────────────────────────────────

@pytest.mark.asyncio
async def test_lazada_upsert_order_idempotent():
    order = {"order_id": 9999, "order_number": "9999", "price": "50.00",
              "statuses": ["delivered"], "_items": []}
    with patch("celerp.db.SessionLocal") as mock_sl:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = ("existing",)
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        result = await lazada_upsert_order("co-2", order)
    assert result is False


@pytest.mark.asyncio
async def test_lazada_upsert_order_creates():
    order = {
        "order_id": 8888,
        "order_number": "8888",
        "price": "120.00",
        "statuses": ["pending"],
        "_items": [{"name": "Gadget", "shop_sku": "GAD-1", "paid_price": "120.00"}],
    }
    with patch("celerp.db.SessionLocal") as mock_sl, \
         patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        result = await lazada_upsert_order("co-2", order)
    assert result is True
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["event_type"] == "doc.created"
    assert call_kwargs["data"]["status"] == "open"


@pytest.mark.asyncio
async def test_lazada_upsert_order_delivered_is_closed():
    order = {
        "order_id": 7777,
        "order_number": "7777",
        "price": "200.00",
        "statuses": ["delivered"],
        "_items": [],
    }
    with patch("celerp.db.SessionLocal") as mock_sl, \
         patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        await lazada_upsert_order("co-2", order)
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["data"]["status"] == "closed"
    assert call_kwargs["data"]["amount_outstanding"] == 0.0


@pytest.mark.asyncio
async def test_lazada_upsert_contact_no_name_or_phone():
    result = await lazada_upsert_contact("co-2", {"order_id": 1, "address_billing": {}})
    assert result is False


@pytest.mark.asyncio
async def test_lazada_upsert_contact_creates():
    order = {
        "order_id": 6666,
        "address_billing": {
            "first_name": "Somchai",
            "last_name": "Rak",
            "phone": "+66812222222",
            "city": "Chiang Mai",
        },
    }
    with patch("celerp.db.SessionLocal") as mock_sl, \
         patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_execute = MagicMock()
        mock_execute.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_execute)
        mock_sl.return_value = mock_session

        result = await lazada_upsert_contact("co-2", order)
    assert result is True
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["event_type"] == "crm.contact.created"
    assert call_kwargs["data"]["name"] == "Somchai Rak"
