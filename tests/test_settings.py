# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for Phase 6 settings restructure.

Covers:
  - /settings redirect (to /settings/general)
  - /settings/general renders (company tab)
  - /settings/sales renders (taxes tab)
  - /settings/inventory renders (locations tab)
  - Default "Head Office" location seeded on registration
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch, MagicMock

from test_helpers import make_test_token, authed_cookies
from ui.api_client import APIError


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _authed(token: str | None = None, role: str = "owner") -> dict:
    return {"celerp_token": token or make_test_token(role=role)}


_COMPANY = {
    "name": "Test Corp",
    "currency": "USD",
    "timezone": "UTC",
    "fiscal_year_start": "01-01",
    "settings": {},
}
_TAXES = [{"name": "VAT", "rate": 7.0, "tax_type": "sales", "is_default": True, "description": ""}]
_TERMS = [{"name": "Net 30", "days": 30, "description": ""}]
_USERS = [{"id": "u1", "name": "Alice", "email": "alice@example.com", "role": "admin", "is_active": True}]
_MODULES = []


def _common_mocks():
    return (
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
        patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
        patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
        patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
        patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={})),
        patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
    )


# ── Redirect test ─────────────────────────────────────────────────────────────

class TestSettingsRedirect:

    @pytest.mark.asyncio
    async def test_settings_redirect(self, ui_client):
        """GET /settings returns 302 to /settings/general."""
        r = await ui_client.get("/settings", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/general" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_settings_redirect_tab_taxes(self, ui_client):
        """GET /settings?tab=taxes redirects to /settings/sales?tab=taxes."""
        r = await ui_client.get("/settings?tab=taxes", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/sales?tab=taxes" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_settings_redirect_tab_locations(self, ui_client):
        """GET /settings?tab=locations redirects to /settings/inventory?tab=locations."""
        r = await ui_client.get("/settings?tab=locations", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/inventory?tab=locations" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_settings_redirect_tab_schema(self, ui_client):
        """GET /settings?tab=schema redirects to /settings/inventory?tab=category-library."""
        r = await ui_client.get("/settings?tab=schema", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/inventory?tab=category-library" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_settings_redirect_tab_users(self, ui_client):
        """GET /settings?tab=users redirects to /settings/general?tab=users."""
        r = await ui_client.get("/settings?tab=users", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/general?tab=users" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_settings_redirect_tab_connectors_falls_to_general(self, ui_client):
        """GET /settings?tab=connectors falls back to /settings/general: the sales
        connectors stub is gone, so the legacy tab key is no longer a known target."""
        r = await ui_client.get("/settings?tab=connectors", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/general" in r.headers.get("location", "")


# ── General settings page ─────────────────────────────────────────────────────

class TestSettingsGeneral:

    @pytest.mark.asyncio
    async def test_settings_general_renders(self, ui_client):
        """GET /settings/general?tab=company returns 200."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"Settings" in r.content

    @pytest.mark.asyncio
    async def test_settings_general_users_tab(self, ui_client):
        """GET /settings/general?tab=users returns 200 with user data."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
        ):
            r = await ui_client.get("/settings/general?tab=users", cookies=_authed())
        assert r.status_code == 200
        assert b"Alice" in r.content

    @pytest.mark.asyncio
    async def test_settings_general_has_no_modules_tab(self, ui_client):
        """Modules moved to its own /modules page; the settings tab is gone."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/settings/general", cookies=_authed())
        assert r.status_code == 200
        assert b"?tab=modules" not in r.content

    @pytest.mark.asyncio
    async def test_settings_general_unauthenticated(self, ui_client):
        """GET /settings/general without auth cookie → 302 to /login."""
        r = await ui_client.get("/settings/general")
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")


# ── Sales settings page ───────────────────────────────────────────────────────

class TestSettingsSales:

    @pytest.mark.asyncio
    async def test_settings_sales_renders(self, ui_client):
        """GET /settings/sales?tab=taxes returns 200."""
        with (
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
        ):
            r = await ui_client.get("/settings/sales?tab=taxes", cookies=_authed())
        assert r.status_code == 200
        assert b"VAT" in r.content

    @pytest.mark.asyncio
    async def test_settings_sales_terms_conditions_tab(self, ui_client):
        """GET /settings/sales?tab=terms-conditions returns 200."""
        with (
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
            patch("ui.api_client.get_terms_conditions", new=AsyncMock(return_value=[
                {"name": "Standard Sales Terms", "text": "Goods remain property...", "doc_types": ["invoice"], "is_default": True},
            ])),
        ):
            r = await ui_client.get("/settings/sales?tab=terms-conditions", cookies=_authed())
        assert r.status_code == 200
        assert b"Standard Sales Terms" in r.content

    @pytest.mark.asyncio
    async def test_settings_sales_unauthenticated(self, ui_client):
        """GET /settings/sales without auth cookie → 302 to /login."""
        r = await ui_client.get("/settings/sales")
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")


# ── Inventory settings page ───────────────────────────────────────────────────

class TestSettingsInventory:

    @pytest.mark.asyncio
    async def test_settings_inventory_renders(self, ui_client):
        """GET /settings/inventory?tab=locations returns 200."""
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/settings/inventory?tab=locations", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_settings_inventory_category_library_tab(self, ui_client):
        """GET /settings/inventory?tab=categories returns 200 with Browse Library section."""
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_category_display_names", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/inventory?tab=categories", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_settings_inventory_unauthenticated(self, ui_client):
        """GET /settings/inventory without auth cookie → 302 to /login."""
        r = await ui_client.get("/settings/inventory")
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")


# ── Default location seeding ──────────────────────────────────────────────────

class TestDefaultLocationSeeded:

    @pytest.mark.asyncio
    async def test_default_location_seeded_on_registration(self, client, session):
        """Registration creates a 'Head Office' location as the default."""
        from sqlalchemy import select
        from celerp.models.company import Location, Company

        payload = {
            "company_name": "LocSeedCo",
            "email": f"loctest_{uuid.uuid4().hex[:8]}@example.com",
            "name": "Loc Tester",
            "password": "testpass123",
        }
        r = await client.post("/auth/register", json=payload)
        assert r.status_code == 200, r.text

        # Use the test session (same DB the test client uses)
        company = await session.scalar(
            select(Company).where(Company.name == "LocSeedCo")
        )
        assert company is not None
        loc = await session.scalar(
            select(Location).where(
                Location.company_id == company.id,
                Location.name == "Head Office",
            )
        )
        assert loc is not None, "Head Office location should be seeded on registration"
        assert loc.is_default is True
        assert loc.type == "office"


# ── Company Addresses section tests ──────────────────────────────────────────

class TestCompanyAddressesUI:

    @pytest.mark.asyncio
    async def test_demo_data_section_removed(self, ui_client):
        """Company tab HTML must NOT contain 'Demo Data' or 'Reload Demo Items'."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"Demo Data" not in r.content
        assert b"Reload Demo Items" not in r.content


class TestCategoriesCRUDUI:

    @pytest.mark.asyncio
    async def test_categories_tab_has_add_form(self, ui_client):
        """Tab=categories must render an Add Category form with new_category_name input."""
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_category_display_names", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/inventory?tab=categories", cookies=_authed())
        assert r.status_code == 200
        assert "new_category_name" in r.text

    @pytest.mark.asyncio
    async def test_categories_tab_rows_have_delete_button(self, ui_client):
        """Applied categories table rows must have a delete affordance."""
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={"gems": []})),
            patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={"gems": []})),
            patch("ui.api_client.get_category_display_names", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/inventory?tab=categories", cookies=_authed())
        assert r.status_code == 200
        # Delete affordance: either hx-delete or a delete button targeting /settings/categories/gems
        assert "/settings/categories/gems" in r.text


class TestLocaleKeys:
    def test_new_category_name_in_all_locales(self):
        """settings.new_category_name must exist in all 11 locale files."""
        import json
        import os
        locales_dir = os.path.join(os.path.dirname(__file__), "..", "ui", "locales")
        langs = ["en", "ar", "de", "es", "fr", "id", "it", "ja", "pt", "th", "vi"]
        for lang in langs:
            path = os.path.join(locales_dir, f"{lang}.json")
            with open(path) as f:
                data = json.load(f)
            assert "settings.new_category_name" in data, f"Missing settings.new_category_name in {lang}.json"


# ── Issue 2: protected price list deletion ────────────────────────────────────

class TestProtectedPriceListDeletion:
    @pytest.mark.asyncio
    async def test_cannot_delete_retail(self, ui_client):
        """DELETE /settings/price-lists/<idx> for Retail must return error fragment."""
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Cost"}, {"name": "Wholesale"}, {"name": "Retail"},
            ])),
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
            patch("ui.api_client.patch_price_lists", new=AsyncMock(return_value=None)),
        ):
            resp = await ui_client.delete("/settings/price-lists/2", cookies=_authed())
        assert resp.status_code == 200
        assert "celerpToast" in resp.headers.get("HX-Trigger", "")

    @pytest.mark.asyncio
    async def test_cannot_delete_cost(self, ui_client):
        """DELETE /settings/price-lists/<idx> for Cost must return error fragment."""
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Cost"}, {"name": "Wholesale"}, {"name": "Retail"},
            ])),
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
            patch("ui.api_client.patch_price_lists", new=AsyncMock(return_value=None)),
        ):
            resp = await ui_client.delete("/settings/price-lists/0", cookies=_authed())
        assert resp.status_code == 200
        assert "celerpToast" in resp.headers.get("HX-Trigger", "")

    @pytest.mark.asyncio
    async def test_cannot_delete_wholesale(self, ui_client):
        """DELETE /settings/price-lists/<idx> for Wholesale must return error fragment."""
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Cost"}, {"name": "Wholesale"}, {"name": "Retail"},
            ])),
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
            patch("ui.api_client.patch_price_lists", new=AsyncMock(return_value=None)),
        ):
            resp = await ui_client.delete("/settings/price-lists/1", cookies=_authed())
        assert resp.status_code == 200
        assert "celerpToast" in resp.headers.get("HX-Trigger", "")

    @pytest.mark.asyncio
    async def test_can_delete_custom_price_list(self, ui_client):
        """DELETE /settings/price-lists/<idx> for a user-added list must succeed."""
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Cost"}, {"name": "Wholesale"}, {"name": "Retail"}, {"name": "VIP"},
            ])),
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
            patch("ui.api_client.get_base_price_list", new=AsyncMock(return_value="Retail")),
            patch("ui.api_client.patch_price_lists", new=AsyncMock(return_value=None)),
        ):
            resp = await ui_client.delete("/settings/price-lists/3", cookies=_authed())
        # Should redirect (204) or succeed without an error fragment
        assert resp.status_code in (200, 204)
        if resp.status_code == 200:
            assert "flash--error" not in resp.text

    @pytest.mark.asyncio
    async def test_cannot_delete_base_price_list(self, ui_client):
        """DELETE /settings/price-lists/<idx> for the base price list must return error fragment."""
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Cost"}, {"name": "Wholesale"}, {"name": "Retail"}, {"name": "VIP"},
            ])),
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
            patch("ui.api_client.get_base_price_list", new=AsyncMock(return_value="VIP")),
            patch("ui.api_client.patch_price_lists", new=AsyncMock(return_value=None)),
        ):
            resp = await ui_client.delete("/settings/price-lists/3", cookies=_authed())
        assert resp.status_code == 200
        assert "celerpToast" in resp.headers.get("HX-Trigger", "")
        assert "base price list" in resp.headers.get("HX-Trigger", "")

