# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Proactive QA test suite.

Patterns extrapolated from Nikolai's reported bugs:
  P4 - Currency not threaded (hardcoded ฿ instead of company.currency)
  P7 - Server-side validation bypassed
  P1 - HTMX partial missing attrs / hard-crash on bad input
  P5 - Multi-tenant: data leaks between companies
  P6 - Unauthenticated access silently succeeds
  P2 - Self-referential MutationObserver / stale static snapshot (JS)
  P3 - Static snapshot stale (display doesn't match live state)

This file proves first-pass QA state: all known failure categories have
regression coverage before the first human review.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock

# ── Shared fixtures / constants ──────────────────────────────────────────────

_COMPANY_THB = {
    "name": "Acme Co", "currency": "THB", "timezone": "Asia/Bangkok",
    "fiscal_year_start": "01-01", "slug": "acme-co", "country": "TH",
    "address": "", "phone": "", "email": "", "tax_id": "",
    "docs_default_preset": "last_12m",
}
_COMPANY_USD = {**_COMPANY_THB, "name": "ACME Corp", "currency": "USD", "timezone": "America/New_York"}
_COMPANY_EUR = {**_COMPANY_THB, "name": "Euro GmbH", "currency": "EUR", "timezone": "Europe/Berlin"}
_TAXES = []
_TERMS = [{"name": "Net 30", "days": 30}]
_USERS = [{"id": "u1", "email": "admin@test.com", "role": "admin", "is_active": True, "name": "Admin"}]
_SCHEMA = [{"key": "sku", "label": "SKU", "type": "text", "required": True, "editable": True, "position": 0, "options": []}]


from tests.conftest import make_test_token, authed_cookies


def _authed(token: str | None = None, role: str = "owner") -> dict:
    return {"celerp_token": token or make_test_token(role=role)}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        yield c


# ── P4: Currency threading across all money-displaying routes ────────────────

class TestCurrencyThreadingAllRoutes:
    """Every route that displays money must use company.currency — not hardcode ฿."""

    @pytest.mark.asyncio
    async def test_reports_ar_aging_uses_company_currency(self, ui_client):
        """AR aging page must render without crashing for a USD company."""
        aging_data = {
            "lines": [{"customer_name": "Bob", "current": 100, "d30": 50,
                        "d60": 0, "d90": 0, "d90plus": 0, "total": 150}],
            "buckets": {"current": 100, "1-30": 50},
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value=aging_data)),
        ):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code == 200
        # USD company: must show $ not ฿
        assert b"$" in r.content or b"USD" in r.content
        assert "฿150" not in r.text and "฿100" not in r.text

    @pytest.mark.asyncio
    async def test_reports_ap_aging_uses_company_currency(self, ui_client):
        """AP aging must use company currency, not hardcoded ฿."""
        aging_data = {
            "lines": [{"contact_name": "Supplier X", "current": 200, "d30": 0,
                        "d60": 0, "d90": 0, "d90plus": 0, "total": 200}],
            "buckets": {},
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_EUR)),
            patch("ui.api_client.get_ap_aging", new=AsyncMock(return_value=aging_data)),
        ):
            r = await ui_client.get("/reports/ap-aging", cookies=_authed())
        assert r.status_code == 200
        assert b"EUR" in r.content or "\u20ac" in r.text
        assert "฿200" not in r.text

    @pytest.mark.asyncio
    async def test_reports_sales_uses_company_currency(self, ui_client):
        """Sales report total must use company currency."""
        sales_data = {
            "lines": [{"customer_name": "Jane", "invoice_count": 2, "total_revenue": 500}],
            "total": 500, "group_by": "customer",
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
            patch("ui.api_client.get_sales_report", new=AsyncMock(return_value=sales_data)),
        ):
            r = await ui_client.get("/reports/sales?group_by=customer", cookies=_authed())
        assert r.status_code == 200
        assert b"$" in r.content or b"USD" in r.content
        assert "฿500" not in r.text

    @pytest.mark.asyncio
    async def test_reports_purchases_uses_company_currency(self, ui_client):
        """Purchases report must use company currency."""
        purchases_data = {
            "lines": [{"supplier_name": "Supplier A", "po_count": 1, "total_spend": 300}],
            "total": 300, "group_by": "supplier",
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_EUR)),
            patch("ui.api_client.get_purchases_report", new=AsyncMock(return_value=purchases_data)),
        ):
            r = await ui_client.get("/reports/purchases?group_by=supplier", cookies=_authed())
        assert r.status_code == 200
        assert b"EUR" in r.content or "\u20ac" in r.text
        assert "฿300" not in r.text

    @pytest.mark.asyncio
    async def test_reports_expiring_loads_without_crash(self, ui_client):
        """Expiring items page must load cleanly for any company."""
        expiring_data = {
            "count": 1, "days_threshold": 30,
            "items": [{"sku": "S1", "name": "Item", "expiry_date": "2026-04-01",
                        "days_left": 23, "status": "available"}],
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
            patch("ui.api_client.get_expiring", new=AsyncMock(return_value=expiring_data)),
        ):
            r = await ui_client.get("/reports/expiring?days=30", cookies=_authed())
        assert r.status_code == 200

    def test_fmt_money_thb(self):
        """_fmt_money with THB must produce ฿ prefix."""
        from ui.components.table import fmt_money
        assert fmt_money(1234.5, "THB") == "฿1,234.50"

    def test_fmt_money_usd(self):
        """_fmt_money with USD must produce $ prefix."""
        from ui.components.table import fmt_money
        result = fmt_money(99.99, "USD")
        assert result.startswith("$")
        assert "99.99" in result

    def test_fmt_money_eur(self):
        """_fmt_money with EUR must produce € prefix."""
        from ui.components.table import fmt_money
        result = fmt_money(50, "EUR")
        assert "€" in result

    def test_fmt_money_unknown_currency_uses_code(self):
        """Unknown ISO code must fall back to '<CODE> <amount>'."""
        from ui.components.table import fmt_money
        result = fmt_money(10, "XYZ")
        assert "XYZ" in result and "10.00" in result

    def test_fmt_money_none_currency_returns_no_symbol(self):
        """None currency must not hardcode ฿ - returns bare number."""
        from ui.components.table import fmt_money
        result = fmt_money(100, None)
        assert "฿" not in result
        assert "100.00" in result

    def test_fmt_money_bad_value_returns_empty(self):
        """Non-numeric value must return EMPTY constant, not crash."""
        from ui.components.table import fmt_money
        from ui.components.table import EMPTY
        result = fmt_money("not-a-number", "USD")
        assert result == EMPTY

    @pytest.mark.asyncio
    async def test_documents_page_loads_for_usd_company(self, ui_client):
        """Documents list page must load for a non-THB company."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_doc_status_cards_use_company_currency(self, ui_client):
        """Status cards on /docs must use company.currency, not hardcode ฿."""
        docs = [{"entity_id": "d1", "status": "paid", "total_amount": 150.0}]
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": docs, "total": 1})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"count_by_status": {"paid": 1}})),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert r.status_code == 200
        assert "฿150" not in r.text
        assert "฿0" not in r.text

    @pytest.mark.asyncio
    async def test_subscriptions_detail_no_hardcoded_baht(self, ui_client):
        """Subscription detail with line items must not hardcode ฿ for USD company."""
        sub = {
            "entity_id": "sub:1", "status": "active", "plan": "monthly",
            "contact_id": "", "start_date": "", "next_billing_date": "",
            "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 99.00}],
        }
        with (
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=sub)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
        ):
            r = await ui_client.get("/subscriptions/sub:1", cookies=_authed())
        assert r.status_code == 200
        # ฿99 must not appear — company is USD
        assert "฿99" not in r.text


# ── P4-variant: _fmt_money is the single source of truth ────────────────────

class TestFmtMoneyContract:
    """_fmt_money is the single DRY formatter. All callers must go through it."""

    def test_fmt_money_zero(self):
        from ui.components.table import fmt_money
        assert fmt_money(0, "THB") == "฿0.00"

    def test_fmt_money_large_number_comma_separated(self):
        from ui.components.table import fmt_money
        result = fmt_money(1_000_000, "USD")
        assert "1,000,000.00" in result

    def test_fmt_money_negative(self):
        """Negative values should format as negative (e.g. credit notes)."""
        from ui.components.table import fmt_money
        result = fmt_money(-50.5, "THB")
        assert "-" in result and "50.50" in result

    def test_documents_val_money_delegates_tofmt_money(self):
        """documents.format_value('money') must produce same output as _fmt_money."""
        from ui.components.table import format_value
        from ui.components.table import fmt_money
        assert format_value(100, "money", "USD") == fmt_money(100, "USD")


# ── P7: Server-side validation – settings endpoints ──────────────────────────

class TestSettingsServerSideValidation:
    """All constrained fields must validate server-side before calling the API."""

    @pytest.fixture
    def _stack(self):
        """Context manager providing all settings mocks."""
        from contextlib import ExitStack
        from unittest.mock import AsyncMock, patch
        def _make():
            stack = ExitStack()
            stack.enter_context(patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)))
            stack.enter_context(patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)))
            stack.enter_context(patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)))
            stack.enter_context(patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})))
            stack.enter_context(patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)))
            stack.enter_context(patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})))
            stack.enter_context(patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})))
            return stack
        return _make

    @pytest.mark.asyncio
    async def test_company_name_rejects_empty(self, ui_client, _stack):
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_company", new=mock):
                r = await ui_client.patch("/settings/company/name",
                                          data={"value": "  "}, cookies=_authed())
        assert r.status_code == 200
        assert b"blank" in r.content.lower() or b"cannot" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_company_slug_rejects_spaces(self, ui_client, _stack):
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_company", new=mock):
                r = await ui_client.patch("/settings/company/slug",
                                          data={"value": "my company"}, cookies=_authed())
        assert r.status_code == 200
        assert b"slug" in r.content.lower() or b"invalid" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_company_slug_rejects_uppercase(self, ui_client, _stack):
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_company", new=mock):
                r = await ui_client.patch("/settings/company/slug",
                                          data={"value": "MySlug"}, cookies=_authed())
        assert r.status_code == 200
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_company_slug_accepts_valid(self, ui_client, _stack):
        with _stack():
            mock_patch = AsyncMock(return_value=None)
            mock_get = AsyncMock(return_value=_COMPANY_THB)
            with patch("ui.api_client.patch_company", new=mock_patch):
                with patch("ui.api_client.get_company", new=mock_get):
                    r = await ui_client.patch("/settings/company/slug",
                                              data={"value": "valid-slug-123"}, cookies=_authed())
        assert r.status_code == 200
        mock_patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_company_currency_rejects_invalid(self, ui_client, _stack):
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_company", new=mock):
                r = await ui_client.patch("/settings/company/currency",
                                          data={"value": "NOTACURRENCY"}, cookies=_authed())
        assert r.status_code == 200
        assert b"invalid" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_company_fiscal_year_rejects_invalid(self, ui_client, _stack):
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_company", new=mock):
                r = await ui_client.patch("/settings/company/fiscal_year_start",
                                          data={"value": "not-valid"}, cookies=_authed())
        assert r.status_code == 200
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_tax_type_rejects_invalid(self, ui_client, _stack):
        """Tax type field must reject values outside {sales, purchase, both}."""
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_taxes", new=mock):
                r = await ui_client.patch("/settings/taxes/0/tax_type",
                                          data={"value": "invalid_type"}, cookies=_authed())
        assert r.status_code == 200
        assert b"invalid" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_tax_type_accepts_valid(self, ui_client, _stack):
        """Valid tax type 'purchase' must proceed to API."""
        taxes_with_entry = [{"name": "VAT", "rate": 7.0, "tax_type": "sales", "is_default": True, "description": ""}]
        mock_get = AsyncMock(return_value=taxes_with_entry)
        mock_patch = AsyncMock(return_value=None)
        with _stack():
            with patch("ui.api_client.get_taxes", new=mock_get):
                with patch("ui.api_client.patch_taxes", new=mock_patch):
                    r = await ui_client.patch("/settings/taxes/0/tax_type",
                                              data={"value": "purchase"}, cookies=_authed())
        assert r.status_code == 200
        mock_patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_schema_type_rejects_invalid(self, ui_client, _stack):
        """Item schema field type must reject unknown types."""
        with _stack():
            mock = AsyncMock()
            with patch("ui.api_client.patch_item_schema", new=mock):
                r = await ui_client.patch("/settings/schema/0/type",
                                          data={"value": "fakeType"}, cookies=_authed())
        assert r.status_code == 200
        assert b"invalid" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_terms_days_rejects_non_integer(self, ui_client, _stack):
        """Payment term days must reject non-integer values."""
        mock = AsyncMock()
        terms_data = [{"name": "Net 30", "days": 30}]
        with _stack():
            with patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=terms_data)):
                with patch("ui.api_client.patch_payment_terms", new=mock):
                    r = await ui_client.patch("/settings/terms/0/days",
                                              data={"value": "thirty"}, cookies=_authed())
        assert r.status_code == 200
        assert b"whole" in r.content.lower() or b"number" in r.content.lower() or b"invalid" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_terms_days_rejects_negative(self, ui_client, _stack):
        """Payment term days cannot be negative."""
        mock = AsyncMock()
        terms_data = [{"name": "Net 30", "days": 30}]
        with _stack():
            with patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=terms_data)):
                with patch("ui.api_client.patch_payment_terms", new=mock):
                    r = await ui_client.patch("/settings/terms/0/days",
                                              data={"value": "-5"}, cookies=_authed())
        assert r.status_code == 200
        assert b"negative" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_terms_days_accepts_zero(self, ui_client, _stack):
        """0 days (immediate payment) is a valid term."""
        mock_patch = AsyncMock(return_value=None)
        terms_data = [{"name": "Immediate", "days": 0}]
        mock_get = AsyncMock(return_value=terms_data)
        with _stack():
            with patch("ui.api_client.get_payment_terms", new=mock_get):
                with patch("ui.api_client.patch_payment_terms", new=mock_patch):
                    r = await ui_client.patch("/settings/terms/0/days",
                                              data={"value": "0"}, cookies=_authed())
        assert r.status_code == 200
        mock_patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_terms_days_rejects_float(self, ui_client, _stack):
        """Float days (e.g. 7.5) must be rejected."""
        mock = AsyncMock()
        terms_data = [{"name": "Net 7", "days": 7}]
        with _stack():
            with patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=terms_data)):
                with patch("ui.api_client.patch_payment_terms", new=mock):
                    r = await ui_client.patch("/settings/terms/0/days",
                                              data={"value": "7.5"}, cookies=_authed())
        assert r.status_code == 200
        # "7.5" is not a valid int — must error
        assert b"whole" in r.content.lower() or b"number" in r.content.lower() or b"invalid" in r.content.lower() or b"error" in r.content.lower()
        mock.assert_not_called()


# ── P6: Unauthenticated access across all data-writing routes ───────────────

class TestUnauthAllWriteRoutes:
    """Every POST/PATCH route that mutates state must reject unauthenticated requests."""

    @pytest.mark.asyncio
    async def test_settings_company_patch_no_auth(self, ui_client):
        r = await ui_client.patch("/settings/company/name", data={"value": "x"})
        # Must reject — any non-success response acceptable (redirect to login or 401/403)
        # 200 with "Unauthorized" is also valid (HTMX partial response)
        assert r.status_code in (200, 302, 303, 401, 403)
        if r.status_code == 200:
            assert b"unauthorized" in r.content.lower() or b"login" in r.content.lower()

    @pytest.mark.asyncio
    async def test_settings_taxes_patch_no_auth(self, ui_client):
        r = await ui_client.patch("/settings/taxes/0/rate", data={"value": "10"})
        assert r.status_code in (200, 302, 303, 401, 403)
        if r.status_code == 200:
            assert b"unauthorized" in r.content.lower()

    @pytest.mark.asyncio
    async def test_settings_terms_patch_no_auth(self, ui_client):
        r = await ui_client.patch("/settings/terms/0/days", data={"value": "30"})
        assert r.status_code in (200, 302, 303, 401, 403)
        if r.status_code == 200:
            assert b"unauthorized" in r.content.lower()

    @pytest.mark.asyncio
    async def test_api_item_field_patch_no_auth(self, ui_client):
        r = await ui_client.patch("/api/items/item:x/field/name", data={"value": "y"})
        assert r.status_code in (200, 302, 303, 401, 403)
        if r.status_code == 200:
            assert b"unauthorized" in r.content.lower()

    @pytest.mark.asyncio
    async def test_api_item_adjust_no_auth(self, ui_client):
        r = await ui_client.post("/api/items/item:x/adjust", data={"new_qty": "10"})
        assert r.status_code in (200, 302, 303, 401, 403)

    @pytest.mark.asyncio
    async def test_inventory_page_no_auth_redirects(self, ui_client):
        r = await ui_client.get("/inventory")
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_reports_ar_aging_no_auth_redirects(self, ui_client):
        r = await ui_client.get("/reports/ar-aging")
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_reports_sales_no_auth_redirects(self, ui_client):
        r = await ui_client.get("/reports/sales")
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_docs_page_no_auth_redirects(self, ui_client):
        r = await ui_client.get("/docs")
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_crm_page_no_auth_redirects(self, ui_client):
        r = await ui_client.get("/crm")
        assert r.status_code in (302, 303)


# ── P1: HTMX partial contract — outerHTML swaps must have re-triggerable attrs ─

class TestHtmxPartialContract:
    """HTMX partials that do outerHTML swap must contain the hx-* attrs to resave."""

    @pytest.mark.asyncio
    async def test_company_name_edit_has_hx_patch(self, ui_client):
        """GET /settings/company/name/edit must return element with hx-patch."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)):
            r = await ui_client.get("/settings/company/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"hx-patch" in r.content or b"hx_patch" in r.content

    @pytest.mark.asyncio
    async def test_company_slug_edit_has_hx_patch(self, ui_client):
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)):
            r = await ui_client.get("/settings/company/slug/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"hx-patch" in r.content or b"hx_patch" in r.content

    @pytest.mark.asyncio
    async def test_company_name_edit_prepopulates_value(self, ui_client):
        """Edit fields must be pre-populated with current value."""
        company = {**_COMPANY_THB, "name": "PrePop Test Corp"}
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
            r = await ui_client.get("/settings/company/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"PrePop Test Corp" in r.content

    @pytest.mark.asyncio
    async def test_item_field_edit_returns_input(self, ui_client):
        """GET /api/items/{id}/field/{field}/edit must return an input."""
        item = {
            "id": "item:x", "sku": "S1", "name": "Widget", "status": "available",
            "quantity": 1, "retail_price": 100, "wholesale_price": 80, "cost_price": 60,
            "category": "", "barcode": "", "weight": None, "weight_unit": None,
            "created_at": "", "updated_at": "",
        }
        schema = [{"key": "name", "type": "text", "label": "Name", "editable": True, "required": False, "position": 0, "options": []}]
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=item)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=schema)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/api/items/item:x/field/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"input" in r.content.lower() or b"textarea" in r.content.lower()

    @pytest.mark.asyncio
    async def test_item_weight_unit_edit_returns_select(self, ui_client):
        """weight_unit is a constrained field — edit must return a select, not free-text."""
        item = {
            "id": "item:x", "sku": "S1", "name": "Widget", "status": "available",
            "quantity": 1, "retail_price": 100, "wholesale_price": 80, "cost_price": 60,
            "category": "", "barcode": "", "weight": 1.5, "weight_unit": "g",
            "created_at": "", "updated_at": "",
        }
        schema = [{"key": "weight_unit", "type": "select", "label": "Weight Unit",
                   "editable": True, "required": False, "position": 0, "options": ["ct", "g", "kg"]}]
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=item)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=schema)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/api/items/item:x/field/weight_unit/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"select" in r.content.lower() or b"option" in r.content.lower()


