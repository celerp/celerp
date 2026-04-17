# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Unit tests for celerp/services/{crm,docs,items}.py (connector service layer).

All DB calls are mocked. SessionLocal is imported inside function bodies,
so we patch celerp.db.SessionLocal.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_session_ctx(existing=None):
    """
    Return a context-manager-shaped mock for SessionLocal().
    execute().first() returns `existing`.
    """
    sess = MagicMock()
    execute_result = MagicMock()
    execute_result.first = MagicMock(return_value=existing)
    sess.execute = AsyncMock(return_value=execute_result)
    sess.commit = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, sess


# ── services/items.py ─────────────────────────────────────────────────────────

class TestUpsertFromConnector:
    @pytest.mark.asyncio
    async def test_creates_new_item(self):
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = "idem-001"
        item.sku = "SKU-001"
        item.name = "Blue Sapphire"
        item.sale_price = 1200.0
        item.quantity = 5

        cm, sess = _mock_session_ctx(existing=None)

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_inventory.services.emit_event", new=AsyncMock(return_value=MagicMock())):
            result = await upsert_from_connector("company-1", item)

        assert result is True

    @pytest.mark.asyncio
    async def test_skips_duplicate(self):
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = "idem-dup"
        item.sku = "SKU-DUP"
        item.name = "Duplicate"
        item.sale_price = None
        item.quantity = None

        cm, _ = _mock_session_ctx(existing=("row_id",))

        with patch("celerp.db.SessionLocal", return_value=cm):
            result = await upsert_from_connector("company-1", item)

        assert result is False

    @pytest.mark.asyncio
    async def test_raises_without_idempotency_key(self):
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = None

        with pytest.raises(ValueError, match="idempotency_key required"):
            await upsert_from_connector("company-1", item)

    @pytest.mark.asyncio
    async def test_omits_none_optional_fields(self):
        """sale_price=None and quantity=None must not appear in emitted data."""
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = "idem-sparse"
        item.sku = "SPARSE-001"
        item.name = "Sparse Item"
        item.sale_price = None
        item.quantity = None

        cm, _ = _mock_session_ctx(existing=None)
        captured = {}

        async def capture_emit(session, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_inventory.services.emit_event", new=capture_emit):
            await upsert_from_connector("company-1", item)

        assert "sale_price" not in captured.get("data", {})
        assert "quantity" not in captured.get("data", {})


# ── services/crm.py ───────────────────────────────────────────────────────────

class TestCreateCrmEntity:
    @pytest.mark.asyncio
    async def test_delegates_to_emit_event(self):
        from celerp_contacts.services import create_crm_entity

        sess = MagicMock()
        data = {"id": "contact:abc", "idempotency_key": "idem-crm-1", "name": "Alice"}

        with patch("celerp_contacts.services.emit_event", new=AsyncMock(return_value={"ok": True})) as m:
            await create_crm_entity(sess, "company-1", "contact", data)

        m.assert_awaited_once()
        call_kwargs = m.call_args.kwargs
        assert call_kwargs["event_type"] == "crm.contact.created"
        assert call_kwargs["company_id"] == "company-1"


class TestUpsertContactFromShopify:
    @pytest.mark.asyncio
    async def test_creates_new_contact(self):
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {
            "id": 12345,
            "email": "alice@example.com",
            "first_name": "Alice",
            "last_name": "Smith",
            "phone": "+66800000001",
            "addresses": [{"city": "Bangkok", "country": "TH", "phone": None}],
        }

        cm, _ = _mock_session_ctx(existing=None)

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_contacts.services.emit_event", new=AsyncMock(return_value=MagicMock())):
            result = await upsert_contact_from_shopify("company-1", customer)

        assert result is True

    @pytest.mark.asyncio
    async def test_skips_duplicate_contact(self):
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 99999, "email": "dup@example.com", "first_name": "", "last_name": ""}

        cm, _ = _mock_session_ctx(existing=("row",))

        with patch("celerp.db.SessionLocal", return_value=cm):
            result = await upsert_contact_from_shopify("company-1", customer)

        assert result is False

    @pytest.mark.asyncio
    async def test_falls_back_to_email_for_name(self):
        """When first+last are empty, name should fall back to email."""
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 7, "email": "fallback@example.com", "first_name": "", "last_name": ""}

        cm, _ = _mock_session_ctx(existing=None)
        captured = {}

        async def capture_emit(session, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_contacts.services.emit_event", new=capture_emit):
            await upsert_contact_from_shopify("company-1", customer)

        assert captured["data"]["name"] == "fallback@example.com"

    @pytest.mark.asyncio
    async def test_falls_back_to_shopify_id_when_no_email(self):
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 42, "first_name": "", "last_name": ""}

        cm, _ = _mock_session_ctx(existing=None)
        captured = {}

        async def capture_emit(session, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_contacts.services.emit_event", new=capture_emit):
            await upsert_contact_from_shopify("company-1", customer)

        assert captured["data"]["name"] == "shopify:42"


# ── services/docs.py ──────────────────────────────────────────────────────────

class TestUpsertOrderFromShopify:
    @pytest.mark.asyncio
    async def test_creates_paid_order_as_closed(self):
        from celerp_docs.doc_service import upsert_order_from_shopify

        order = {
            "id": 5001,
            "name": "#5001",
            "financial_status": "paid",
            "total_price": "350.00",
            "line_items": [{"title": "Ring", "quantity": 1, "price": "350.00"}],
        }

        cm, _ = _mock_session_ctx(existing=None)
        captured = {}

        async def capture_emit(session, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_docs.doc_service.emit_event", new=capture_emit):
            result = await upsert_order_from_shopify("company-1", order)

        assert result is True
        assert captured["data"]["status"] == "closed"
        assert captured["data"]["amount_outstanding"] == 0.0

    @pytest.mark.asyncio
    async def test_creates_unpaid_order_as_open(self):
        from celerp_docs.doc_service import upsert_order_from_shopify

        order = {
            "id": 5002,
            "name": "#5002",
            "financial_status": "pending",
            "total_price": "200.00",
            "line_items": [],
        }

        cm, _ = _mock_session_ctx(existing=None)
        captured = {}

        async def capture_emit(session, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_docs.doc_service.emit_event", new=capture_emit):
            result = await upsert_order_from_shopify("company-1", order)

        assert result is True
        assert captured["data"]["status"] == "open"
        assert captured["data"]["amount_outstanding"] == 200.0

    @pytest.mark.asyncio
    async def test_skips_duplicate_order(self):
        from celerp_docs.doc_service import upsert_order_from_shopify

        order = {"id": 9999, "name": "#9999", "financial_status": "paid", "total_price": "0", "line_items": []}

        cm, _ = _mock_session_ctx(existing=("row",))

        with patch("celerp.db.SessionLocal", return_value=cm):
            result = await upsert_order_from_shopify("company-1", order)

        assert result is False

    @pytest.mark.asyncio
    async def test_line_items_mapped_correctly(self):
        from celerp_docs.doc_service import upsert_order_from_shopify

        order = {
            "id": 5003,
            "name": "#5003",
            "financial_status": "pending",
            "total_price": "450.00",
            "line_items": [
                {"title": "Bracelet", "quantity": 2, "price": "150.00"},
                {"title": "Ring", "quantity": 1, "price": "150.00"},
            ],
        }

        cm, _ = _mock_session_ctx(existing=None)
        captured = {}

        async def capture_emit(session, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("celerp.db.SessionLocal", return_value=cm), \
             patch("celerp_docs.doc_service.emit_event", new=capture_emit):
            await upsert_order_from_shopify("company-1", order)

        items = captured["data"]["line_items"]
        assert len(items) == 2
        assert items[0]["name"] == "Bracelet"
        assert items[0]["line_total"] == 300.0
        assert items[1]["line_total"] == 150.0