# ── Restore journey: restart continuation + post-restart notice ───────────────

class TestRestoreJourneyContinuation:
    @pytest.mark.asyncio
    async def test_restart_app_route_restarts_and_polls(self, ui_client):
        """The Restart Now continuation triggers the graceful restart and swaps to a
        status that reloads the page once the server is back."""
        with patch("ui.api_client.restart_system", new=AsyncMock(return_value={"ok": True})) as mock_restart:
            r = await ui_client.post("/backup/restart-app", cookies=_authed())
        assert r.status_code == 200
        assert mock_restart.await_count == 1
        html = r.text
        assert "Restarting" in html
        assert "setInterval" in html  # self-recovers; no dead end

    @pytest.mark.asyncio
    async def test_restart_app_route_surfaces_api_error(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.restart_system", new=AsyncMock(side_effect=APIError(403, "Admin role required"))):
            r = await ui_client.post("/backup/restart-app", cookies=_authed())
        assert r.status_code == 200
        assert "Admin role required" in r.text

    @pytest.mark.asyncio
    async def test_login_shows_restore_notice_once(self, ui_client, monkeypatch, tmp_path):
        """The one-shot notice survives the post-restore restart: first login render
        shows it (unmissable), then it is gone - no permanent banner."""
        import json as _json
        from celerp.config import settings
        from celerp.services.backup_import import RESTORE_NOTICE_FILE

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        (tmp_path / RESTORE_NOTICE_FILE).write_text(_json.dumps({
            "company_name": "Acme",
            "warnings": ["celerp-fictional"],
            "schema_warning": None,
            "restart_scheduled": True,
        }))
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
            r = await ui_client.get("/login")
            assert r.status_code == 200
            assert "Backup from Acme restored" in r.text
            assert "celerp-fictional" in r.text
            assert not (tmp_path / RESTORE_NOTICE_FILE).exists()  # consumed

            r2 = await ui_client.get("/login")
            assert "Backup from Acme restored" not in r2.text  # one-shot, not perpetual


@pytest.mark.asyncio
async def test_sales_tab_connectors_falls_back_to_taxes(ui_client):
    """GET /settings/sales?tab=connectors renders the taxes tab: the coming-soon
    connectors stub is deleted, so the unknown tab key falls back to the default."""
    with (
        patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
        patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
        patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
    ):
        r = await ui_client.get("/settings/sales?tab=connectors", cookies=_authed())
    assert r.status_code == 200
    assert b"VAT" in r.content
    assert b"Coming soon" not in r.content


@pytest.mark.asyncio
async def test_settings_inline_save_persists_auto_complete(ui_client):
    """Option A: POST /settings/manufacturing saves inline - a 200 with the saved indicator,
    not a 303 reload - and passes auto_complete_work_orders through to the client."""
    saved = AsyncMock(return_value={})
    with patch("ui.api_client.update_mfg_settings", new=saved):
        r = await ui_client.post(
            "/settings/manufacturing",
            data={"auto_complete_work_orders": "1", "auto_create_work_orders": "1", "hours_per_day": "8"},
            cookies=_authed(),
        )
    assert r.status_code == 200
    assert "Settings saved." in r.text
    saved.assert_awaited_once()
    payload = saved.await_args.args[1]
    assert payload["auto_complete_work_orders"] is True


# ── Reorder digest upsell nudge ───────────────────────────────────────────────

class TestReorderDigestUpsell:
    """POST /settings/inventory/reorder: a non-paid user who flips the low-stock
    digest off->on saves the setting and is nudged toward Connect delivery; a
    paid user, a re-check (already on), or a relay-status failure gets no nudge."""

    _URL = "/settings/inventory/reorder"

    def _post_data(self) -> dict:
        return {
            "reorder_alert_email": "1",
            "reorder_alerts_enabled": "1",
            "inventory_method": "fifo",
        }

    @pytest.mark.asyncio
    async def test_reorder_digest_paid_no_upsell(self, ui_client):
        """A paid user's save consults relay status and redirects saved=1 with no upsell=1."""
        prior = {**_COMPANY, "reorder_alert_email": False}
        relay = AsyncMock(return_value={"connected": True, "tier": "cloud"})
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=prior)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as patch_co,
            patch("ui.api_client.get_relay_status", new=relay),
        ):
            r = await ui_client.post(self._URL, data=self._post_data(), cookies=_authed())
        assert r.status_code == 303
        loc = r.headers.get("location", "")
        assert "tab=reorder" in loc and "saved=1" in loc
        assert "upsell=1" not in loc
        relay.assert_awaited()  # paid branch requires the relay status to be consulted
        assert patch_co.await_args.args[1]["reorder_alert_email"] is True

    @pytest.mark.asyncio
    async def test_reorder_digest_recheck_no_modal(self, ui_client):
        """A non-paid save that leaves the digest already-on emits no upsell=1 (transition-only)."""
        prior = {**_COMPANY, "reorder_alert_email": True}
        relay = AsyncMock(return_value={"connected": False, "tier": None})
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=prior)),
            patch("ui.api_client.patch_company", new=AsyncMock()),
            patch("ui.api_client.get_relay_status", new=relay),
        ):
            r = await ui_client.post(self._URL, data=self._post_data(), cookies=_authed())
        assert r.status_code == 303
        loc = r.headers.get("location", "")
        assert "saved=1" in loc
        assert "upsell=1" not in loc
        relay.assert_awaited()  # status is still consulted, but no off->on flip means no nudge

    @pytest.mark.asyncio
    async def test_reorder_digest_relay_error_skips_modal(self, ui_client):
        """When relay status errors on the off->on save, the setting still saves and no upsell=1."""
        prior = {**_COMPANY, "reorder_alert_email": False}
        relay = AsyncMock(side_effect=APIError(503, "relay down"))
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=prior)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as patch_co,
            patch("ui.api_client.get_relay_status", new=relay),
        ):
            r = await ui_client.post(self._URL, data=self._post_data(), cookies=_authed())
        assert r.status_code == 303
        loc = r.headers.get("location", "")
        assert "saved=1" in loc
        assert "upsell=1" not in loc
        relay.assert_awaited()  # the read was attempted, then swallowed
        assert patch_co.await_args.args[1]["reorder_alert_email"] is True  # saved as submitted
