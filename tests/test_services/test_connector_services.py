# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Unit tests for the connector service layer (items / contacts / docs upserts).

These mock the DB: `connector_upsert` (the shared engine helper) is patched, so
each test asserts the payload the service builds and how it maps the upsert
outcome. The real DB behaviour of `connector_upsert` is covered by
tests/test_services/test_connector_upsert_integration.py.

The service functions do `from celerp.events.engine import connector_upsert`
inside the body, so we patch it at its source module.
"""
from __future__ import annotations

import contextlib
import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_session_ctx():
    """A context-manager-shaped mock for SessionLocal() with an async commit."""
    sess = MagicMock()
    sess.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@contextlib.contextmanager
def _patch_upsert(outcome="created"):
    """Patch SessionLocal + connector_upsert; yields the captured upsert kwargs."""
    captured: dict = {}

    async def _fake(session, **kwargs):
        captured.update(kwargs)
        return outcome

    with patch("celerp.db.SessionLocal", return_value=_mock_session_ctx()), \
         patch("celerp.events.engine.connector_upsert", new=_fake):
        yield captured


# ── items ─────────────────────────────────────────────────────────────────────

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

        with _patch_upsert("created") as captured:
            result = await upsert_from_connector("company-1", item)

        assert result == "created"
        assert captured["idem_key"] == "idem-001"
        assert captured["entity_type"] == "item"
        assert captured["data"]["sku"] == "SKU-001"
        assert captured["data"]["sale_price"] == 1200.0

    @pytest.mark.asyncio
    async def test_skips_duplicate(self):
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = "idem-dup"
        item.sku = "SKU-DUP"
        item.name = "Duplicate"
        item.sale_price = None
        item.quantity = None

        with _patch_upsert("noop"):
            result = await upsert_from_connector("company-1", item)

        assert result == "noop"

    @pytest.mark.asyncio
    async def test_updates_existing(self):
        """A changed re-import returns "updated" (drives result.updated, not created)."""
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = "idem-upd"
        item.sku = "SKU-UPD"
        item.name = "Changed"
        item.sale_price = 9.0
        item.quantity = 1

        with _patch_upsert("updated"):
            result = await upsert_from_connector("company-1", item)

        assert result == "updated"

    @pytest.mark.asyncio
    async def test_raises_without_idempotency_key(self):
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = None

        with pytest.raises(ValueError, match="idempotency_key required"):
            await upsert_from_connector("company-1", item)

    @pytest.mark.asyncio
    async def test_omits_none_optional_fields(self):
        """sale_price=None and quantity=None must not appear in the upsert payload."""
        from celerp_inventory.services import upsert_from_connector

        item = MagicMock()
        item.idempotency_key = "idem-sparse"
        item.sku = "SPARSE-001"
        item.name = "Sparse Item"
        item.sale_price = None
        item.quantity = None

        with _patch_upsert("created") as captured:
            await upsert_from_connector("company-1", item)

        assert "sale_price" not in captured["data"]
        assert "quantity" not in captured["data"]


# ── contacts: non-connector create path (still emit_event) ─────────────────────

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

        with _patch_upsert("created") as captured:
            result = await upsert_contact_from_shopify("company-1", customer)

        assert result == "created"
        assert captured["idem_key"] == "shopify:customer:12345"
        assert captured["data"]["name"] == "Alice Smith"

    @pytest.mark.asyncio
    async def test_skips_duplicate_contact(self):
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 99999, "email": "dup@example.com", "first_name": "", "last_name": ""}

        with _patch_upsert("noop"):
            result = await upsert_contact_from_shopify("company-1", customer)

        assert result == "noop"

    @pytest.mark.asyncio
    async def test_upsert_is_company_scoped(self):
        """The service passes the caller's company_id straight through to the upsert
        (the ledger dedup is per-company; verified end-to-end in the integration test)."""
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 12345, "email": "x@example.com"}

        with _patch_upsert("created") as captured:
            result = await upsert_contact_from_shopify("company-A", customer)

        assert result == "created"
        assert captured["company_id"] == "company-A"
        assert captured["idem_key"] == "shopify:customer:12345"

    @pytest.mark.asyncio
    async def test_falls_back_to_email_for_name(self):
        """When first+last are empty, name should fall back to email."""
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 7, "email": "fallback@example.com", "first_name": "", "last_name": ""}

        with _patch_upsert("created") as captured:
            await upsert_contact_from_shopify("company-1", customer)

        assert captured["data"]["name"] == "fallback@example.com"

    @pytest.mark.asyncio
    async def test_falls_back_to_shopify_id_when_no_email(self):
        from celerp_contacts.services import upsert_contact_from_shopify

        customer = {"id": 42, "first_name": "", "last_name": ""}

        with _patch_upsert("created") as captured:
            await upsert_contact_from_shopify("company-1", customer)

        assert captured["data"]["name"] == "shopify:42"


# ── docs ──────────────────────────────────────────────────────────────────────

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

        with _patch_upsert("created") as captured:
            result = await upsert_order_from_shopify("company-1", order)

        assert result == "created"
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

        with _patch_upsert("created") as captured:
            result = await upsert_order_from_shopify("company-1", order)

        assert result == "created"
        assert captured["data"]["status"] == "open"
        assert captured["data"]["amount_outstanding"] == 200.0

    @pytest.mark.asyncio
    async def test_skips_duplicate_order(self):
        from celerp_docs.doc_service import upsert_order_from_shopify

        order = {"id": 9999, "name": "#9999", "financial_status": "paid", "total_price": "0", "line_items": []}

        with _patch_upsert("noop"):
            result = await upsert_order_from_shopify("company-1", order)

        assert result == "noop"

    @pytest.mark.asyncio
    async def test_blank_total_does_not_raise(self):
        """A blank/absent total must coerce to 0.0, not raise (else the record errors
        every sync and pins the watermark)."""
        from celerp_docs.doc_service import upsert_order_from_shopify

        order = {"id": 9001, "name": "#9001", "financial_status": "pending",
                 "total_price": "", "line_items": []}

        with _patch_upsert("created") as captured:
            result = await upsert_order_from_shopify("company-1", order)

        assert result == "created"
        assert captured["data"]["total"] == 0.0

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

        with _patch_upsert("created") as captured:
            await upsert_order_from_shopify("company-1", order)

        items = captured["data"]["line_items"]
        assert len(items) == 2
        assert items[0]["name"] == "Bracelet"
        assert items[0]["line_total"] == 300.0
        assert items[1]["line_total"] == 150.0