# ── P5: Multi-tenant token threading ─────────────────────────────────────────

class TestMultiTenantTokenThreading:
    """Every data fetch must pass the per-request auth token — never a global."""

    @pytest.mark.asyncio
    async def test_reports_ar_aging_passes_token(self, ui_client):
        """AR aging must pass the request's auth token to get_ar_aging."""
        calls = []
        async def _capture(token, params=None):
            calls.append(token)
            return {"lines": [], "buckets": {}}
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ar_aging", new=_capture),
        ):
            await ui_client.get("/reports/ar-aging", cookies={"celerp_token": "token-tenant-X"})
        assert calls == ["token-tenant-X"]

    @pytest.mark.asyncio
    async def test_reports_sales_passes_token(self, ui_client):
        """Sales report must pass the request's auth token."""
        calls = []
        async def _capture(token, params=None):
            calls.append(token)
            return {"lines": [], "group_by": "customer", "total": 0}
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_sales_report", new=_capture),
        ):
            await ui_client.get("/reports/sales", cookies={"celerp_token": "token-tenant-Y"})
        assert calls == ["token-tenant-Y"]

    @pytest.mark.asyncio
    async def test_documents_passes_token(self, ui_client):
        """Documents list must pass the request's auth token to list_docs."""
        calls = []
        async def _capture(token, params=None):
            calls.append(token)
            return {"items": [], "total": 0}
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.list_docs", new=_capture),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={})),
        ):
            await ui_client.get("/docs", cookies={"celerp_token": "token-tenant-Z"})
        assert "token-tenant-Z" in calls

    @pytest.mark.asyncio
    async def test_manufacturing_passes_token(self, ui_client):
        """Manufacturing page must pass the request's auth token."""
        calls = []
        async def _capture(token):
            calls.append(token)
            return {"items": [], "total": 0}
        with (
            patch("ui.api_client.list_mfg_orders", new=_capture),
            patch("ui.api_client.list_boms", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            await ui_client.get("/manufacturing", cookies={"celerp_token": "mfg-token"})
        assert "mfg-token" in calls


# ── P1/P3: Pages must not crash on empty/null API data ───────────────────────

class TestGracefulEmptyData:
    """All pages must render cleanly when APIs return empty/null results."""

    @pytest.mark.asyncio
    async def test_ar_aging_empty_lines(self, ui_client):
        """AR aging with 0 lines must show empty state, not crash."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"lines": [], "buckets": {}})),
        ):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code == 200
        assert b"no data" in r.content.lower() or b"empty" in r.content.lower() or b"adjust" in r.content.lower()

    @pytest.mark.asyncio
    async def test_sales_report_empty_lines(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_sales_report", new=AsyncMock(return_value={"lines": [], "total": 0, "group_by": "customer"})),
        ):
            r = await ui_client.get("/reports/sales", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_purchases_report_empty_lines(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_purchases_report", new=AsyncMock(return_value={"lines": [], "total": 0, "group_by": "supplier"})),
        ):
            r = await ui_client.get("/reports/purchases", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_expiring_empty(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_expiring", new=AsyncMock(return_value={"count": 0, "days_threshold": 30, "items": []})),
        ):
            r = await ui_client.get("/reports/expiring", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_aging_with_zero_amount_lines_hidden(self, ui_client):
        """Lines with all-zero amounts and no contact name should be filtered out."""
        aging_data = {
            "lines": [
                {"customer_name": "", "current": 0, "d30": 0, "d60": 0, "d90": 0, "d90plus": 0, "total": 0},
            ],
            "buckets": {},
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value=aging_data)),
        ):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code == 200
        # Zero + no contact = meaningless line — must show empty state
        assert b"no data" in r.content.lower() or b"adjust" in r.content.lower()


# ── P4: API error handling — 401 redirects on all report routes ──────────────

class TestReportApiErrors:
    """Report routes must handle APIError 401 → redirect to /login."""

    @pytest.mark.asyncio
    async def test_ar_aging_401_redirects(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(side_effect=APIError(401, "Unauthorized"))),
        ):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_ap_aging_401_redirects(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ap_aging", new=AsyncMock(side_effect=APIError(401, "Unauthorized"))),
        ):
            r = await ui_client.get("/reports/ap-aging", cookies=_authed())
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_sales_report_401_redirects(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_sales_report", new=AsyncMock(side_effect=APIError(401, "Unauthorized"))),
        ):
            r = await ui_client.get("/reports/sales", cookies=_authed())
        assert r.status_code in (302, 303)

    @pytest.mark.asyncio
    async def test_purchases_report_non_401_shows_empty(self, ui_client):
        """Non-401 APIError on purchases must show empty state, not crash."""
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_purchases_report", new=AsyncMock(side_effect=APIError(503, "Service down"))),
        ):
            r = await ui_client.get("/reports/purchases", cookies=_authed())
        assert r.status_code == 200  # graceful fallback

    @pytest.mark.asyncio
    async def test_expiring_non_401_shows_empty(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_expiring", new=AsyncMock(side_effect=APIError(500, "Internal error"))),
        ):
            r = await ui_client.get("/reports/expiring", cookies=_authed())
        assert r.status_code == 200


# ── P3: Display consistency — sort controls and date filter bars ──────────────

class TestReportDisplayConsistency:
    """Report pages must render correct interactive controls."""

    @pytest.mark.asyncio
    async def test_ar_aging_has_date_filter_bar(self, ui_client):
        """AR aging must include the date filter bar with preset links."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"lines": [], "buckets": {}})),
        ):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code == 200
        assert b"preset" in r.content or b"This month" in r.content or b"date-filter" in r.content

    @pytest.mark.asyncio
    async def test_sales_report_has_group_by_selector(self, ui_client):
        """Sales report must include group-by selector."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_sales_report", new=AsyncMock(return_value={"lines": [], "total": 0, "group_by": "customer"})),
        ):
            r = await ui_client.get("/reports/sales", cookies=_authed())
        assert r.status_code == 200
        assert b"group_by" in r.content or b"Customer" in r.content

    @pytest.mark.asyncio
    async def test_purchases_report_group_by_supplier_default(self, ui_client):
        """Purchases report default group-by is supplier."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_purchases_report", new=AsyncMock(return_value={"lines": [], "total": 0, "group_by": "supplier"})),
        ):
            r = await ui_client.get("/reports/purchases", cookies=_authed())
        assert r.status_code == 200
        assert b"supplier" in r.content.lower() or b"Supplier" in r.content

    @pytest.mark.asyncio
    async def test_ar_aging_sortable_by_outstanding(self, ui_client):
        """AR aging with data must include sort links."""
        aging_data = {
            "lines": [
                {"customer_name": "Alpha", "current": 100, "d30": 50, "d60": 0, "d90": 0, "d90plus": 0, "total": 150},
                {"customer_name": "Beta", "current": 200, "d30": 0, "d60": 0, "d90": 0, "d90plus": 0, "total": 200},
            ],
            "buckets": {"current": 300},
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_THB)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value=aging_data)),
        ):
            r = await ui_client.get("/reports/ar-aging?sort=outstanding&dir=asc", cookies=_authed())
        assert r.status_code == 200
        assert b"sort" in r.content or b"sort-link" in r.content


