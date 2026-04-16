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

from tests.conftest import make_test_token, authed_cookies


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
    async def test_settings_general_modules_tab(self, ui_client):
        """GET /settings/general?tab=modules returns 200."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
        ):
            r = await ui_client.get("/settings/general?tab=modules", cookies=_authed())
        assert r.status_code == 200

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
        ):
            r = await ui_client.get("/settings/inventory?tab=locations", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_settings_inventory_category_library_tab(self, ui_client):
        """GET /settings/inventory?tab=category-library returns 200."""
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/inventory?tab=category-library", cookies=_authed())
        assert r.status_code == 200
        assert b"Category Library" in r.content

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

    @pytest.mark.asyncio
    async def test_company_addresses_section_present(self, ui_client):
        """Company tab HTML must contain company-addresses-section."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=_MODULES)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"company-addresses-section" in r.content

    @pytest.mark.asyncio
    async def test_company_address_add(self, ui_client):
        """POST /settings/company/addresses creates a location and returns the section."""
        created_loc = {"id": "loc-123", "name": "New Address", "type": "address", "address": {}, "is_default": False}
        with (
            patch("ui.api_client.create_location", new=AsyncMock(return_value=created_loc)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [created_loc], "total": 1})),
        ):
            r = await ui_client.post("/settings/company/addresses", cookies=_authed())
        assert r.status_code == 200
        assert b"company-addresses-section" in r.content

    @pytest.mark.asyncio
    async def test_company_address_set_default(self, ui_client):
        """POST /settings/company/addresses/{id}/set-default patches is_default."""
        patched_loc = {"id": "loc-123", "name": "Head Office", "type": "address", "address": {}, "is_default": True}
        patch_mock = AsyncMock(return_value=patched_loc)
        with (
            patch("ui.api_client.patch_location", new=patch_mock),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [patched_loc], "total": 1})),
        ):
            r = await ui_client.post("/settings/company/addresses/loc-123/set-default", cookies=_authed())
        assert r.status_code == 200
        assert b"company-addresses-section" in r.content
        # Verify patch_location was called with is_default=True
        patch_mock.assert_called_once()
        call_args = patch_mock.call_args
        assert call_args[0][1] == "loc-123"
        assert call_args[0][2].get("is_default") is True
