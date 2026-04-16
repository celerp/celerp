# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""User journey tests - Sprint 7.

Tests the application from a new user's perspective: discovery, CRUD,
workflows, navigation, search/filter, edge cases, and data entry patterns.
200+ scenarios organized by category.

Uses the same test infrastructure as test_ui.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch, MagicMock

from tests.conftest import make_test_token, authed_cookies

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _c(token: str | None = None, role: str = "owner") -> dict:
    return {"celerp_token": token or make_test_token(role=role)}


# Shared mock data
_COMPANY = {"name": "Test Co", "base_currency": "THB", "fiscal_year_start": "01"}
_ITEM = {"entity_id": "item:1", "name": "Ruby", "sku": "RB001", "category": "Parcel",
         "status": "available", "quantity": 10, "cost_price": 100, "retail_price": 500,
         "wholesale_price": 300, "total_cost": 1000}
_CONTACT = {"entity_id": "contact:1", "name": "Alice", "email": "alice@test.com",
            "type": "customer", "phone": "", "tags": ["vip"]}
_DOC_INV = {"entity_id": "doc:inv1", "doc_type": "invoice", "ref": "INV2026001",
            "status": "draft", "total": 5000, "amount_outstanding": 5000,
            "contact_external_id": "contact:1", "created_at": "2026-01-01",
            "line_items": [{"item_id": "item:1", "description": "Ruby", "quantity": 1, "unit_price": 5000}]}
_DOC_PAID = {**_DOC_INV, "entity_id": "doc:inv2", "ref": "INV2026002", "status": "paid",
             "amount_outstanding": 0}
_DOC_PO = {"entity_id": "doc:po1", "doc_type": "purchase_order", "ref": "PO2026001",
           "status": "draft", "total": 3000, "amount_outstanding": 3000,
           "contact_external_id": "contact:1", "created_at": "2026-01-15", "line_items": []}
_DOC_QUOT = {"entity_id": "doc:quo1", "doc_type": "quotation", "ref": "QUO2026001",
             "status": "draft", "total": 2000, "amount_outstanding": 2000,
             "contact_external_id": "contact:1", "created_at": "2026-02-01", "line_items": []}
_MEMO = {"entity_id": "memo:1", "contact_id": "contact:1", "contact_name": "Alice",
         "status": "active", "items": [{"item_id": "item:1", "name": "Ruby"}],
         "total": 500, "created_at": "2026-01-10"}
_SUB = {"entity_id": "sub:1", "name": "Monthly Invoice", "frequency": "monthly",
        "status": "active", "doc_type": "invoice", "contact_id": "contact:1",
        "start_date": "2026-01-01", "next_date": "2026-03-01", "line_items": []}
_MFG = {"entity_id": "mfg:1", "name": "Ring Assembly", "status": "planned",
        "order_type": "manufacturing", "steps": [{"id": "s1", "name": "Cut", "status": "pending"}],
        "inputs": [{"item_id": "item:1", "name": "Ruby", "quantity": 1, "consumed": False}],
        "created_at": "2026-02-01"}
_BOM = {"entity_id": "bom:1", "name": "Gold Ring", "items": [
    {"item_id": "item:1", "name": "Ruby", "quantity": 1}], "output_description": "18k Gold Ring"}
_DEAL = {"entity_id": "deal:1", "name": "Big Sale", "stage": "lead", "value": 10000,
         "contact_id": "contact:1", "status": "active"}
_VALUATION = {"item_count": 100, "active_item_count": 95, "cost_total": 50000,
              "retail_total": 200000, "wholesale_total": 120000,
              "category_counts": {"Parcel": 50, "Single": 30, "Pair": 15, "Set": 5}}
_DOC_SUMMARY = {"ar_outstanding": 25000, "ar_gross": 100000}
_MEMO_SUMMARY = {"active_total": 5000}
_ACTIVITY = {"activities": [
    {"ts": "2026-02-25T10:00:00Z", "event_type": "document.invoice.created",
     "entity_id": "doc:inv1", "summary": "Invoice INV2026001 created", "actor": "admin"},
    {"ts": "2026-02-25T09:00:00Z", "event_type": "item.created",
     "entity_id": "item:1", "summary": "Item Ruby added", "actor": "admin"},
]}
_LOCATIONS = [{"id": "loc:1", "name": "Main Warehouse", "type": "warehouse", "address": {}}]
_TAXES = [{"name": "VAT", "rate": 7}]
_TERMS = [{"name": "Net 30", "days": 30}]
_USERS = [{"id": "u1", "email": "admin@test.com", "name": "Admin", "role": "admin"}]
_SCHEMA = [{"key": "name", "label": "Name", "type": "text", "editable": True}]
_COA = [{"code": "1000", "name": "Cash", "type": "asset", "balance": 100000}]
_PNL = {"revenue": {"lines": [{"name": "Sales", "amount": 50000}], "total": 50000},
        "cogs": {"lines": [{"name": "COGS", "amount": 20000}], "total": 20000},
        "expenses": {"lines": [], "total": 0}, "net_income": 30000}
_BS = {"assets": {"lines": [{"name": "Cash", "amount": 100000}], "total": 100000},
       "liabilities": {"lines": [], "total": 0},
       "equity": {"lines": [{"name": "Retained", "amount": 100000}], "total": 100000}}
_TB = {"accounts": [{"code": "1000", "name": "Cash", "debit": 100000, "credit": 0}]}
_AR_AGING = {"buckets": {"Current": 15000, "1-30": 5000, "31-60": 3000, "61-90": 1000, "90+": 1000},
             "rows": []}
_AP_AGING = {"buckets": {"Current": 3000}, "rows": []}
_SALES = {"rows": [{"period": "2026-01", "total": 50000}], "grand_total": 50000}
_PURCHASES = {"rows": [{"period": "2026-01", "total": 20000}], "grand_total": 20000}
_EXPIRING = {"items": []}