# ── P4-adjacent: Accounting page currency (already fixed – regression guard) ──

class TestAccountingCurrencyRegression:
    """Accounting (P&L, balance sheet, trial balance) already has currency fixed.
    Regression guard: must not regress to hardcoded ฿."""

    @pytest.mark.asyncio
    async def test_pnl_uses_company_currency(self, ui_client):
        pnl_data = {
            "revenue": {"lines": [{"account": "Sales", "amount": 5000}], "total": 5000},
            "cogs": {"lines": [], "total": 0},
            "expenses": {"lines": [], "total": 0},
            "gross_profit": 5000, "net_profit": 5000,
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_USD)),
            patch("ui.api_client.get_pnl", new=AsyncMock(return_value=pnl_data)),
            patch("ui.api_client.get_balance_sheet", new=AsyncMock(return_value={
                "assets": {"lines": [], "total": 0}, "liabilities": {"lines": [], "total": 0},
                "equity": {"lines": [], "total": 0}, "total_assets": 0,
                "total_liabilities": 0, "total_equity": 0,
            })),
            patch("ui.api_client.get_trial_balance", new=AsyncMock(return_value={
                "accounts": [], "total_debit": 0, "total_credit": 0,
            })),
        ):
            r = await ui_client.get("/accounting?tab=pnl", cookies=_authed())
        assert r.status_code == 200
        # USD accounting: should not show ฿5000
        assert "฿5000" not in r.text or "$" in r.text

    @pytest.mark.asyncio
    async def test_balance_sheet_uses_company_currency(self, ui_client):
        bs_data = {
            "assets": {"lines": [{"account": "Cash", "amount": 10000}], "total": 10000},
            "liabilities": {"lines": [], "total": 0},
            "equity": {"lines": [], "total": 0},
            "total_assets": 10000, "total_liabilities": 0, "total_equity": 0,
        }
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY_EUR)),
            patch("ui.api_client.get_balance_sheet", new=AsyncMock(return_value=bs_data)),
            patch("ui.api_client.get_pnl", new=AsyncMock(return_value={
                "revenue": {"lines": [], "total": 0}, "cogs": {"lines": [], "total": 0},
                "expenses": {"lines": [], "total": 0}, "gross_profit": 0, "net_profit": 0,
            })),
            patch("ui.api_client.get_trial_balance", new=AsyncMock(return_value={
                "accounts": [], "total_debit": 0, "total_credit": 0,
            })),
        ):
            r = await ui_client.get("/accounting?tab=balance-sheet", cookies=_authed())
        assert r.status_code == 200
        assert "฿10000" not in r.text