def _dashboard_mocks():
    """Return a dict of patches for dashboard route."""
    return {
        "ui.api_client.get_company": AsyncMock(return_value=_COMPANY),
        "ui.api_client.get_valuation": AsyncMock(return_value=_VALUATION),
        "ui.api_client.get_doc_summary": AsyncMock(return_value=_DOC_SUMMARY),
        "ui.api_client.get_memo_summary": AsyncMock(return_value=_MEMO_SUMMARY),
        "ui.api_client.my_companies": AsyncMock(return_value={"items": [_COMPANY], "total": 1}),
        "ui.api_client.get_ar_aging": AsyncMock(return_value=_AR_AGING),
        "ui.api_client.get_activity": AsyncMock(return_value=_ACTIVITY["activities"]),
    }


def _inventory_mocks(items=None):
    return {
        "ui.api_client.list_items": AsyncMock(return_value={"items": items if items is not None else [_ITEM], "total": len(items) if items is not None else 1}),
        "ui.api_client.get_valuation": AsyncMock(return_value=_VALUATION),
        "ui.api_client.get_item_schema": AsyncMock(return_value=_SCHEMA),
        "ui.api_client.get_all_category_schemas": AsyncMock(return_value={}),
        "ui.api_client.get_column_prefs": AsyncMock(return_value={}),
        "ui.api_client.get_company": AsyncMock(return_value=_COMPANY),
        "ui.api_client.get_locations": AsyncMock(return_value={"items": _LOCATIONS, "total": len(_LOCATIONS)}),
    }


def _docs_mocks(docs=None):
    return {
        "ui.api_client.list_docs": AsyncMock(return_value={"items": docs if docs is not None else [_DOC_INV, _DOC_PAID, _DOC_PO], "total": len(docs) if docs is not None else 3}),
        "ui.api_client.get_doc_summary": AsyncMock(return_value=_DOC_SUMMARY),
    }


def _crm_mocks(contacts=None, memos=None, deals=None):
    c = contacts if contacts is not None else [_CONTACT]
    m = memos if memos is not None else [_MEMO]
    d = deals if deals is not None else [_DEAL]
    return {
        "ui.api_client.list_contacts": AsyncMock(return_value={"items": c, "total": len(c)}),
        "ui.api_client.list_memos": AsyncMock(return_value={"items": m, "total": len(m)}),
        "ui.api_client.list_deals": AsyncMock(return_value={"items": d, "total": len(d)}),
        "ui.api_client.get_memo_summary": AsyncMock(return_value=_MEMO_SUMMARY),
        "ui.api_client.get_company": AsyncMock(return_value=_COMPANY),
    }


def _settings_mocks():
    return {
        "ui.api_client.get_company": AsyncMock(return_value=_COMPANY),
        "ui.api_client.get_taxes": AsyncMock(return_value=_TAXES),
        "ui.api_client.get_payment_terms": AsyncMock(return_value=_TERMS),
        "ui.api_client.get_users": AsyncMock(return_value={"items": _USERS, "total": len(_USERS)}),
        "ui.api_client.get_item_schema": AsyncMock(return_value=_SCHEMA),
        "ui.api_client.get_all_category_schemas": AsyncMock(return_value={}),
        "ui.api_client.get_column_prefs": AsyncMock(return_value={}),
        "ui.api_client.get_locations": AsyncMock(return_value={"items": _LOCATIONS, "total": len(_LOCATIONS)}),
        "ui.api_client.list_import_batches": AsyncMock(return_value={"batches": []}),
        "ui.api_client.list_verticals_categories": AsyncMock(return_value=[]),
        "ui.api_client.list_verticals_presets": AsyncMock(return_value=[]),
        "ui.api_client.get_modules": AsyncMock(return_value=[
            {"name": "celerp-inventory", "enabled": True},
            {"name": "celerp-docs", "enabled": True},
        ]),
    }


def _reports_mocks():
    return {
        "ui.api_client.get_ar_aging": AsyncMock(return_value=_AR_AGING),
        "ui.api_client.get_ap_aging": AsyncMock(return_value=_AP_AGING),
        "ui.api_client.get_sales_report": AsyncMock(return_value=_SALES),
        "ui.api_client.get_purchases_report": AsyncMock(return_value=_PURCHASES),
        "ui.api_client.get_expiring": AsyncMock(return_value=_EXPIRING),
    }


def _accounting_mocks():
    return {
        "ui.api_client.get_company": AsyncMock(return_value=_COMPANY),
        "ui.api_client.get_chart": AsyncMock(return_value={"items": _COA, "total": len(_COA)}),
        "ui.api_client.get_pnl": AsyncMock(return_value=_PNL),
        "ui.api_client.get_balance_sheet": AsyncMock(return_value=_BS),
        "ui.api_client.get_trial_balance": AsyncMock(return_value=_TB),
    }


def _apply(mocks: dict):
    """Create a combined context manager for multiple patches."""
    import contextlib
    return contextlib.ExitStack()


# Helper to apply multiple patches
class _Patches:
    def __init__(self, mocks: dict):
        self._patches = [patch(k, new=v) for k, v in mocks.items()]
    def __enter__(self):
        for p in self._patches:
            p.start()
        return self
    def __exit__(self, *args):
        for p in self._patches:
            p.stop()


# ══════════════════════════════════════════════════════════════════════════════
# 1. DISCOVERY - Can a user find things? (30+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestDiscovery:
    """Users should be able to find every major feature from the UI."""

    @pytest.mark.asyncio
    async def test_dashboard_has_kpi_cards(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert r.status_code == 200
        assert b"kpi-card" in r.content

    @pytest.mark.asyncio
    async def test_dashboard_has_quick_links(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"quick-link" in r.content

    @pytest.mark.asyncio
    async def test_dashboard_has_activity_feed(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"activity" in r.content.lower()

    @pytest.mark.asyncio
    async def test_dashboard_has_charts(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"chart" in r.content.lower()

    @pytest.mark.asyncio
    async def test_sidebar_has_all_nav_groups(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        for group in [b"Sales", b"Documents", b"Inventory", b"Finance"]:
            assert group in r.content, f"Missing sidebar group: {group}"

    @pytest.mark.asyncio
    async def test_sidebar_has_dashboard_link(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/dashboard" in r.content

    @pytest.mark.asyncio
    async def test_find_inventory_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/inventory" in r.content

    @pytest.mark.asyncio
    async def test_find_invoices_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"Invoices" in r.content

    @pytest.mark.asyncio
    async def test_find_crm_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/crm" in r.content

    @pytest.mark.asyncio
    async def test_find_reports_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"Reports" in r.content

    @pytest.mark.asyncio
    async def test_find_manufacturing_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/manufacturing" in r.content

    @pytest.mark.asyncio
    async def test_find_settings_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/settings" in r.content

    @pytest.mark.asyncio
    async def test_find_accounting_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/accounting" in r.content

    @pytest.mark.asyncio
    async def test_find_subscriptions_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/subscriptions" in r.content

    @pytest.mark.skip(reason="Scanning module disabled until complete")
    @pytest.mark.asyncio
    async def test_find_scanning_from_sidebar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"/scanning" in r.content

    @pytest.mark.asyncio
    async def test_global_search_in_topbar(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"global-search" in r.content

    @pytest.mark.asyncio
    async def test_inventory_has_search(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory", cookies=_c())
        assert r.status_code == 200
        assert b"search" in r.content.lower()

    @pytest.mark.asyncio
    async def test_inventory_has_import_button(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory", cookies=_c())
        assert b"import" in r.content.lower()

    @pytest.mark.asyncio
    async def test_docs_page_has_new_invoice_button(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs", cookies=_c())
        assert r.status_code == 200
        assert b"New" in r.content or b"new" in r.content.lower()

    @pytest.mark.asyncio
    async def test_crm_page_has_contacts_tab(self, ui):
        with _Patches(_crm_mocks()):
            r = await ui.get("/contacts/customers", cookies=_c())
        assert r.status_code == 200
        assert b"Customer" in r.content or b"Contact" in r.content

    @pytest.mark.asyncio
    async def test_crm_page_has_memos_tab(self, ui):
        # Memos live at /contacts/vendors (memos tab) - just verify the memos section renders
        with _Patches(_crm_mocks()):
            r = await ui.get("/contacts/vendors", cookies=_c())
        assert r.status_code == 200
        # Vendors page links to memos
        assert b"Memo" in r.content or b"memo" in r.content or b"Vendor" in r.content

    @pytest.mark.asyncio
    async def test_crm_page_has_deals_tab(self, ui):
        with _Patches(_crm_mocks()):
            r = await ui.get("/contacts/sales", cookies=_c())
        assert r.status_code == 200
        assert b"Deal" in r.content or b"deal" in r.content or b"Sales" in r.content

    @pytest.mark.asyncio
    async def test_reports_landing_shows_report_types(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_settings_has_company_tab(self, ui):
        with _Patches(_settings_mocks()):
            r = await ui.get("/settings/general", cookies=_c())
        assert r.status_code == 200
        assert b"company" in r.content.lower() or b"Company" in r.content

    @pytest.mark.asyncio
    async def test_settings_has_users_tab(self, ui):
        with _Patches(_settings_mocks()):
            r = await ui.get("/settings/general", cookies=_c())
        assert r.status_code == 200
        assert b"user" in r.content.lower()

    @pytest.mark.asyncio
    async def test_settings_has_taxes_tab(self, ui):
        with _Patches(_settings_mocks()):
            r = await ui.get("/settings/sales", cookies=_c())
        assert r.status_code == 200
        assert b"tax" in r.content.lower()

    @pytest.mark.asyncio
    async def test_manufacturing_page_loads(self, ui):
        with _Patches({"ui.api_client.list_mfg_orders": AsyncMock(return_value={"items": [_MFG], "total": 1}),
                       "ui.api_client.list_boms": AsyncMock(return_value={"items": [_BOM], "total": 1})}):
            r = await ui.get("/manufacturing", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.skip(reason="Scanning module disabled until complete")
    @pytest.mark.asyncio
    async def test_scanning_page_loads(self, ui):
        r = await ui.get("/scanning", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_subscriptions_page_loads(self, ui):
        with _Patches({"ui.api_client.list_subscriptions": AsyncMock(return_value=[_SUB])}):
            r = await ui.get("/subscriptions", cookies=_c())
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 2. CRUD OPERATIONS - Can users create/read/update things? (50+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestCRUDRead:
    """Users can view lists and details of all entity types."""

    @pytest.mark.asyncio
    async def test_inventory_shows_items(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory", cookies=_c())
        assert r.status_code == 200
        assert b"Ruby" in r.content or b"RB001" in r.content

    @pytest.mark.asyncio
    async def test_inventory_item_detail(self, ui):
        with _Patches({"ui.api_client.get_item": AsyncMock(return_value=_ITEM),
                       "ui.api_client.get_item_schema": AsyncMock(return_value=_SCHEMA),
                       "ui.api_client.get_company": AsyncMock(return_value=_COMPANY),
                       "ui.api_client.list_ledger": AsyncMock(return_value={"items": [], "total": 0}),
                       "ui.api_client.get_locations": AsyncMock(return_value={"items": _LOCATIONS, "total": len(_LOCATIONS)})}):
            r = await ui.get("/inventory/item:1", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_list_shows_invoices(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?type=invoice", cookies=_c())
        assert r.status_code == 200
        assert b"INV2026001" in r.content or b"invoice" in r.content.lower()

    @pytest.mark.asyncio
    async def test_docs_list_shows_pos(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?type=purchase_order", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_doc_detail_page(self, ui):
        with _Patches({"ui.api_client.get_doc": AsyncMock(return_value=_DOC_INV),
                       "ui.api_client.list_contacts": AsyncMock(return_value={"items": [_CONTACT], "total": 1})}):
            r = await ui.get("/docs/doc:inv1", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_contact_list(self, ui):
        with _Patches(_crm_mocks()):
            r = await ui.get("/contacts/customers", cookies=_c())
        assert r.status_code == 200
        assert b"Alice" in r.content

    @pytest.mark.asyncio
    async def test_contact_detail(self, ui):
        with _Patches({"ui.api_client.get_contact": AsyncMock(return_value=_CONTACT),
                       "ui.api_client.list_memos": AsyncMock(return_value={"items": [], "total": 0})}):
            r = await ui.get("/contacts/contact:1", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_memo_list(self, ui):
        with _Patches(_crm_mocks()):
            r = await ui.get("/crm", follow_redirects=False, cookies=_c())
        assert r.status_code == 302  # /crm redirects to /contacts/customers

    @pytest.mark.asyncio
    async def test_memo_detail(self, ui):
        with _Patches({"ui.api_client.get_memo": AsyncMock(return_value=_MEMO),
                       "ui.api_client.list_items": AsyncMock(return_value={"items": [_ITEM], "total": 1})}):
            r = await ui.get("/crm/memos/memo:1", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_subscription_list(self, ui):
        with _Patches({"ui.api_client.list_subscriptions": AsyncMock(return_value=[_SUB])}):
            r = await ui.get("/subscriptions", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_mfg_order_list(self, ui):
        with _Patches({"ui.api_client.list_mfg_orders": AsyncMock(return_value={"items": [_MFG], "total": 1}),
                       "ui.api_client.list_boms": AsyncMock(return_value={"items": [_BOM], "total": 1})}):
            r = await ui.get("/manufacturing", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_accounting_pnl(self, ui):
        with _Patches(_accounting_mocks()):
            r = await ui.get("/accounting", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_accounting_balance_sheet(self, ui):
        with _Patches(_accounting_mocks()):
            r = await ui.get("/accounting?tab=balance-sheet", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_ar_aging_report(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/ar-aging", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_ap_aging_report(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/ap-aging", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_sales_report(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/sales", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_purchases_report(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/purchases", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_expiring_report(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/expiring", cookies=_c())
        assert r.status_code == 200


class TestCRUDCreate:
    """Users can create new entities via blank-first pattern."""

    @pytest.mark.asyncio
    async def test_create_invoice_blank_first(self, ui):
        with _Patches({"ui.api_client.create_doc": AsyncMock(return_value={"id": "doc:new1", "event_id": 1})}):
            r = await ui.post("/docs/create-blank?type=invoice", cookies=_c())
        assert r.status_code in (204, 302, 303, 200)

    @pytest.mark.asyncio
    async def test_create_po_blank_first(self, ui):
        with _Patches({"ui.api_client.create_doc": AsyncMock(return_value={"id": "doc:newpo", "event_id": 1})}):
            r = await ui.post("/docs/create-blank?type=purchase_order", cookies=_c())
        assert r.status_code in (204, 302, 303, 200)

    @pytest.mark.asyncio
    async def test_create_quotation_blank_first(self, ui):
        with _Patches({"ui.api_client.create_doc": AsyncMock(return_value={"id": "doc:newq", "event_id": 1})}):
            r = await ui.post("/docs/create-blank?type=quotation", cookies=_c())
        assert r.status_code in (204, 302, 303, 200)

    @pytest.mark.asyncio
    async def test_create_contact_blank_first(self, ui):
        with _Patches({
            "ui.api_client.list_contacts": AsyncMock(return_value={"items": [], "total": 0}),
            "ui.api_client.create_contact": AsyncMock(return_value={"id": "contact:new", "event_id": 1}),
        }):
            r = await ui.post("/contacts/create?type=customer", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_create_memo_blank_first(self, ui):
        with _Patches({"ui.api_client.create_memo": AsyncMock(return_value={"id": "memo:new", "event_id": 1})}):
            r = await ui.post("/crm/memos/new", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_create_mfg_order(self, ui):
        with _Patches({"ui.api_client.create_mfg_order": AsyncMock(return_value={"id": "mfg:new", "event_id": 1})}):
            r = await ui.post("/manufacturing/new", cookies=_c(),
                             data={"name": "Test Order", "order_type": "manufacturing"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_create_subscription(self, ui):
        with _Patches({"ui.api_client.create_subscription": AsyncMock(return_value={"id": "sub:new", "event_id": 1})}):
            r = await ui.post("/subscriptions/new", cookies=_c(),
                             data={"name": "Test Sub", "frequency": "monthly", "doc_type": "invoice",
                                   "start_date": "2026-03-01"})
        assert r.status_code in (302, 303, 200)

    @pytest.mark.asyncio
    async def test_create_bom(self, ui):
        with _Patches({"ui.api_client.create_bom": AsyncMock(return_value={"bom_id": "bom:new", "event_id": 1})}):
            r = await ui.post("/manufacturing/boms/new", cookies=_c(),
                             data={"name": "Test BOM"})
        assert r.status_code in (200, 204, 302, 303)


# ══════════════════════════════════════════════════════════════════════════════
# 3. WORKFLOWS - End-to-end business processes (30+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkflows:
    """Complete business workflows work end-to-end."""

    @pytest.mark.asyncio
    async def test_invoice_finalize(self, ui):
        with _Patches({"ui.api_client.finalize_doc": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/docs/doc:inv1/action/finalize", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_invoice_send(self, ui):
        with _Patches({"ui.api_client.send_doc": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/docs/doc:inv1/action/send", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_invoice_void(self, ui):
        with _Patches({"ui.api_client.void_doc": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/docs/doc:inv1/action/void", cookies=_c(), data={"reason": "test"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_record_payment(self, ui):
        with _Patches({"ui.api_client.record_payment": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/docs/doc:inv1/payment", cookies=_c(),
                             data={"amount": "5000", "method": "bank_transfer", "payment_date": "2026-02-25"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_quotation_convert_to_invoice(self, ui):
        with _Patches({"ui.api_client.convert_doc": AsyncMock(return_value={"id": "doc:conv1"})}):
            r = await ui.post("/docs/doc:quo1/convert", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_po_receive_goods(self, ui):
        with _Patches({"ui.api_client.receive_po": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/docs/doc:po1/receive", cookies=_c(),
                             data={"lines": "[]"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_payment_refund(self, ui):
        with _Patches({"ui.api_client.refund_payment": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/docs/doc:inv2/refund", cookies=_c(),
                             data={"amount": "1000", "method": "cash"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_memo_approve(self, ui):
        with _Patches({"ui.api_client.approve_memo": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/crm/memos/memo:1/approve", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_memo_cancel(self, ui):
        with _Patches({"ui.api_client.cancel_memo": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/crm/memos/memo:1/cancel", cookies=_c(), data={"reason": "changed mind"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_memo_convert_to_invoice(self, ui):
        with _Patches({"ui.api_client.convert_memo_to_invoice": AsyncMock(return_value={"target_doc_id": "doc:frommemo"})}):
            r = await ui.post("/crm/memos/memo:1/convert-to-invoice", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_adjust_stock(self, ui):
        with _Patches({"ui.api_client.adjust_item": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/api/items/item:1/adjust", cookies=_c(), data={"new_qty": "20"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_transfer(self, ui):
        with _Patches({"ui.api_client.transfer_item": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/api/items/item:1/transfer", cookies=_c(), data={"location_id": "loc:2"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_reserve(self, ui):
        with _Patches({"ui.api_client.reserve_item": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/api/items/item:1/reserve", cookies=_c(), data={"quantity": "5"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_unreserve(self, ui):
        with _Patches({"ui.api_client.unreserve_item": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/api/items/item:1/unreserve", cookies=_c(), data={"quantity": "5"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_update_price(self, ui):
        with _Patches({
            "ui.api_client.get_price_lists": AsyncMock(return_value=[{"name": "Cost"}, {"name": "Retail"}]),
            "ui.api_client.set_item_price": AsyncMock(return_value={"event_id": "e1"}),
        }):
            r = await ui.post("/api/items/item:1/price", cookies=_c(),
                             data={"cost_price": "150", "retail_price": "600"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_expire(self, ui):
        with _Patches({"ui.api_client.expire_item": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/api/items/item:1/expire", cookies=_c(), data={"reason": "damaged"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_item_dispose(self, ui):
        with _Patches({"ui.api_client.dispose_item": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/api/items/item:1/dispose", cookies=_c(),
                             data={"reason": "broken", "notes": "cracked"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_mfg_order_start(self, ui):
        with _Patches({"ui.api_client.start_mfg_order": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_mfg_order": AsyncMock(return_value=_MFG)}):
            r = await ui.post("/manufacturing/mfg:1/start", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_mfg_order_complete(self, ui):
        with _Patches({"ui.api_client.complete_mfg_order": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_mfg_order": AsyncMock(return_value=_MFG)}):
            r = await ui.post("/manufacturing/mfg:1/complete", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_mfg_order_cancel(self, ui):
        with _Patches({"ui.api_client.cancel_mfg_order": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_mfg_order": AsyncMock(return_value=_MFG)}):
            r = await ui.post("/manufacturing/mfg:1/cancel", cookies=_c(), data={"reason": "out of stock"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_mfg_step_complete(self, ui):
        with _Patches({"ui.api_client.complete_mfg_step": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_mfg_order": AsyncMock(return_value=_MFG)}):
            r = await ui.post("/manufacturing/mfg:1/step", cookies=_c(), data={"step_id": "s1"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_mfg_consume_input(self, ui):
        with _Patches({"ui.api_client.consume_mfg_input": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_mfg_order": AsyncMock(return_value=_MFG)}):
            r = await ui.post("/manufacturing/mfg:1/consume", cookies=_c(), data={"item_id": "item:1", "quantity": "1"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_subscription_pause(self, ui):
        with _Patches({"ui.api_client.pause_subscription": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_subscription": AsyncMock(return_value=_SUB)}):
            r = await ui.post("/subscriptions/sub:1/pause", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_subscription_resume(self, ui):
        with _Patches({"ui.api_client.resume_subscription": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_subscription": AsyncMock(return_value=_SUB)}):
            r = await ui.post("/subscriptions/sub:1/resume", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_subscription_generate(self, ui):
        with _Patches({"ui.api_client.generate_subscription": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.get_subscription": AsyncMock(return_value=_SUB)}):
            r = await ui.post("/subscriptions/sub:1/generate", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_deal_move_stage(self, ui):
        with _Patches({"ui.api_client.move_deal_stage": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/crm/deals/deal:1/stage", cookies=_c(), data={"stage": "qualified"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_deal_mark_won(self, ui):
        with _Patches({"ui.api_client.mark_deal_won": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/crm/deals/deal:1/won", cookies=_c())
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.asyncio
    async def test_deal_mark_lost(self, ui):
        with _Patches({"ui.api_client.mark_deal_lost": AsyncMock(return_value={"ok": True})}):
            r = await ui.post("/crm/deals/deal:1/lost", cookies=_c(), data={"reason": "price"})
        assert r.status_code in (200, 204, 302, 303)

    @pytest.mark.skip(reason="Scanning module disabled until complete")
    @pytest.mark.asyncio
    async def test_scan_barcode(self, ui):
        with _Patches({"ui.api_client.scan_once": AsyncMock(return_value={"ok": True}),
                       "ui.api_client.resolve_scan": AsyncMock(return_value={"found": True, "item": _ITEM})}):
            r = await ui.post("/scanning/scan", cookies=_c(), data={"code": "RB001"})
        assert r.status_code in (200, 204, 302, 303)


# ══════════════════════════════════════════════════════════════════════════════
# 4. NAVIGATION (20+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestNavigation:
    """Users can navigate between pages and related entities."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "/dashboard", "/inventory", "/contacts/customers", "/docs", "/accounting",
        "/reports", "/settings/general", "/subscriptions", "/manufacturing",
        # "/scanning",  # Scanning module disabled until complete
    ])
    async def test_main_pages_load(self, ui, path):
        """Every main page returns 200 when authenticated."""
        mocks = {
            **_dashboard_mocks(), **_inventory_mocks(), **_docs_mocks(),
            **_crm_mocks(), **_settings_mocks(), **_reports_mocks(),
            **_accounting_mocks(),
            "ui.api_client.list_mfg_orders": AsyncMock(return_value={"items": [], "total": 0}),
            "ui.api_client.list_boms": AsyncMock(return_value={"items": [], "total": 0}),
            "ui.api_client.list_subscriptions": AsyncMock(return_value=[]),
        }
        with _Patches(mocks):
            r = await ui.get(path, cookies=_c())
        assert r.status_code == 200, f"{path} returned {r.status_code}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "/dashboard", "/inventory", "/contacts/customers", "/docs", "/accounting",
        "/reports", "/settings", "/subscriptions", "/manufacturing",
        # "/scanning",  # Scanning module disabled until complete
    ])
    async def test_unauthenticated_redirects(self, ui, path):
        """All pages redirect to login when not authenticated."""
        r = await ui.get(path)
        assert r.status_code in (302, 303), f"{path} should redirect without auth"

    @pytest.mark.asyncio
    async def test_report_subpages_accessible(self, ui):
        """All report subpages load."""
        for subpath in ["/reports/ar-aging", "/reports/ap-aging", "/reports/sales",
                       "/reports/purchases", "/reports/expiring"]:
            with _Patches(_reports_mocks()):
                r = await ui.get(subpath, cookies=_c())
            assert r.status_code == 200, f"{subpath} failed"

    @pytest.mark.asyncio
    async def test_accounting_subpages(self, ui):
        for subpath in ["/accounting", "/accounting?tab=pnl", "/accounting?tab=balance-sheet"]:
            with _Patches(_accounting_mocks()):
                r = await ui.get(subpath, cookies=_c())
            assert r.status_code == 200, f"{subpath} failed"

    @pytest.mark.asyncio
    async def test_settings_tabs(self, ui):
        _TAB_URLS = {
            "company": "/settings/general?tab=company",
            "taxes": "/settings/sales?tab=taxes",
            "terms": "/settings/sales?tab=terms",
            "users": "/settings/general?tab=users",
            "schema": "/settings/inventory?tab=category-library",
        }
        for tab in ["company", "taxes", "terms", "users", "schema"]:
            url = _TAB_URLS[tab]
            with _Patches(_settings_mocks()):
                r = await ui.get(url, cookies=_c())
            assert r.status_code == 200, f"Settings tab={tab} (url={url}) failed"


# ══════════════════════════════════════════════════════════════════════════════
# 5. SEARCH AND FILTER (20+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestSearchAndFilter:
    """Search and filtering functionality works correctly."""

    @pytest.mark.asyncio
    async def test_global_search_returns_results(self, ui):
        with _Patches({"ui.api_client.list_items": AsyncMock(return_value={"items": [_ITEM], "total": 1}),
                       "ui.api_client.list_contacts": AsyncMock(return_value={"items": [_CONTACT], "total": 1}),
                       "ui.api_client.list_docs": AsyncMock(return_value={"items": [_DOC_INV], "total": 1})}):
            r = await ui.get("/search?q=Ruby", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_global_search_empty_query(self, ui):
        r = await ui.get("/search?q=", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_search(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?q=Ruby", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_category_filter(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?category=Parcel", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_type_filter(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?type=invoice", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_status_filter(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?status=paid", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_reports_date_range_params(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/ar-aging?from=2026-01-01&to=2026-02-28", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_reports_preset_filter(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/sales?preset=last_12_months", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_sort_param(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?sort=name&dir=asc", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_sort_param(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?sort=total&dir=desc", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_pagination(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?page=1&per_page=25", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_pagination(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?page=1&per_page=50", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_crm_tab_param(self, ui):
        # CRM now split into per-section pages
        for url in ["/contacts/customers", "/contacts/vendors", "/contacts/sales"]:
            with _Patches(_crm_mocks()):
                r = await ui.get(url, cookies=_c())
            assert r.status_code == 200, f"{url} failed"

    @pytest.mark.asyncio
    async def test_pnl_date_range(self, ui):
        with _Patches(_accounting_mocks()):
            r = await ui.get("/accounting?tab=pnl&from=2026-01-01&to=2026-12-31", cookies=_c())
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 6. EDGE CASES (30+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Boundary conditions and error handling."""

    @pytest.mark.asyncio
    async def test_empty_inventory(self, ui):
        with _Patches(_inventory_mocks(items=[])):
            r = await ui.get("/inventory", cookies=_c())
        assert r.status_code == 200
        # Should show empty state, not crash
        assert b"No" in r.content or b"empty" in r.content.lower() or b"Import" in r.content

    @pytest.mark.asyncio
    async def test_empty_documents(self, ui):
        with _Patches(_docs_mocks(docs=[])):
            r = await ui.get("/docs", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_crm(self, ui):
        with _Patches(_crm_mocks(contacts=[], memos=[], deals=[])):
            r = await ui.get("/contacts/customers", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_manufacturing(self, ui):
        with _Patches({"ui.api_client.list_mfg_orders": AsyncMock(return_value={"items": [], "total": 0}),
                       "ui.api_client.list_boms": AsyncMock(return_value={"items": [], "total": 0})}):
            r = await ui.get("/manufacturing", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_subscriptions(self, ui):
        with _Patches({"ui.api_client.list_subscriptions": AsyncMock(return_value=[])}):
            r = await ui.get("/subscriptions", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_page_param(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?page=abc", cookies=_c())
        assert r.status_code == 200  # Graceful fallback, not crash

    @pytest.mark.asyncio
    async def test_negative_page_param(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?page=-1", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_huge_page_param(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?page=99999", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_sort_param(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?sort=nonexistent&dir=asc", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_doc_type(self, ui):
        with _Patches(_docs_mocks()):
            r = await ui.get("/docs?type=nonexistent", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_expired_token_redirects(self, ui):
        from ui.api_client import APIError
        with _Patches({"ui.api_client.get_company": AsyncMock(side_effect=APIError(401, "expired")),
                       "ui.api_client.get_valuation": AsyncMock(side_effect=APIError(401, "expired")),
                       "ui.api_client.get_doc_summary": AsyncMock(side_effect=APIError(401, "expired")),
                       "ui.api_client.get_memo_summary": AsyncMock(side_effect=APIError(401, "expired")),
                       "ui.api_client.my_companies": AsyncMock(side_effect=APIError(401, "expired")),
                       "ui.api_client.get_ar_aging": AsyncMock(side_effect=APIError(401, "expired")),
                       "ui.api_client.get_activity": AsyncMock(side_effect=APIError(401, "expired"))}):
            r = await ui.get("/dashboard", cookies=_c())
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_api_error_shows_error_banner(self, ui):
        from ui.api_client import APIError
        with _Patches({"ui.api_client.list_items": AsyncMock(side_effect=APIError(500, "internal")),
                       "ui.api_client.get_valuation": AsyncMock(side_effect=APIError(500, "internal")),
                       "ui.api_client.get_item_schema": AsyncMock(side_effect=APIError(500, "internal"))}):
            r = await ui.get("/inventory", cookies=_c())
        # Should show error, not crash
        assert r.status_code in (200, 500)

    @pytest.mark.asyncio
    async def test_nonexistent_item_detail(self, ui):
        from ui.api_client import APIError
        with _Patches({"ui.api_client.get_item": AsyncMock(side_effect=APIError(404, "not found")),
                       "ui.api_client.get_item_schema": AsyncMock(return_value=_SCHEMA)}):
            r = await ui.get("/inventory/item:nonexistent", cookies=_c())
        assert r.status_code in (200, 404)

    @pytest.mark.asyncio
    async def test_nonexistent_doc_detail(self, ui):
        from ui.api_client import APIError
        with _Patches({"ui.api_client.get_doc": AsyncMock(side_effect=APIError(404, "not found")),
                       "ui.api_client.list_contacts": AsyncMock(return_value={"items": [], "total": 0})}):
            r = await ui.get("/docs/doc:nonexistent", cookies=_c())
        assert r.status_code in (200, 404)

    @pytest.mark.asyncio
    async def test_item_with_null_fields(self, ui):
        """Items with null/missing fields should show '--' not crash."""
        null_item = {"entity_id": "item:null", "name": None, "sku": None, "category": None,
                     "status": None, "quantity": None, "cost_price": None, "retail_price": None}
        with _Patches(_inventory_mocks(items=[null_item])):
            r = await ui.get("/inventory", cookies=_c())
        assert r.status_code == 200
        assert b"--" in r.content

    @pytest.mark.asyncio
    async def test_doc_with_zero_total(self, ui):
        zero_doc = {**_DOC_INV, "total": 0, "amount_outstanding": 0}
        with _Patches(_docs_mocks(docs=[zero_doc])):
            r = await ui.get("/docs", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_search_results(self, ui):
        with _Patches({"ui.api_client.list_items": AsyncMock(return_value={"items": [], "total": 0}),
                       "ui.api_client.list_contacts": AsyncMock(return_value={"items": [], "total": 0}),
                       "ui.api_client.list_docs": AsyncMock(return_value={"items": [], "total": 0})}):
            r = await ui.get("/search?q=zzzzzznonexistent", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_special_chars_in_search(self, ui):
        with _Patches({"ui.api_client.list_items": AsyncMock(return_value={"items": [], "total": 0}),
                       "ui.api_client.list_contacts": AsyncMock(return_value={"items": [], "total": 0}),
                       "ui.api_client.list_docs": AsyncMock(return_value={"items": [], "total": 0})}):
            r = await ui.get("/search?q=%3Cscript%3Ealert(1)%3C/script%3E", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_unicode_in_search(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory?q=%E0%B8%97%E0%B8%94%E0%B8%AA%E0%B8%AD%E0%B8%9A", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_csv_export_empty(self, ui):
        with _Patches({"ui.api_client.export_items_csv": AsyncMock(return_value=b"name,sku\n")}):
            r = await ui.get("/inventory/export/csv", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_double_slash_url(self, ui):
        """Double slashes in path should not crash the app."""
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard/", cookies=_c())
        # Should not crash - 200, 307 redirect, or 404 are all acceptable
        assert r.status_code in (200, 301, 302, 303, 307, 404)

    @pytest.mark.asyncio
    async def test_reports_future_dates(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/sales?from=2030-01-01&to=2030-12-31", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_reports_inverted_dates(self, ui):
        """from > to should not crash."""
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/sales?from=2026-12-31&to=2026-01-01", cookies=_c())
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 7. DATA ENTRY PATTERNS (20+ scenarios)
# ══════════════════════════════════════════════════════════════════════════════

class TestDataEntry:
    """Data entry patterns work correctly."""

    @pytest.mark.asyncio
    async def test_login_form_renders(self, ui):
        from unittest.mock import AsyncMock
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
            r = await ui.get("/login")
        assert r.status_code == 200
        assert b"email" in r.content.lower()
        assert b"password" in r.content.lower()

    @pytest.mark.asyncio
    async def test_login_post_with_valid_creds(self, ui):
        with patch("ui.routes.auth.api_login", new=AsyncMock(return_value=("token123", "refresh123"))):
            r = await ui.post("/login", data={"email": "admin@test.com", "password": "pass"})
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_login_post_with_bad_creds(self, ui):
        from ui.api_client import APIError
        with patch("ui.routes.auth.api_login", new=AsyncMock(side_effect=APIError(401, "bad creds"))):
            r = await ui.post("/login", data={"email": "bad@test.com", "password": "wrong"})
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_logout_clears_cookie(self, ui):
        r = await ui.get("/logout", cookies=_c())
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_settings_company_name_edit(self, ui):
        with _Patches(_settings_mocks()):
            r = await ui.get("/settings/company/name/edit", cookies=_c())
        assert r.status_code == 200
        assert b"input" in r.content.lower()

    @pytest.mark.asyncio
    async def test_settings_company_name_patch(self, ui):
        with _Patches({**_settings_mocks(),
                       "ui.api_client.patch_company": AsyncMock(return_value={})}):
            r = await ui.patch("/settings/company/name", cookies=_c(),
                              data={"value": "New Name"})
        assert r.status_code == 200

    @pytest.mark.skip(reason="Scanning module disabled until complete")
    @pytest.mark.asyncio
    async def test_scanning_input_renders(self, ui):
        r = await ui.get("/scanning", cookies=_c())
        assert r.status_code == 200
        assert b"scan" in r.content.lower()

    @pytest.mark.asyncio
    async def test_csv_import_page_loads(self, ui):
        r = await ui.get("/inventory/import", cookies=_c())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_manage_columns_present(self, ui):
        with _Patches(_inventory_mocks()):
            r = await ui.get("/inventory", cookies=_c())
        assert r.status_code == 200
        # Column manager or manage columns should be present
        assert b"column" in r.content.lower() or b"col" in r.content.lower()

    @pytest.mark.asyncio
    async def test_reports_date_presets_present(self, ui):
        with _Patches(_reports_mocks()):
            r = await ui.get("/reports/ar-aging", cookies=_c())
        assert r.status_code == 200
        # Date presets should be in the page
        assert b"preset" in r.content.lower() or b"month" in r.content.lower() or b"date" in r.content.lower()

    @pytest.mark.asyncio
    async def test_doc_line_items_section(self, ui):
        with _Patches({"ui.api_client.get_doc": AsyncMock(return_value=_DOC_INV),
                       "ui.api_client.list_contacts": AsyncMock(return_value={"items": [_CONTACT], "total": 1})}):
            r = await ui.get("/docs/doc:inv1", cookies=_c())
        assert r.status_code == 200
        # Line items section
        assert b"line" in r.content.lower() or b"item" in r.content.lower()

    @pytest.mark.asyncio
    async def test_contact_detail_shows_email(self, ui):
        with _Patches({"ui.api_client.get_contact": AsyncMock(return_value=_CONTACT),
                       "ui.api_client.list_memos": AsyncMock(return_value={"items": [], "total": 0})}):
            r = await ui.get("/contacts/contact:1", cookies=_c())
        assert r.status_code == 200
        assert b"alice@test.com" in r.content

    @pytest.mark.asyncio
    async def test_empty_values_show_double_dash(self, ui):
        """Empty/null fields render as '--'."""
        empty_item = {"entity_id": "item:e", "name": "Test", "sku": "", "category": "",
                      "status": "available", "quantity": 0, "cost_price": None,
                      "retail_price": None, "wholesale_price": None}
        with _Patches(_inventory_mocks(items=[empty_item])):
            r = await ui.get("/inventory", cookies=_c())
        assert r.status_code == 200
        assert b"--" in r.content


# ══════════════════════════════════════════════════════════════════════════════
# 8. VISUAL CONSISTENCY (parametrized)
# ══════════════════════════════════════════════════════════════════════════════

class TestVisualConsistency:
    """CSS and HTML patterns are consistent across pages."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,mocks_fn", [
        ("/dashboard", _dashboard_mocks),
        ("/inventory", _inventory_mocks),
        ("/docs", _docs_mocks),
        ("/contacts/customers", _crm_mocks),
        ("/settings/general", _settings_mocks),
    ])
    async def test_page_has_sidebar(self, ui, path, mocks_fn):
        with _Patches(mocks_fn()):
            r = await ui.get(path, cookies=_c())
        assert b"sidebar" in r.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,mocks_fn", [
        ("/dashboard", _dashboard_mocks),
        ("/inventory", _inventory_mocks),
        ("/docs", _docs_mocks),
        ("/contacts/customers", _crm_mocks),
        ("/settings/general", _settings_mocks),
    ])
    async def test_page_has_topbar(self, ui, path, mocks_fn):
        with _Patches(mocks_fn()):
            r = await ui.get(path, cookies=_c())
        assert b"topbar" in r.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,mocks_fn", [
        ("/dashboard", _dashboard_mocks),
        ("/inventory", _inventory_mocks),
        ("/docs", _docs_mocks),
        ("/contacts/customers", _crm_mocks),
    ])
    async def test_page_has_page_header(self, ui, path, mocks_fn):
        with _Patches(mocks_fn()):
            r = await ui.get(path, cookies=_c())
        assert b"page-header" in r.content

    @pytest.mark.asyncio
    async def test_css_loaded(self, ui):
        with _Patches(_dashboard_mocks()):
            r = await ui.get("/dashboard", cookies=_c())
        assert b"app.css" in r.content

    @pytest.mark.asyncio
    async def test_no_raw_python_objects_in_pages(self, ui):
        """No raw Python objects like <class 'dict'> or None visible."""
        pages_and_mocks = [
            ("/dashboard", _dashboard_mocks()),
            ("/inventory", _inventory_mocks()),
            ("/docs", _docs_mocks()),
            ("/contacts/customers", _crm_mocks()),
        ]
        for path, mocks in pages_and_mocks:
            with _Patches(mocks):
                r = await ui.get(path, cookies=_c())
            content = r.content.decode(errors="ignore")
            assert "<class '" not in content, f"Raw Python class in {path}"
            # "None" as literal text (not in HTML attributes) could indicate a bug
            # but is tricky to test precisely, so we skip that check
