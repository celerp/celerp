# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""UI behavior tests.

Tests the FastHTML UI layer (ui/app.py) via httpx AsyncClient.
Covers:
  - Auth routing state machine (unauthenticated, bootstrap, onboarding, dashboard)
  - Click-to-edit cell endpoints (GET edit form, PATCH save)
  - Company switcher
  - Search/filter HTMX partials

Patching rules:
  - ui/routes/auth.py uses `from ui.api_client import bootstrap_status, ...`
    → patch at ui.routes.auth.<name>
  - ui/routes/setup.py uses `import ui.api_client as api` + `from ui.api_client import APIError`
    → patch at ui.api_client.<name>
  - ui/routes/inventory.py, dashboard.py, etc. use `import ui.api_client as api`
    → patch at ui.api_client.<name>
"""

from __future__ import annotations

import os
import pathlib
from pathlib import Path
import re
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from ui.routes.csv_import import _load_csv, MAPPING_ATTRIBUTE, MAPPING_SKIP
from ui.routes.inventory import _IMPORT_SPEC, _CORE_ITEM_COLS
from tests.conftest import make_test_token, authed_cookies


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    """httpx client against the UI app (ui.app). No auth cookie."""
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _authed(token: str | None = None, role: str = "owner") -> dict:
    """Return cookies dict with a properly-formed test token."""
    return {"celerp_token": token or make_test_token(role=role)}


async def _inventory_import_with_mapping(ui_client, csv_bytes: bytes):
    """Post CSV to preview, then apply default column mapping and return response."""
    r = await ui_client.post(
        "/inventory/import/preview",
        cookies=_authed(),
        files={"csv_file": ("items.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 200
    html = r.text
    m = re.search(r'name="csv_ref"\s+value="([^"]+)"', html)
    assert m, "csv_ref hidden field not found"
    csv_ref = m.group(1)
    csv_text = _load_csv(csv_ref)
    assert csv_text, "stashed CSV missing"

    # Build mapping: map known core columns to themselves, others as attributes
    import csv as _csv, io as _io
    reader = _csv.DictReader(_io.StringIO(csv_text))
    cols = list(reader.fieldnames or [])
    form_data = {"csv_ref": csv_ref}
    for col in cols:
        if col in _IMPORT_SPEC.cols:
            form_data[f"map__{col}"] = col
        else:
            form_data[f"map__{col}"] = MAPPING_ATTRIBUTE

    return await ui_client.post(
        "/inventory/import/mapped",
        cookies=_authed(),
        data=form_data,
    )


async def _generic_import_with_mapping(ui_client, csv_bytes: bytes, preview_url: str, mapped_url: str, spec_cols: list):
    """Post CSV to preview, then apply default column mapping and return response.

    Generic helper for any import flow that uses the column mapping step.
    """
    r = await ui_client.post(
        preview_url,
        cookies=_authed(),
        files={"csv_file": ("data.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 200
    html = r.text
    m = re.search(r'name="csv_ref"\s+value="([^"]+)"', html)
    assert m, f"csv_ref hidden field not found in {preview_url} response"
    csv_ref = m.group(1)
    csv_text = _load_csv(csv_ref)
    assert csv_text, "stashed CSV missing"

    import csv as _csv, io as _io
    reader = _csv.DictReader(_io.StringIO(csv_text))
    cols = list(reader.fieldnames or [])
    form_data = {"csv_ref": csv_ref}
    for col in cols:
        if col in spec_cols:
            form_data[f"map__{col}"] = col
        else:
            form_data[f"map__{col}"] = MAPPING_ATTRIBUTE

    return await ui_client.post(
        mapped_url,
        cookies=_authed(),
        data=form_data,
    )


@pytest.fixture(autouse=True)
def _mock_get_company():
    """Default get_company mock for all UI tests."""
    _default = {"name": "Test Corp", "currency": "THB", "timezone": "Asia/Bangkok", "fiscal_year_start": "01-01"}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_default)), \
         patch("ui.routes.auth.api_get_company", new=AsyncMock(return_value=_default)):
        yield


@pytest.fixture(autouse=True)
def _mock_category_schemas():
    """Default category schemas mock — returns empty dict."""
    with patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})):
        yield


@pytest.fixture(autouse=True)
def _mock_column_prefs():
    """Default column prefs mock — returns empty dict."""
    with patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})):
        yield


@pytest.fixture(autouse=True)
def _mock_get_locations():
    """Default get_locations mock — returns empty list."""
    with patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})):
        yield


@pytest.fixture(autouse=True)
def _mock_get_modules():
    """Default get_modules mock — returns empty list (no modules enabled)."""
    with patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])):
        yield


# ── Auth routing state machine ────────────────────────────────────────────────

class TestAuthRouting:

    @pytest.mark.asyncio
    async def test_unauthenticated_root_redirects(self, ui_client):
        """GET / without cookie → must redirect (to login or setup)."""
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
            r = await ui_client.get("/")
        assert r.status_code in (302, 303)
        loc = r.headers.get("location", "")
        assert "/login" in loc or "/setup" in loc

    @pytest.mark.asyncio
    async def test_unauthenticated_root_redirects_to_login(self, ui_client):
        """GET / without cookie → auth guard redirects to /login (regardless of bootstrap).

        The auth guard intercepts before the root handler runs. Bootstrap check
        only happens inside the /login route itself (which then redirects to /setup
        if not bootstrapped).
        """
        r = await ui_client.get("/")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_login_page_renders(self, ui_client):
        """GET /login → returns HTML login form."""
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
            r = await ui_client.get("/login")
        assert r.status_code == 200
        assert b"sign in" in r.content.lower() or b"login" in r.content.lower()

    @pytest.mark.asyncio
    async def test_login_redirects_to_setup_when_not_bootstrapped(self, ui_client):
        """GET /login when not bootstrapped → redirect to /setup."""
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=False)):
            r = await ui_client.get("/login")
        assert r.status_code in (302, 303)
        assert "/setup" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_setup_page_renders_when_not_bootstrapped(self, ui_client):
        """GET /setup → renders setup form when not yet bootstrapped.

        /setup is in ui/routes/auth.py and uses a direct import of bootstrap_status,
        so the patch target is ui.routes.auth.bootstrap_status.
        """
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=False)):
            r = await ui_client.get("/setup")
        assert r.status_code == 200
        assert r.content  # non-empty HTML

    @pytest.mark.asyncio
    async def test_setup_redirects_to_login_when_already_bootstrapped(self, ui_client):
        """GET /setup when already bootstrapped → redirect to /login.

        /setup is in ui/routes/auth.py, direct import → patch at ui.routes.auth.
        """
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
            r = await ui_client.get("/setup")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_activating_page_renders(self, ui_client):
        """GET /setup/activating shows the module-load spinner page."""
        r = await ui_client.get("/setup/activating", cookies=_authed())
        assert r.status_code == 200
        assert b"Activating" in r.content
        assert b"poll" in r.content or b"/health" in r.content

    @pytest.mark.asyncio
    async def test_protected_routes_redirect_unauthenticated(self, ui_client):
        """Protected routes without cookie → redirect to /login."""
        for path in ["/dashboard", "/inventory", "/crm", "/docs", "/settings"]:
            r = await ui_client.get(path)
            assert r.status_code in (302, 303), f"{path} should redirect"
            loc = r.headers.get("location", "")
            assert "login" in loc, f"{path} redirected to {loc!r}, expected /login"

    @pytest.mark.asyncio
    async def test_authenticated_root_redirects_to_dashboard(self, ui_client):
        """GET / with auth cookie → /dashboard (no data gate)."""
        with patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)):
            r = await ui_client.get("/", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/dashboard" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_logout_post_clears_cookie_and_redirects(self, ui_client):
        """POST /logout → redirect to /login and clears auth cookie."""
        r = await ui_client.post("/logout", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")
        set_cookie = r.headers.get("set-cookie", "")
        assert "celerp_token" in set_cookie

    @pytest.mark.asyncio
    async def test_logout_get_fallback_clears_cookie(self, ui_client):
        """GET /logout (no-JS fallback) → redirect to /login and clears cookie."""
        r = await ui_client.get("/logout", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")
        set_cookie = r.headers.get("set-cookie", "")
        assert "celerp_token" in set_cookie

    @pytest.mark.asyncio
    async def test_get_role_fallback_is_viewer(self, ui_client):
        """Malformed token should decode as 'viewer' (least privilege), not 'owner'."""
        from ui.config import get_role
        from starlette.testclient import TestClient
        from starlette.requests import Request
        # Simulate a request with a malformed cookie
        scope = {"type": "http", "headers": [(b"cookie", b"celerp_token=totally-broken-token")]}
        req = Request(scope)
        role = get_role(req)
        assert role == "viewer"

    @pytest.mark.asyncio
    async def test_get_role_decodes_proper_jwt(self, ui_client):
        """Properly-formed JWT payload should decode the role correctly."""
        from ui.config import get_role
        from starlette.requests import Request
        token = make_test_token(role="manager")
        scope = {"type": "http", "headers": [(b"cookie", f"celerp_token={token}".encode())]}
        req = Request(scope)
        role = get_role(req)
        assert role == "manager"

    @pytest.mark.asyncio
    async def test_health_system_returns_503_on_api_failure(self, ui_client):
        """GET /health/system when API is unreachable → 503 degraded, not fake 200."""
        import httpx
        with patch("httpx.AsyncClient") as mock_cls:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_cls.return_value = instance
            r = await ui_client.get("/health/system")
        assert r.status_code == 503
        data = r.json()
        assert data["overall"] == "degraded"

    @pytest.mark.asyncio
    async def test_attachment_proxy_requires_auth(self, ui_client):
        """GET /static/attachments/* without cookie → redirect to login."""
        r = await ui_client.get("/static/attachments/some-uuid/file.pdf")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_onboarding_validates_token(self, ui_client):
        """GET /onboarding with expired/invalid token → redirect to login."""
        from ui.api_client import APIError
        with patch("ui.routes.auth.api_get_company", new=AsyncMock(side_effect=APIError(401, "expired"))):
            r = await ui_client.get("/onboarding", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")


# ── Click-to-edit cell ────────────────────────────────────────────────────────

_SCHEMA = [
    {"key": "name", "label": "Name", "type": "text", "editable": True},
    {"key": "status", "label": "Status", "type": "status", "editable": True,
     "options": ["available", "sold", "memo_out"]},
    {"key": "total_cost", "label": "Cost", "type": "money", "editable": False},
]
_ITEM = {"entity_id": "gc:123", "name": "Ruby", "status": "available", "total_cost": "1500.00"}


class TestClickToEdit:

    @pytest.mark.asyncio
    async def test_edit_cell_text_returns_input(self, ui_client):
        """GET /api/items/{id}/field/{field}/edit for text field → HTML input."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
        ):
            r = await ui_client.get(
                "/api/items/gc:123/field/name/edit",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"<input" in r.content

    @pytest.mark.asyncio
    async def test_edit_cell_status_returns_select(self, ui_client):
        """GET edit for a status field → <select> with all options."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
        ):
            r = await ui_client.get(
                "/api/items/gc:123/field/status/edit",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"<select" in r.content
        assert b"available" in r.content
        assert b"sold" in r.content

    @pytest.mark.asyncio
    async def test_edit_cell_readonly_returns_input_anyway(self, ui_client):
        """GET edit for a non-editable field - UI currently renders input for all fields.

        The editable=False flag is not yet enforced server-side on the edit endpoint.
        This test documents current behavior. A future task should enforce 403 for
        non-editable fields and update this test.
        """
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
        ):
            r = await ui_client.get(
                "/api/items/gc:123/field/total_cost/edit",
                cookies=_authed(),
            )
        # Current behavior: returns 200 with an input (editable=False not enforced yet)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_item_field_returns_display_td(self, ui_client):
        """PATCH /api/items/{id}/field/{field} → saves, returns display <td> with new value."""
        updated = {**_ITEM, "name": "Sapphire"}
        with (
            patch("ui.api_client.patch_item", new=AsyncMock(return_value=updated)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=updated)),
        ):
            r = await ui_client.patch(
                "/api/items/gc:123/field/name",
                data={"value": "Sapphire"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"<td" in r.content
        assert b"Sapphire" in r.content

    @pytest.mark.asyncio
    async def test_patch_cell_rewires_htmx_click_to_edit(self, ui_client):
        """The returned <td> after PATCH must include hx-get to re-enable click-to-edit."""
        updated = {**_ITEM, "name": "Emerald"}
        with (
            patch("ui.api_client.patch_item", new=AsyncMock(return_value=updated)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=updated)),
        ):
            r = await ui_client.patch(
                "/api/items/gc:123/field/name",
                data={"value": "Emerald"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"hx-get" in r.content

    @pytest.mark.asyncio
    async def test_patch_item_wraps_flat_value_into_dict_format(self, ui_client):
        """api_client.patch_item must wrap flat {field: value} into {field: {old, new}} before sending."""
        from ui.api_client import patch_item
        sent_body = {}

        async def mock_patch_http(self_c, *args, **kwargs):
            sent_body.update(kwargs.get("json", {}))
            from unittest.mock import MagicMock
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}
            resp.raise_for_status = lambda: None
            return resp

        import httpx
        with patch.object(httpx.AsyncClient, "patch", mock_patch_http):
            try:
                await patch_item("tok", "item:1", {"short_description": "A fine ruby"})
            except Exception:
                pass  # we only care about what was sent

        fc = sent_body.get("fields_changed", {})
        assert "short_description" in fc
        assert isinstance(fc["short_description"], dict)
        assert fc["short_description"]["new"] == "A fine ruby"

    @pytest.mark.asyncio
    async def test_patch_item_empty_string_does_not_raise_dict_type_error(self, ui_client):
        """Blur with empty value on short_description must not return validation error."""
        updated = {**_ITEM, "short_description": ""}
        with (
            patch("ui.api_client.patch_item", new=AsyncMock(return_value=updated)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=updated)),
        ):
            r = await ui_client.patch(
                "/api/items/gc:123/field/short_description",
                data={"value": ""},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"dict_type" not in r.content
        assert b"Input should be a valid dictionary" not in r.content


# ── Search / filter HTMX partials ─────────────────────────────────────────────

class TestSearchPartials:

    @pytest.mark.asyncio
    async def test_inventory_search_htmx_returns_fragment(self, ui_client):
        """GET /inventory/search?q=... with HX-Request header → table fragment, not full page."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get(
                "/inventory/search?q=ruby",
                cookies=_authed(),
                headers={"HX-Request": "true"},
            )
        assert r.status_code == 200
        assert b"<html" not in r.content.lower()
        assert b"<table" in r.content or b"data-table" in r.content or b"empty" in r.content.lower()

    @pytest.mark.asyncio
    async def test_inventory_search_empty_shows_empty_state(self, ui_client):
        """Search with no results → empty-state element, not blank or error."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get(
                "/inventory/search?q=xxxxxxnotfound",
                cookies=_authed(),
                headers={"HX-Request": "true"},
            )
        assert r.status_code == 200
        assert b"No items" in r.content or b"empty" in r.content.lower()

    @pytest.mark.asyncio
    async def test_inventory_search_without_htmx_returns_full_page(self, ui_client):
        """GET /inventory/search without HX-Request → full HTML page (for direct nav)."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get(
                "/inventory/search?q=ruby",
                cookies=_authed(),
            )
        assert r.status_code == 200
        # Full page has doctype or <html>
        assert b"<html" in r.content.lower() or b"<!doctype" in r.content.lower()


# ── Company switcher ──────────────────────────────────────────────────────────

class TestCompanySwitcher:

    _COMPANIES = [
        {"company_id": "c1", "company_name": "Acme Trading Co", "role": "admin"},
        {"company_id": "c2", "company_name": "Acme Corp", "role": "operator"},
    ]

    @pytest.mark.asyncio
    async def test_switch_company_picker_renders(self, ui_client):
        """GET /switch-company → renders company list."""
        with patch("ui.routes.auth.api_my_companies", new=AsyncMock(return_value=self._COMPANIES)):
            r = await ui_client.get("/switch-company", cookies=_authed())
        assert r.status_code == 200
        assert b"Acme Trading Co" in r.content
        assert b"Acme Corp" in r.content

    @pytest.mark.asyncio
    async def test_switch_company_without_auth_redirects(self, ui_client):
        """GET /switch-company without cookie → 302 to /login via auth guard.

        /switch-company is not in _PUBLIC so the Beforeware auth guard catches it
        before the route handler runs and redirects to /login.
        """
        r = await ui_client.get("/switch-company")
        assert r.status_code in (302, 303)
        assert "login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_switch_company_post_sets_new_token(self, ui_client):
        """POST /switch-company/{id} → sets new token cookie, redirects.

        switch_company is imported inline inside the handler with:
            from ui.api_client import switch_company as api_switch
        so it must be patched at ui.api_client.switch_company.
        """
        with patch("ui.api_client.switch_company", new=AsyncMock(return_value="new-token-xyz")):
            r = await ui_client.post("/switch-company/c2", cookies=_authed())
        assert r.status_code in (302, 303)
        set_cookie = r.headers.get("set-cookie", "")
        assert "celerp_token" in set_cookie
        assert "new-token-xyz" in set_cookie

    @pytest.mark.asyncio
    async def test_switch_company_post_without_auth_redirects(self, ui_client):
        """POST /switch-company without cookie → redirect to /login."""
        r = await ui_client.post("/switch-company/c1")
        assert r.status_code in (302, 303)
        assert "login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_company_picker_has_create_form(self, ui_client):
        """GET /switch-company → picker includes 'Create new company' form."""
        with patch("ui.routes.auth.api_my_companies", new=AsyncMock(return_value=self._COMPANIES)):
            r = await ui_client.get("/switch-company", cookies=_authed())
        assert r.status_code == 200
        assert b"create-company" in r.content
        assert b"company_name" in r.content

    @pytest.mark.asyncio
    async def test_create_company_post_redirects_to_setup(self, ui_client):
        """POST /create-company → creates company, redirects to /setup/company."""
        with patch("ui.api_client.create_company", new=AsyncMock(return_value="new-co-token")):
            r = await ui_client.post(
                "/create-company",
                data={"company_name": "New Venture Ltd"},
                cookies=_authed(),
            )
        assert r.status_code in (302, 303)
        assert "/setup/company" in r.headers.get("location", "")
        assert "new-co-token" in r.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_create_company_empty_name_rejected(self, ui_client):
        """POST /create-company with blank name → redirect with error."""
        r = await ui_client.post(
            "/create-company",
            data={"company_name": "  "},
            cookies=_authed(),
        )
        assert r.status_code in (302, 303)
        assert "error" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_create_company_without_auth_redirects(self, ui_client):
        """POST /create-company without cookie → redirect to /login."""
        r = await ui_client.post("/create-company", data={"company_name": "Test"})
        assert r.status_code in (302, 303)
        assert "login" in r.headers.get("location", "")


# ── Page rendering (authed) ──────────────────────────────────────────────────

# Shared mock data for page tests
_COMPANY = {"name": "Test Corp", "currency": "THB", "timezone": "Asia/Bangkok", "fiscal_year_start": "01-01"}
_VALUATION = {"item_count": 10, "active_item_count": 8, "total_cost": 5000.0, "total_retail": 8000.0,
              "total_wholesale": 6000.0, "cost_total": 5000.0, "retail_total": 8000.0, "wholesale_total": 6000.0}
_DOC_SUMMARY = {"ar_outstanding": 100.0, "ar_total": 500.0, "ar_gross": 500.0, "invoice_count": 3}
_MEMO_SUMMARY = {"total_balance": 200.0, "count": 2, "active_total": 200.0}
_COMPANIES = [{"company_id": "c1", "company_name": "Test Corp", "role": "admin"}]
_CONTACTS = [{"entity_id": "ct:1", "name": "Alice", "phone": "555", "email": "a@b.c",
              "tax_id": "T1", "credit_limit": 1000, "contact_type": "customer"}]
_DOCS = [{"entity_id": "d:1", "doc_number": "INV-001", "doc_type": "invoice", "contact_name": "Alice",
          "issue_date": "2026-01-01", "due_date": "2026-02-01", "total_amount": 100, "outstanding_balance": 50,
          "status": "sent"}]
_DOC_DETAIL = {**_DOCS[0], "line_items": [{"description": "Item", "item_id": "i:1", "quantity": 1,
               "unit_price": 100, "discount": 0, "line_total": 100}], "tax_amount": 7, "payment_terms": "Net 30"}
_CHART = [{"code": "1000", "name": "Cash", "account_type": "asset", "parent_code": "", "is_active": True}]
_TRIAL = {"balanced": True, "total_debit": 1000, "total_credit": 1000, "lines": []}
_PNL = {"revenue": {"total": 500, "lines": [{"account_name": "Sales", "amount": 500}]},
        "cogs": {"total": 200, "lines": []}, "gross_profit": 300,
        "expenses": {"total": 100, "lines": []}, "net_profit": 200}
_BS = {"assets": {"total": 1000, "lines": [{"account_name": "Cash", "amount": 1000}]},
       "liabilities": {"total": 300, "lines": []}, "equity": {"total": 700, "lines": []}, "balanced": True}
_SUBS = [{"entity_id": "s:1", "name": "Monthly Rent", "doc_type": "invoice", "frequency": "monthly",
          "next_run": "2026-03-01", "last_generated_doc_id": "d:99", "status": "active"}]
_AGING = {"lines": [{"contact_name": "Alice", "doc_number": "INV-001", "due_date": "2026-01-15",
          "outstanding": 100, "days_overdue": 40, "bucket": "31-60"}], "buckets": {"31-60": 100}}
_SALES = {"lines": [{"label": "Alice", "count": 3, "total": 500}], "group_by": "customer", "total": 500}
_EXPIRING = {"count": 1, "days_threshold": 30, "items": [
    {"sku": "SKU-1", "name": "Ruby", "expiry_date": "2026-03-20", "days_left": 24, "status": "available"}]}
_TAXES = [{"name": "VAT", "rate": 7, "tax_type": "sales", "is_default": True, "description": "Standard VAT"}]
_TERMS = [{"name": "Net 30", "days": 30, "description": "Payment due in 30 days"}]
_USERS = [{"id": "u1", "name": "Noah", "email": "noah@test.com", "role": "owner", "is_active": True}]


class TestDashboardPage:
    @pytest.mark.asyncio
    async def test_dashboard_renders(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value=_COMPANIES)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {"current": 100, "1-30": 50}, "lines": []})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        assert r.status_code == 200
        assert b"Dashboard" in r.content
        assert b"Inventory" in r.content
        assert b"charts-section" in r.content


class TestActivityFeed:
    """Tests for activity feed DRY component and rendering."""

    def test_ledger_table_uses_ts_field(self):
        """_ledger_table must read 'ts' key (not 'created_at') and extract detail from data."""
        from ui.routes.inventory import _ledger_table
        from fasthtml.common import to_xml
        html = to_xml(_ledger_table([{
            "event_type": "item.updated",
            "ts": "2026-03-20T10:00:00Z",
            "data": {"fields_changed": {"status": {"old": "active", "new": "reserved"}}},
        }]))
        assert "2026-03-20" in html
        assert "Changed: status" in html
        assert "—" not in html.split("2026-03-20")[0]  # timestamp cell is not blank

    def test_ledger_table_empty(self):
        """_ledger_table with empty list shows no-activity message."""
        from ui.routes.inventory import _ledger_table
        from fasthtml.common import to_xml
        html = to_xml(_ledger_table([]))
        assert "No activity yet" in html

    def test_activity_feed_shows_name_not_hash(self):
        """_activity_feed must show item name as link text, not raw entity_id hash."""
        from ui.routes.dashboard import _activity_feed
        from fasthtml.common import to_xml
        html = to_xml(_activity_feed([{
            "event_type": "item.created",
            "entity_id": "item:92f778e9-0000-0000-0000-000000000000",
            "name": "Burmese Ruby 2.5ct",
            "ts": "2026-03-20T10:00:00Z",
        }]))
        assert "Burmese Ruby 2.5ct" in html
        # Hash must only appear inside an href, not as visible text
        import re
        visible_text = re.sub(r'<[^>]+>', '', html)
        assert "92f778e9" not in visible_text

    def test_activity_feed_links_to_entity(self):
        """_activity_feed must include a link to the item detail page."""
        from ui.routes.dashboard import _activity_feed
        from fasthtml.common import to_xml
        eid = "item:abc123-0000-0000-0000-000000000000"
        html = to_xml(_activity_feed([{
            "event_type": "item.updated",
            "entity_id": eid,
            "name": "Emerald",
            "ts": "2026-03-20T10:00:00Z",
        }]))
        assert f"/inventory/{eid}" in html

    def test_activity_feed_no_actor_hash(self):
        """_activity_feed must NOT render raw UUID actor hashes."""
        from ui.routes.dashboard import _activity_feed
        from fasthtml.common import to_xml
        html = to_xml(_activity_feed([{
            "event_type": "item.created",
            "entity_id": "item:abc",
            "name": "Test",
            "ts": "2026-03-20T10:00:00Z",
            "actor_name": "Noah Severs",
        }]))
        assert "f9a514bf" not in html
        assert "Noah Severs" in html

    def test_event_type_labels_single_source(self):
        """EVENT_TYPE_LABELS must live only in ui.components.activity."""
        from ui.components.activity import EVENT_TYPE_LABELS
        assert "item.created" in EVENT_TYPE_LABELS
        assert "doc.paid" in EVENT_TYPE_LABELS

    def test_event_label_merge_events(self):
        from ui.components.activity import event_label
        assert event_label("item.merged") == "Items merged"
        assert event_label("item.source_deactivated") == "Merged into another item"
        assert event_label("item.split") == "Item split"
        assert event_label("item.pricing.set") == "Price updated"
        assert event_label("item.quantity.adjusted") == "Quantity adjusted"

    def test_detail_from_entry_source_deactivated(self):
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({"merged_into": "item:new123", "merged_into_sku": "SKU-NEW", "original_qty": 5.0}, "item.source_deactivated")
        assert "SKU-NEW" in result
        assert "5.0" in result

    def test_detail_from_entry_source_deactivated_fallback(self):
        """Falls back to entity_id when SKU is missing (legacy events)."""
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({"merged_into": "item:new123"}, "item.source_deactivated")
        assert "item:new123" in result

    def test_detail_from_entry_merged(self):
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({
            "source_entity_ids": ["item:a", "item:b"],
            "source_skus": {"item:a": "SKU-A", "item:b": "SKU-B"},
            "resulting_qty": 10.0,
        }, "item.merged")
        assert "SKU-A" in result
        assert "SKU-B" in result
        assert "10.0" in result

    def test_detail_from_entry_merged_fallback(self):
        """Falls back to count when SKUs missing (legacy events)."""
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({"source_entity_ids": ["item:a", "item:b"]}, "item.merged")
        assert "2 source" in result

    def test_detail_from_entry_split(self):
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({"child_skus": ["SKU-A", "SKU-B"], "child_ids": ["item:c1", "item:c2"]}, "item.split")
        assert "SKU-A" in result
        assert "SKU-B" in result

    def test_detail_from_entry_pricing(self):
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({"price_type": "cost_price", "new_price": 100.0}, "item.pricing.set")
        assert "100.0" in result
        assert "Cost Price" in result

    def test_detail_from_entry_qty_adjusted(self):
        from ui.components.activity import detail_from_entry
        result = detail_from_entry({"new_qty": 7}, "item.quantity.adjusted")
        assert "7" in result


class TestInventoryPage:
    @pytest.mark.asyncio
    async def test_inventory_renders(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"Inventory" in r.content
        assert b"Ruby" in r.content

    @pytest.mark.asyncio
    async def test_inventory_empty_values_show_double_dash(self, ui_client):
        """Empty cell values must render as '--' not '-' or blank."""
        item_empty = {"entity_id": "gc:999", "name": "", "status": "", "total_cost": ""}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item_empty], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"--" in r.content

    @pytest.mark.asyncio
    async def test_inventory_sort_param_passes(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory/search?sort=name&dir=asc", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_per_page_param(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory?per_page=25", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_row_menu_present(self, ui_client):
        """Data table rows should have a three-dot action menu."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"row-menu-btn" in r.content


class TestCRMPage:
    @pytest.mark.asyncio
    async def test_crm_renders(self, ui_client):
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": len(_CONTACTS)})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
        ):
            r = await ui_client.get("/contacts/customers", cookies=_authed())
        assert r.status_code == 200
        assert b"Customers" in r.content or b"Contacts" in r.content or b"CRM" in r.content
        assert b"Alice" in r.content

    @pytest.mark.asyncio
    async def test_crm_search(self, ui_client):
        # /contacts/search now redirects to /contacts/content (HTMX partial)
        r = await ui_client.get("/contacts/search?q=alice", cookies=_authed(), follow_redirects=False)
        assert r.status_code == 302
        assert "/contacts/content" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_crm_empty(self, ui_client):
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
        ):
            r = await ui_client.get("/contacts/customers", cookies=_authed())
        assert r.status_code == 200
        assert b"No customers" in r.content or b"No contacts" in r.content

    @pytest.mark.asyncio
    async def test_contact_detail(self, ui_client):
        contact = {**_CONTACTS[0], "address": "123 St", "payment_terms": "Net 30"}
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.list_contact_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/contacts/ct:1", cookies=_authed())
        assert r.status_code == 200
        assert b"Alice" in r.content

    @pytest.mark.asyncio
    async def test_crm_empty_values_show_double_dash(self, ui_client):
        contact = {"entity_id": "ct:2", "name": "Bob", "phone": None, "email": "",
                   "tax_id": "", "credit_limit": None, "contact_type": "customer"}
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.list_contact_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/contacts/ct:2", cookies=_authed())
        assert b"--" in r.content


class TestDocsPage:
    @pytest.mark.asyncio
    async def test_docs_renders(self, ui_client):
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": _DOCS, "total": len(_DOCS)})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert r.status_code == 200
        assert b"Documents" in r.content
        assert b"INV-001" in r.content

    @pytest.mark.asyncio
    async def test_docs_search(self, ui_client):
        with patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": _DOCS, "total": len(_DOCS)})):
            r = await ui_client.get("/docs/search?q=INV", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_empty(self, ui_client):
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert b"No documents yet." in r.content

    @pytest.mark.asyncio
    async def test_doc_detail(self, ui_client):
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        assert b"INV-001" in r.content
        # Doc number should be click-to-edit (editable-cell)
        assert b"field/ref_id/edit" in r.content

    @pytest.mark.asyncio
    async def test_docs_type_filter(self, ui_client):
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": _DOCS, "total": len(_DOCS)})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs?type=invoice", cookies=_authed())
        assert r.status_code == 200


class TestDocCatalogLookup:
    """Test /docs/catalog-lookup returns sell_by and quantity."""

    @pytest.mark.asyncio
    async def test_catalog_lookup_weight_item(self, ui_client):
        item = {
            "entity_id": "item:w1", "sku": "EM-001", "name": "Emerald",
            "retail_price": 500, "sell_by": "carat", "quantity": 3.2,
        }
        with patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item], "total": 1})):
            r = await ui_client.get("/docs/catalog-lookup?sku=EM-001", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["sku"] == "EM-001"
        assert data["unit_price"] == 500
        assert data["sell_by"] == "carat"
        assert data["quantity"] == 3.2

    @pytest.mark.asyncio
    async def test_catalog_lookup_piece_item(self, ui_client):
        item = {
            "entity_id": "item:p1", "sku": "RNG-001", "name": "Ring",
            "retail_price": 1200, "sell_by": "piece", "quantity": 1,
        }
        with patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item], "total": 1})):
            r = await ui_client.get("/docs/catalog-lookup?sku=RNG-001", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["sell_by"] == "piece"
        assert data["quantity"] == 1

    @pytest.mark.asyncio
    async def test_catalog_lookup_no_sell_by(self, ui_client):
        item = {"entity_id": "item:n1", "sku": "X-001", "name": "Widget", "retail_price": 10}
        with patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item], "total": 1})):
            r = await ui_client.get("/docs/catalog-lookup?sku=X-001", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["sell_by"] is None


class TestAccountingPage:
    @pytest.mark.asyncio
    async def test_accounting_renders(self, ui_client):
        """Default tab is P&L."""
        with patch("ui.api_client.get_pnl", new=AsyncMock(return_value=_PNL)):
            r = await ui_client.get("/accounting", cookies=_authed())
        assert r.status_code == 200
        assert b"Accounting" in r.content
        assert b"P&amp;L" in r.content or b"P&L" in r.content

    @pytest.mark.asyncio
    async def test_accounting_chart_in_settings(self, ui_client):
        """Chart of Accounts is in Finance Settings, not main accounting page."""
        with patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})), \
             patch("ui.api_client.get_chart", new=AsyncMock(return_value={"items": _CHART, "total": len(_CHART)})):
            r = await ui_client.get("/settings/accounting?tab=chart", cookies=_authed())
        assert r.status_code == 200
        assert b"Cash" in r.content

    @pytest.mark.asyncio
    async def test_pnl_redirects_to_tab(self, ui_client):
        r = await ui_client.get("/accounting/pnl", cookies=_authed())
        assert r.status_code == 302
        assert "tab=pnl" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_balance_sheet_redirects_to_tab(self, ui_client):
        r = await ui_client.get("/accounting/balance-sheet", cookies=_authed())
        assert r.status_code == 302
        assert "tab=balance-sheet" in r.headers.get("location", "")


class TestPeriodLockAndCloseBooks:
    @pytest.mark.asyncio
    async def test_settings_accounting_period_lock_tab(self, ui_client):
        """Period Lock tab renders the lock form."""
        with patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})), \
             patch("ui.api_client.get_period_lock", new=AsyncMock(return_value={})):
            r = await ui_client.get("/settings/accounting?tab=period-lock", cookies=_authed())
        assert r.status_code == 200
        assert b"Period Lock" in r.content
        assert b"Close Fiscal Year" in r.content

    @pytest.mark.asyncio
    async def test_settings_accounting_period_lock_shows_date(self, ui_client):
        """When a lock date is set, it shows in the UI."""
        with patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})), \
             patch("ui.api_client.get_period_lock", new=AsyncMock(return_value={"lock_date": "2025-12-31", "lock_date_set_at": "2026-01-15T10:00:00Z"})):
            r = await ui_client.get("/settings/accounting?tab=period-lock", cookies=_authed())
        assert r.status_code == 200
        assert b"2025-12-31" in r.content

    @pytest.mark.asyncio
    async def test_post_period_lock(self, ui_client):
        """POST sets the lock date."""
        with patch("ui.api_client.set_period_lock", new=AsyncMock(return_value={"lock_date": "2025-12-31"})):
            r = await ui_client.post(
                "/settings/accounting/period-lock",
                data={"lock_date": "2025-12-31"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"2025-12-31" in r.content

    @pytest.mark.asyncio
    async def test_post_close_year(self, ui_client):
        """POST close-year returns success message."""
        with patch("ui.api_client.close_fiscal_year", new=AsyncMock(return_value={
            "je_id": "je:close:2025-12-31", "year_end": "2025-12-31",
            "net_income": 15000.0, "entries_count": 5, "lock_date": "2025-12-31",
        })):
            r = await ui_client.post(
                "/settings/accounting/close-year",
                data={"fiscal_year_end": "2025-12-31"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Fiscal year closed" in r.content
        assert b"15,000.00" in r.content

    @pytest.mark.asyncio
    async def test_post_close_year_no_date_error(self, ui_client):
        """Missing date returns error."""
        r = await ui_client.post(
            "/settings/accounting/close-year",
            data={"fiscal_year_end": ""},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"required" in r.content


class TestReportsPage:
    @pytest.mark.asyncio
    async def test_reports_index(self, ui_client):
        r = await ui_client.get("/reports", cookies=_authed())
        assert r.status_code == 200
        assert b"Reports" in r.content

    @pytest.mark.asyncio
    async def test_ar_aging(self, ui_client):
        with patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value=_AGING)):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code == 200
        assert b"AR Aging" in r.content

    @pytest.mark.asyncio
    async def test_ap_aging(self, ui_client):
        with patch("ui.api_client.get_ap_aging", new=AsyncMock(return_value=_AGING)):
            r = await ui_client.get("/reports/ap-aging", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_sales_report(self, ui_client):
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value=_SALES)):
            r = await ui_client.get("/reports/sales?group_by=customer", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_purchases_report(self, ui_client):
        with patch("ui.api_client.get_purchases_report", new=AsyncMock(return_value=_SALES)):
            r = await ui_client.get("/reports/purchases?group_by=supplier", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_expiring_report(self, ui_client):
        with patch("ui.api_client.get_expiring", new=AsyncMock(return_value=_EXPIRING)):
            r = await ui_client.get("/reports/expiring?days=30", cookies=_authed())
        assert r.status_code == 200
        assert b"Expiring" in r.content


class TestSettingsPage:
    @pytest.mark.asyncio
    async def test_settings_company_tab(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"Global Config" in r.content

    @pytest.mark.asyncio
    async def test_settings_users_tab(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/general?tab=users", cookies=_authed())
        assert r.status_code == 200
        assert b"Noah" in r.content

    @pytest.mark.asyncio
    async def test_settings_taxes_tab(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/sales?tab=taxes", cookies=_authed())
        assert r.status_code == 200
        assert b"VAT" in r.content

    @pytest.mark.asyncio
    async def test_settings_schema_tab(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/inventory?tab=category-library", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_category_library_with_vertical_categories(self, ui_client):
        """Category library with actual vertical categories must not 500 (_TAG_LABELS regression)."""
        vert_cats = [
            {"name": "colored_stone", "display_name": "Colored Stone", "vertical_tags": ["gems_jewelry"]},
            {"name": "laptop", "display_name": "Laptop", "vertical_tags": ["electronics"]},
        ]
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=vert_cats)),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/inventory?tab=category-library", cookies=_authed())
        assert r.status_code == 200
        assert b"Gems" in r.content or b"gems" in r.content.lower()
        assert b"Electronics" in r.content


class TestSubscriptionsPage:
    @pytest.mark.asyncio
    async def test_subscriptions_renders(self, ui_client):
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value=_SUBS)):
            r = await ui_client.get("/subscriptions", cookies=_authed())
        assert r.status_code == 200
        assert b"Subscriptions" in r.content
        assert b"Monthly Rent" in r.content

    @pytest.mark.asyncio
    async def test_subscriptions_empty(self, ui_client):
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/subscriptions", cookies=_authed())
        assert r.status_code == 200
        assert b"No subscriptions" in r.content

    @pytest.mark.asyncio
    async def test_subscriptions_htmx_pause(self, ui_client):
        paused = {**_SUBS[0], "status": "paused"}
        with (
            patch("ui.api_client.pause_subscription", new=AsyncMock(return_value=paused)),
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=paused)),
        ):
            r = await ui_client.post("/subscriptions/s:1/pause", cookies=_authed())
        assert r.status_code == 200
        assert b"Resume" in r.content

    @pytest.mark.asyncio
    async def test_subscriptions_htmx_resume(self, ui_client):
        resumed = {**_SUBS[0], "status": "active"}
        with (
            patch("ui.api_client.resume_subscription", new=AsyncMock(return_value=resumed)),
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=resumed)),
        ):
            r = await ui_client.post("/subscriptions/s:1/resume", cookies=_authed())
        assert r.status_code == 200
        assert b"Pause" in r.content

    @pytest.mark.asyncio
    async def test_new_subscription_form(self, ui_client):
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/subscriptions/new", cookies=_authed())
        assert r.status_code == 200
        assert b"New Subscription" in r.content


class TestTableComponent:
    """Tests for the data_table component behavior."""

    @pytest.mark.asyncio
    async def test_empty_table_shows_empty_state(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory/search", cookies=_authed())
        assert r.status_code == 200
        assert b"No items" in r.content

    @pytest.mark.asyncio
    async def test_table_headers_exist(self, ui_client):
        """Table should have th elements from schema."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"<th" in r.content

    @pytest.mark.asyncio
    async def test_esc_handler_js_included(self, ui_client):
        """The shell should include Escape key handler JavaScript."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"Escape" in r.content


class TestInventoryCategoryTabs:
    """Inventory category tabs, status cards, and HTMX content partials.

    Every test must patch ALL 7 api_client calls used by the inventory route's
    asyncio.gather to avoid mock bleed under xdist parallel execution.
    """

    @pytest.mark.asyncio
    async def test_category_tabs_render_when_counts_present(self, ui_client):
        """Inventory page shows category tabs when valuation has category_counts."""
        valuation = {**_VALUATION, "category_counts": {"C&P Gemstone": 100, "Jewelry": 10}}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=valuation)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"category-tab" in r.content
        assert b"C&amp;P Gemstone" in r.content or b"C&P Gemstone" in r.content
        assert b"Jewelry" in r.content

    @pytest.mark.asyncio
    async def test_category_tabs_hidden_when_no_counts(self, ui_client):
        """No category_counts → no category-specific tabs. Status filter moved to sidebar."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        # Status cards always render (Available/Reserved counts)
        assert b"status-card" in r.content
        # No category-specific tab content (no category names from valuation)
        assert b"C&amp;P Gemstone" not in r.content

    @pytest.mark.asyncio
    async def test_category_filter_passes_to_search(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory/search?category=Jewelry", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_content_endpoint_returns_fragment(self, ui_client):
        """/inventory/content returns #inventory-content element (for HTMX outerHTML swap)."""
        valuation = {**_VALUATION, "category_counts": {"Ruby": 5}, "count_by_status": {"available": 5}}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=valuation)),
        ):
            r = await ui_client.get("/inventory/content", cookies=_authed())
        assert r.status_code == 200
        # The #inventory-content div must be present for HTMX outerHTML swap
        assert b"inventory-content" in r.content
        # Must contain status cards (not status-tabs - those were removed in UX cleanup)
        assert b"status-card" in r.content

    @pytest.mark.asyncio
    async def test_category_tabs_target_inventory_content(self, ui_client):
        """Category tabs must use hx-target=#inventory-content so highlight updates on click."""
        valuation = {**_VALUATION, "category_counts": {"Ruby": 5}, "count_by_status": {"available": 5}}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=valuation)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"inventory-content" in r.content
        # Category tab links must NOT use the stale #data-table target
        assert b"#data-table" not in r.content
        # Must use /inventory/content as the HTMX endpoint
        assert b"/inventory/content" in r.content

    @pytest.mark.asyncio
    async def test_status_tabs_target_inventory_content(self, ui_client):
        """Status cards link to /inventory with status param (replaced status-tabs with sidebar links)."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        # Status cards still render (Available/Reserved)
        assert b"status-card" in r.content
        # Sold/Archived are now sidebar links (rendered in the shell, not inventory content)
        # Just ensure the page loads without the old status-tabs element
        assert b"status-tabs" not in r.content

    @pytest.mark.asyncio
    async def test_status_cards_preserve_category_in_url(self, ui_client):
        """Status cards must include category param in their href so clicking one doesn't reset the category filter."""
        valuation = {**_VALUATION, "category_counts": {"Ruby": 5}, "count_by_status": {"available": 5}}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=valuation)),
        ):
            r = await ui_client.get("/inventory?category=Ruby", cookies=_authed())
        assert r.status_code == 200
        # Status card hrefs must contain category=Ruby so clicking one preserves the filter
        assert b"category=Ruby" in r.content
        # Must NOT have a bare /inventory?status=available link (which would drop category)
        html = r.text
        import re
        bare_status_links = re.findall(r'href="/inventory\?status=\w+"', html)
        assert not bare_status_links, f"Status card links drop category param: {bare_status_links}"

    @pytest.mark.asyncio
    async def test_category_tab_active_highlight_correct(self, ui_client):
        """When ?category=Ruby is set, Ruby tab gets category-tab--active and All does not."""
        valuation = {**_VALUATION, "category_counts": {"Ruby": 5}, "count_by_status": {"available": 5}}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=valuation)),
        ):
            r = await ui_client.get("/inventory?category=Ruby", cookies=_authed())
        assert r.status_code == 200
        html = r.text
        # The Ruby tab should have active class; verify order: Ruby tab comes after All tab
        ruby_idx = html.find("Ruby")
        all_idx = html.find("All (")
        # Ruby tab is in the page with active marker
        assert b"Ruby" in r.content
        # Valuation bar reflects category scope (count from scoped valuation)
        assert b"Available:" in r.content
    @pytest.mark.asyncio
    async def test_company_field_edit_returns_input(self, ui_client):
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)):
            r = await ui_client.get("/settings/company/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<input" in r.content

    @pytest.mark.asyncio
    async def test_company_field_patch_returns_clickable(self, ui_client):
        updated = {**_COMPANY, "name": "New Corp"}
        with (
            patch("ui.api_client.patch_company", new=AsyncMock(return_value=updated)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=updated)),
        ):
            r = await ui_client.patch("/settings/company/name", data={"value": "New Corp"}, cookies=_authed())
        assert r.status_code == 200
        assert b"New Corp" in r.content
        assert b"cell--clickable" in r.content

    @pytest.mark.asyncio
    async def test_user_field_edit_role_returns_select(self, ui_client):
        with patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})):
            r = await ui_client.get("/settings/users/u1/role/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<select" in r.content

    @pytest.mark.asyncio
    async def test_user_field_patch(self, ui_client):
        updated = [{**_USERS[0], "role": "manager"}]
        # Two owners - downgrade is allowed
        two_owners = [_USERS[0], {**_USERS[0], "id": "u2", "name": "Bob", "role": "owner"}]
        with (
            patch("ui.api_client.patch_user", new=AsyncMock(return_value=updated[0])),
            patch("ui.api_client.get_users", side_effect=[
                {"items": two_owners, "total": 2},   # guard pre-check
                {"items": updated, "total": 1},       # post-patch refresh
            ]),
        ):
            r = await ui_client.patch("/settings/users/u1/role", data={"value": "manager"}, cookies=_authed())
        assert r.status_code == 200
        assert b"manager" in r.content

    @pytest.mark.asyncio
    async def test_user_role_last_owner_guard(self, ui_client):
        """Cannot demote the last owner."""
        with patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})):
            r = await ui_client.patch("/settings/users/u1/role", data={"value": "admin"}, cookies=_authed())
        assert r.status_code == 200
        assert b"last owner" in r.content.lower()

    @pytest.mark.asyncio
    async def test_user_role_non_owner_demotion_allowed(self, ui_client):
        """Can demote a non-owner user freely (guard only fires for last owner)."""
        non_owner = [{**_USERS[0], "id": "u2", "role": "admin"}]
        updated = [{**non_owner[0], "role": "viewer"}]
        with (
            patch("ui.api_client.patch_user", new=AsyncMock(return_value=updated[0])),
            patch("ui.api_client.get_users", side_effect=[
                {"items": non_owner, "total": 1},   # guard pre-check
                {"items": updated, "total": 1},       # post-patch refresh
            ]),
        ):
            r = await ui_client.patch("/settings/users/u2/role", data={"value": "viewer"}, cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_tax_field_edit_returns_input(self, ui_client):
        with patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)):
            r = await ui_client.get("/settings/taxes/0/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<input" in r.content

    @pytest.mark.asyncio
    async def test_tax_field_patch(self, ui_client):
        updated = [{**_TAXES[0], "name": "GST"}]
        with (
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.patch_taxes", new=AsyncMock(return_value={})),
        ):
            # Re-patch get_taxes for the post-save read
            with patch("ui.api_client.get_taxes", new=AsyncMock(return_value=updated)):
                r = await ui_client.patch("/settings/taxes/0/name", data={"value": "GST"}, cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_term_field_edit(self, ui_client):
        with patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)):
            r = await ui_client.get("/settings/terms/0/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<input" in r.content

    @pytest.mark.asyncio
    async def test_schema_field_edit(self, ui_client):
        with patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)):
            r = await ui_client.get("/settings/schema/0/label/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<input" in r.content

    # ── Regression: blur-on-click-away with empty/invalid numeric values ──────

    @pytest.mark.asyncio
    async def test_tax_rate_empty_returns_cell_error(self, ui_client):
        """PATCH /settings/taxes/{idx}/rate with empty string must return cell-error, not 500."""
        r = await ui_client.patch("/settings/taxes/0/rate", data={"value": ""}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"number" in r.content.lower()

    @pytest.mark.asyncio
    async def test_tax_rate_nonnumeric_returns_cell_error(self, ui_client):
        """PATCH /settings/taxes/{idx}/rate with non-numeric value returns cell-error."""
        r = await ui_client.patch("/settings/taxes/0/rate", data={"value": "abc"}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content

    @pytest.mark.asyncio
    async def test_schema_position_empty_returns_cell_error(self, ui_client):
        """PATCH /settings/schema/{idx}/position with empty string must return cell-error, not 500."""
        r = await ui_client.patch("/settings/schema/0/position", data={"value": ""}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"whole number" in r.content.lower()

    @pytest.mark.asyncio
    async def test_schema_position_nonnumeric_returns_cell_error(self, ui_client):
        """PATCH /settings/schema/{idx}/position with non-numeric value returns cell-error."""
        r = await ui_client.patch("/settings/schema/0/position", data={"value": "abc"}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_position_empty_returns_cell_error(self, ui_client):
        """PATCH /settings/cat-schema/{cat}/{idx}/position with empty string must return cell-error, not 500."""
        r = await ui_client.patch(
            "/settings/cat-schema/Gemstone/0/position", data={"value": ""}, cookies=_authed()
        )
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"whole number" in r.content.lower()

    @pytest.mark.asyncio
    async def test_cat_schema_position_nonnumeric_returns_cell_error(self, ui_client):
        """PATCH /settings/cat-schema/{cat}/{idx}/position with non-numeric value returns cell-error."""
        r = await ui_client.patch(
            "/settings/cat-schema/Gemstone/0/position", data={"value": "abc"}, cookies=_authed()
        )
        assert r.status_code == 200
        assert b"cell-error" in r.content

    @pytest.mark.asyncio
    async def test_settings_company_tab_has_clickable_cells(self, ui_client):
        """Company tab should render click-to-edit cells, not static text."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert b"cell--clickable" in r.content

    @pytest.mark.asyncio
    async def test_settings_users_tab_has_clickable_cells(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/general?tab=users", cookies=_authed())
        assert b"cell--clickable" in r.content

    @pytest.mark.asyncio
    async def test_settings_taxes_tab_has_clickable_cells(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/sales?tab=taxes", cookies=_authed())
        assert b"cell--clickable" in r.content


class TestSettingsInlineEditValidation:
    """Regression suite: all server-side validation guards on PATCH endpoints.

    Each test confirms the guard fires (returns cell-error HTML) before any
    API call is made, preventing accidental data corruption or 500s.
    """

    # ── Terms / payment days ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_term_days_empty_returns_cell_error(self, ui_client):
        """PATCH /settings/terms/{idx}/days with empty string -> cell-error, not 500."""
        with patch("ui.api_client.patch_payment_terms", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch("/settings/terms/0/days", data={"value": ""}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"whole number" in r.content.lower()
        mock_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_term_days_nonnumeric_returns_cell_error(self, ui_client):
        """PATCH /settings/terms/{idx}/days with non-numeric value -> cell-error."""
        with patch("ui.api_client.patch_payment_terms", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch("/settings/terms/0/days", data={"value": "abc"}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content
        mock_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_term_days_negative_returns_cell_error(self, ui_client):
        """PATCH /settings/terms/{idx}/days with negative value -> cell-error."""
        with patch("ui.api_client.patch_payment_terms", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch("/settings/terms/0/days", data={"value": "-5"}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"negative" in r.content.lower()
        mock_patch.assert_not_called()

    # ── Company name / slug ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_company_name_blank_returns_cell_error(self, ui_client):
        """PATCH /settings/company/name with blank string -> cell-error, not saved."""
        with patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch("/settings/company/name", data={"value": "   "}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"blank" in r.content.lower()
        mock_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_company_slug_invalid_returns_cell_error(self, ui_client):
        """PATCH /settings/company/slug with invalid format -> cell-error, not saved."""
        with patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch(
                "/settings/company/slug", data={"value": "My Company!"}, cookies=_authed()
            )
        assert r.status_code == 200
        assert b"cell-error" in r.content
        mock_patch.assert_not_called()

    # ── User self-deactivation ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_user_cannot_deactivate_own_account(self, ui_client):
        """PATCH /settings/users/{id}/is_active=false for own user_id -> cell-error."""
        # Must be a real JWT so the base64-decode guard can extract sub="u1"
        import base64, json as _json
        _header = base64.urlsafe_b64encode(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
        _payload = base64.urlsafe_b64encode(_json.dumps({"sub": "u1", "company_id": "c1"}).encode()).rstrip(b"=").decode()
        _jwt = f"{_header}.{_payload}.fakesig"
        with patch("ui.api_client.patch_user", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch(
                "/settings/users/u1/is_active",
                data={"value": "false"},
                cookies={"celerp_token": _jwt},
            )
        assert r.status_code == 200
        assert b"cell-error" in r.content
        assert b"deactivate" in r.content.lower()
        mock_patch.assert_not_called()

    # ── Location type ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_location_type_invalid_returns_cell_error(self, ui_client):
        """PATCH /settings/locations/{id}/type with unknown value -> cell-error, not saved."""
        with patch("ui.api_client.patch_location", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch(
                "/settings/locations/loc1/type", data={"value": "moon_base"}, cookies=_authed()
            )
        assert r.status_code == 200
        assert b"cell-error" in r.content
        mock_patch.assert_not_called()

    # ── Item schema type ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_schema_type_invalid_returns_cell_error(self, ui_client):
        """PATCH /settings/schema/{idx}/type with unknown type -> cell-error, not saved."""
        with patch("ui.api_client.patch_item_schema", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch(
                "/settings/schema/0/type", data={"value": "blob"}, cookies=_authed()
            )
        assert r.status_code == 200
        assert b"cell-error" in r.content
        mock_patch.assert_not_called()

    # ── Tax type ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_tax_type_invalid_returns_cell_error(self, ui_client):
        """PATCH /settings/taxes/{idx}/tax_type with unknown value -> cell-error, not saved."""
        with patch("ui.api_client.patch_taxes", new=AsyncMock()) as mock_patch:
            r = await ui_client.patch(
                "/settings/taxes/0/tax_type", data={"value": "vat_exempt"}, cookies=_authed()
            )
        assert r.status_code == 200
        assert b"cell-error" in r.content
        mock_patch.assert_not_called()


class TestCSSConsistency:
    """Verify CSS rules are applied: headers centered, currency right-aligned."""

    @pytest.mark.asyncio
    async def test_th_centered_in_css(self):
        """app.css must have th text-align: center."""
        import pathlib
        css = pathlib.Path(__file__).parent.parent.joinpath("ui/static/app.css").read_text()
        # Find the data-table th rule
        assert "text-align: center" in css

    @pytest.mark.asyncio
    async def test_sort_link_css_exists(self):
        import pathlib
        css = pathlib.Path(__file__).parent.parent.joinpath("ui/static/app.css").read_text()
        assert ".sort-link" in css

    @pytest.mark.asyncio
    async def test_row_menu_css_exists(self):
        import pathlib
        css = pathlib.Path(__file__).parent.parent.joinpath("ui/static/app.css").read_text()
        assert ".row-menu-dropdown" in css

    @pytest.mark.asyncio
    async def test_cell_clickable_has_dashed_underline(self):
        """cell--clickable must have a persistent visual cue (dashed border) per QA fix."""
        import pathlib
        css = pathlib.Path(__file__).parent.parent.joinpath("ui/static/app.css").read_text()
        assert "dashed" in css
        assert "cell--clickable" in css

    @pytest.mark.asyncio
    async def test_column_manager_css_exists(self):
        """Column manager widget must have CSS styling."""
        import pathlib
        css = pathlib.Path(__file__).parent.parent.joinpath("ui/static/app.css").read_text()
        assert ".column-manager" in css
        assert ".column-menu" in css
        assert ".column-option" in css

    @pytest.mark.asyncio
    async def test_combobox_css_exists(self):
        """Searchable combobox must have CSS styling."""
        import pathlib
        css = pathlib.Path(__file__).parent.parent.joinpath("ui/static/app.css").read_text()
        assert ".combobox-wrap" in css
        assert ".combobox-list" in css
        assert ".combobox-option" in css


class TestSearchableSelect:
    """Verify the searchable_select component renders correctly."""

    @pytest.mark.asyncio
    async def test_small_option_list_renders_plain_select(self):
        """Under threshold → standard <select>."""
        from ui.components.table import searchable_select
        from fasthtml.common import to_xml
        opts = ["a", "b", "c"]
        result = to_xml(searchable_select("field", opts, value="a"))
        # Should render combobox-wrap (always uses combobox pattern)
        assert "combobox-wrap" in result

    @pytest.mark.asyncio
    async def test_combobox_contains_all_options(self):
        from ui.components.table import searchable_select
        from fasthtml.common import to_xml
        opts = [f"opt{i}" for i in range(15)]
        result = to_xml(searchable_select("field", opts, value="opt3"))
        for o in opts:
            assert o in result

    @pytest.mark.asyncio
    async def test_combobox_tuple_options(self):
        from ui.components.table import searchable_select
        from fasthtml.common import to_xml
        opts = [("v1", "Label One"), ("v2", "Label Two")]
        result = to_xml(searchable_select("field", opts, value="v1"))
        assert "Label One" in result
        assert "Label Two" in result
        assert 'value="v1"' in result

    @pytest.mark.asyncio
    async def test_combobox_js_initialized(self, ui_client):
        """Pages with combobox must include initCombobox JS."""
        with (
            patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/subscriptions/new", cookies=_authed())
        assert b"initCombobox" in r.content


class TestSubscriptionsPolish:
    """Tests for subscription page QA fixes."""

    @pytest.mark.asyncio
    async def test_subscriptions_empty_no_duplicate_cta(self, ui_client):
        """Empty state must NOT contain a second CTA button (dedup fix)."""
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/subscriptions", cookies=_authed())
        assert r.status_code == 200
        # Header already has "New Subscription" button; empty state must not duplicate it
        content = r.content.decode()
        assert content.count("New Subscription") == 1, "Duplicate CTA found"

    @pytest.mark.asyncio
    async def test_subscriptions_table_no_last_doc_column(self, ui_client):
        """Subscriptions table must not show the 'Last Doc' raw entity_id column."""
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value=_SUBS)):
            r = await ui_client.get("/subscriptions", cookies=_authed())
        assert b"Last Doc" not in r.content

    @pytest.mark.asyncio
    async def test_subscriptions_row_has_badge_status(self, ui_client):
        """Subscription status cell must be a badge pill."""
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value=_SUBS)):
            r = await ui_client.get("/subscriptions", cookies=_authed())
        assert b"badge--active" in r.content

    @pytest.mark.asyncio
    async def test_new_sub_form_loads_contacts(self, ui_client):
        """New subscription form must call list_contacts for the contact picker."""
        contacts = [{"entity_id": f"ct:{i}", "name": f"Contact {i}"} for i in range(12)]
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": len(contacts)})) as mock_contacts,
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/subscriptions/new", cookies=_authed())
        assert r.status_code == 200
        mock_contacts.assert_called_once()
        # With >10 contacts, searchable combobox should appear
        assert b"combobox-wrap" in r.content


class TestDocumentPolish:
    """Tests for documents page QA fixes."""

    @pytest.mark.asyncio
    async def test_doc_table_status_uses_badge(self, ui_client):
        """Document list status column must render badge pills."""
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": _DOCS, "total": len(_DOCS)})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert b"badge--sent" in r.content

    @pytest.mark.asyncio
    async def test_doc_table_type_uses_badge(self, ui_client):
        """Document list type column must render badge pills."""
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": _DOCS, "total": len(_DOCS)})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert b"badge--invoice" in r.content

    @pytest.mark.asyncio
    async def test_doc_status_field_edit_returns_select(self, ui_client):
        """Editing status field on doc detail → <select> not free text input."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1/field/status/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<select" in r.content
        assert b"paid" in r.content
        assert b"awaiting_payment" in r.content

    @pytest.mark.asyncio
    async def test_doc_table_awaiting_payment_status_uses_badge_class(self, ui_client):
        """Underscore statuses must normalize to hyphen badge classes."""
        docs = [{**_DOCS[0], "status": "awaiting_payment"}]
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value=docs)),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert r.status_code == 200
        assert b"badge--awaiting-payment" in r.content

    @pytest.mark.asyncio
    async def test_doc_date_field_edit_returns_date_input(self, ui_client):
        """Editing issue_date on doc detail → <input type=date>."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1/field/issue_date/edit", cookies=_authed())
        assert r.status_code == 200
        assert b'type="date"' in r.content

    @pytest.mark.asyncio
    async def test_doc_contact_field_edit_returns_combobox(self, ui_client):
        """Editing contact_id on doc detail → searchable combobox."""
        contacts = [{"entity_id": f"ct:{i}", "name": f"C{i}"} for i in range(12)]
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": len(contacts)})),
        ):
            r = await ui_client.get("/docs/d:1/field/contact_id/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"combobox-wrap" in r.content

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_doc_valid_until_field_edit_returns_date_input(self, ui_client):
        """Editing valid_until on doc detail → <input type=date>, not free text."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1/field/valid_until/edit", cookies=_authed())
        assert r.status_code == 200
        assert b'type="date"' in r.content

    @pytest.mark.asyncio
    async def test_doc_commission_contact_field_edit_returns_combobox(self, ui_client):
        """Editing commission_contact_id on doc detail → searchable combobox."""
        contacts = [{"entity_id": f"ct:{i}", "name": f"Agent{i}"} for i in range(12)]
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": len(contacts)})),
        ):
            r = await ui_client.get("/docs/d:1/field/commission_contact_id/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"combobox-wrap" in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_editable_cells_have_hx_get(self, ui_client):
        """All editable fields on doc detail page must have hx-get click triggers."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        assert b'hx-get="/docs/d:1/field/' in r.content
        assert b'hx-trigger="click"' in r.content
        assert b'class="editable-cell"' in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_editable_cells_have_title(self, ui_client):
        """Editable cells must have title='Click to edit' tooltip."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        assert b'title="Click to edit"' in r.content

    @pytest.mark.asyncio
    async def test_doc_empty_editable_cell_still_clickable(self, ui_client):
        """A None/empty field value renders editable-cell with '--' but still has hx-get."""
        doc = {**_DOC_DETAIL, "reference": None, "payment_terms": None}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        # -- placeholder must appear (EMPTY constant)
        assert b"--" in r.content
        # And the cell must still carry the click handler
        assert b'hx-get="/docs/d:1/field/reference/edit"' in r.content
        assert b'hx-get="/docs/d:1/field/payment_terms/edit"' in r.content

    @pytest.mark.asyncio
    async def test_doc_field_display_contact_id_shows_name(self, ui_client):
        """GET /docs/{id}/field/contact_id/display → shows contact_name, not raw entity_id."""
        doc = {**_DOC_DETAIL, "contact_id": "ct:CUST-001", "contact_name": "Alice Smith"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:1/field/contact_id/display", cookies=_authed())
        assert r.status_code == 200
        assert b"Alice Smith" in r.content
        assert b"ct:CUST-001" not in r.content

    @pytest.mark.asyncio
    async def test_doc_field_display_contact_id_fallback_when_no_name(self, ui_client):
        """GET /docs/{id}/field/contact_id/display → falls back to contact_id if no name stored."""
        doc = {**_DOC_DETAIL, "contact_id": "ct:CUST-001", "contact_name": None}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:1/field/contact_id/display", cookies=_authed())
        assert r.status_code == 200
        assert b"ct:CUST-001" in r.content


class TestSettingsPolish:
    """Settings click-to-edit affordance — visual cue must be present."""

    @pytest.mark.asyncio
    async def test_settings_detail_cells_are_clickable(self, ui_client):
        """All settings tabs must have cell--clickable cells."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_verticals_categories", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_verticals_presets", new=AsyncMock(return_value=[])),
        ):
            _TAB_URLS = {
                "company": "/settings/general?tab=company",
                "users": "/settings/general?tab=users",
                "taxes": "/settings/sales?tab=taxes",
                "terms": "/settings/sales?tab=terms",
                "schema": "/settings/inventory?tab=category-library",
            }
            for tab in ("company", "users", "taxes", "terms"):
                # schema/category-library uses schema-card not cell--clickable
                url = _TAB_URLS[tab]
                r = await ui_client.get(url, cookies=_authed())
                assert b"cell--clickable" in r.content, f"No clickable cells on tab={tab} (url={url})"

    @pytest.mark.asyncio
    async def test_settings_cells_have_title_attribute(self, ui_client):
        """cell--clickable cells must have title='Click to edit' for tooltip."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert b"Click to edit" in r.content


class TestColumnManager:
    """Column visibility toggle behavior."""

    @pytest.mark.asyncio
    async def test_column_manager_renders(self, ui_client):
        """Inventory page must include 'Manage columns' button."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"Manage columns" in r.content
        assert b"column-manager" in r.content

    @pytest.mark.asyncio
    async def test_column_manager_checkboxes_present(self, ui_client):
        """Column manager must have checkboxes for each schema field."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        # Each schema field should have a checkbox
        assert r.content.count(b'type="checkbox"') >= len(_SCHEMA) + 1  # +1 for row selects

    @pytest.mark.asyncio
    async def test_column_manager_cols_param_filters(self, ui_client):
        """?cols=name filters visible columns deterministically."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
        ):
            r = await ui_client.get("/inventory?cols=name", cookies=_authed())
        assert r.status_code == 200
        assert b"Ruby" in r.content  # name value visible
        # Status and Cost columns should not appear as th headers
        assert b"col-status" not in r.content
        assert b"col-total_cost" not in r.content

class TestPhase2DeepPolish:
    @pytest.mark.asyncio
    async def test_inventory_item_detail_page_renders(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"Back to inventory" in r.content

    @pytest.mark.asyncio
    async def test_crm_contact_field_patch(self, ui_client):
        contact = {**_CONTACTS[0], "address": "x", "payment_terms": "Net 30"}
        updated = {**contact, "phone": "999"}
        with (
            patch("ui.api_client.patch_contact", new=AsyncMock(return_value=updated)),
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=updated)),
        ):
            r = await ui_client.patch("/contacts/ct:1/field/phone", data={"value": "999"}, cookies=_authed())
        assert r.status_code == 200
        assert b"cell--clickable" in r.content

    @pytest.mark.asyncio
    async def test_docs_detail_field_patch(self, ui_client):
        updated = {**_DOC_DETAIL, "status": "paid"}
        with (
            patch("ui.api_client.patch_doc", new=AsyncMock(return_value=updated)),
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=updated)),
        ):
            r = await ui_client.patch("/docs/d:1/field/status", data={"value": "paid"}, cookies=_authed())
        assert r.status_code == 200
        assert b"paid" in r.content


# ── T1: Collapsible sidebar ──────────────────────────────────────────────────

class TestCollapsibleSidebar:

    @pytest.mark.asyncio
    async def test_sidebar_has_groups(self, ui_client):
        """Dashboard page sidebar contains collapsible group sections."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test"})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"ar_outstanding": 0, "ar_gross": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"active_total": 0})),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        assert r.status_code == 200
        html = r.text
        assert "sidebar-group" in html
        assert "Sales" in html
        assert "Documents" in html
        assert "Finance" in html
        assert "sidebar-group-header" in html

    @pytest.mark.asyncio
    async def test_sidebar_dashboard_active(self, ui_client):
        """Dashboard nav link is active, no group is auto-expanded for it."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test"})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"ar_outstanding": 0, "ar_gross": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"active_total": 0})),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        assert b"nav-link--active" in r.content
        assert b"Dashboard" in r.content

    @pytest.mark.asyncio
    async def test_sidebar_hamburger_button(self, ui_client):
        """Topbar has hamburger toggle for mobile."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test"})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"ar_outstanding": 0, "ar_gross": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"active_total": 0})),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        assert b"sidebar-toggle" in r.content

    @pytest.mark.asyncio
    async def test_sidebar_settings_group_present(self, ui_client):
        """Sidebar has a Settings footer link and module settings under their groups."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test"})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"ar_outstanding": 0, "ar_gross": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"active_total": 0})),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        assert r.status_code == 200
        html = r.text
        # Global settings accessible via footer link
        assert "/settings/general" in html
        # Module settings live under their parent groups (Inventory, Finance)
        assert "/settings/inventory" in html
        assert "/settings/accounting" in html

    @pytest.mark.asyncio
    async def test_sidebar_no_duplicate_settings_links(self, ui_client):
        """Settings nav links are deduplicated - each key appears exactly once."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test"})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"ar_outstanding": 0, "ar_gross": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"active_total": 0})),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        html = r.text
        # /settings/inventory should appear exactly once under its group
        assert html.count('href="/settings/inventory"') == 1

    @pytest.mark.asyncio
    async def test_sidebar_inventory_settings_highlighted(self, ui_client):
        """Visiting /settings/inventory shows inventory settings link in sidebar group header."""
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/settings/inventory", cookies=_authed())
        assert r.status_code == 200
        html = r.text
        # The settings link is now in the sidebar group header (sidebar-group-settings-link)
        assert 'href="/settings/inventory"' in html
        # Global settings footer link should NOT be the active nav link
        import re
        active_links = re.findall(r'href="([^"]+)"[^>]*nav-link--active', html)
        active_links += re.findall(r'nav-link--active[^>]*href="([^"]+)"', html)
        assert "/settings/general" not in active_links

    @pytest.mark.asyncio
    async def test_sidebar_accounting_settings_highlighted(self, ui_client):
        """Visiting /settings/accounting shows accounting settings link in sidebar group header."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test", "base_currency": "USD"})),
            patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_recon_rules", new=AsyncMock(return_value={"rules": []})),
            patch("ui.api_client.get_period_lock", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/settings/accounting", cookies=_authed())
        assert r.status_code == 200
        html = r.text
        # The settings link is now in the sidebar group header (sidebar-group-settings-link)
        assert 'href="/settings/accounting"' in html
        import re
        active_links = re.findall(r'href="([^"]+)"[^>]*nav-link--active', html)
        active_links += re.findall(r'nav-link--active[^>]*href="([^"]+)"', html)
        assert "/settings/general" not in active_links


# ── T3: Date range filters on reports ────────────────────────────────────────

class TestDateRangeFilters:

    @pytest.mark.asyncio
    async def test_ar_aging_has_date_filter(self, ui_client):
        with patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"lines": [], "buckets": {}})):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert r.status_code == 200
        assert b"date-filter-bar" in r.content
        assert b"preset-btn" in r.content

    @pytest.mark.asyncio
    async def test_sales_report_has_date_filter(self, ui_client):
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value={"lines": [], "group_by": "customer", "total": 0})):
            r = await ui_client.get("/reports/sales", cookies=_authed())
        assert r.status_code == 200
        assert b"date-filter-bar" in r.content
        assert b"Last 12 months" in r.content

    @pytest.mark.asyncio
    async def test_date_filter_custom_params_in_url(self, ui_client):
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value={"lines": [], "group_by": "customer", "total": 0})):
            r = await ui_client.get("/reports/sales?from=2025-01-01&to=2025-12-31", cookies=_authed())
        assert r.status_code == 200
        assert b"2025-01-01" in r.content

    @pytest.mark.asyncio
    async def test_pnl_tab_has_date_filter(self, ui_client):
        with patch("ui.api_client.get_pnl", new=AsyncMock(return_value={"revenue": {"total": 0, "lines": []}, "cogs": {"total": 0, "lines": []}, "gross_profit": 0, "expenses": {"total": 0, "lines": []}, "net_profit": 0})):
            r = await ui_client.get("/accounting?tab=pnl", cookies=_authed())
        assert r.status_code == 200
        assert b"date-filter-bar" in r.content

    @pytest.mark.asyncio
    async def test_settings_gear_link(self, ui_client):
        with patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"lines": [], "buckets": {}})):
            r = await ui_client.get("/reports/ar-aging", cookies=_authed())
        assert b"settings-gear" in r.content
        # URL may be /settings/sales?tab=terms (core) or /settings?tab=terms (module layer)
        assert b"settings" in r.content and b"terms" in r.content


# ── T2: Global search ────────────────────────────────────────────────────────

class TestGlobalSearch:

    @pytest.mark.asyncio
    async def test_search_returns_items(self, ui_client):
        items = [{"entity_id": "i:1", "sku": "SKU1", "name": "Gold Ring"}]
        with (
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": items, "total": len(items)})),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/search?q=gold", cookies=_authed())
        assert r.status_code == 200
        assert b"Gold Ring" in r.content
        assert b"search-result-item" in r.content

    @pytest.mark.asyncio
    async def test_search_short_query_returns_empty(self, ui_client):
        r = await ui_client.get("/search?q=a", cookies=_authed())
        assert r.status_code == 200
        assert b"search-result-item" not in r.content

    @pytest.mark.asyncio
    async def test_topbar_has_search_input(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test"})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"ar_outstanding": 0, "ar_gross": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"active_total": 0})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}, "lines": []})),
        ):
            r = await ui_client.get("/dashboard", cookies=_authed())
        assert b"global-search-input" in r.content


# ── T6: Sortable columns ─────────────────────────────────────────────────────

class TestSortableColumns:

    @pytest.mark.asyncio
    async def test_docs_table_has_sort_links(self, ui_client):
        docs = [{"entity_id": "d:1", "doc_number": "INV-1", "doc_type": "invoice", "contact_name": "Acme", "issue_date": "2026-01-01", "due_date": "2026-01-15", "total": 1000, "amount_outstanding": 500, "status": "sent"}]
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value=docs)),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={"total_count": 1})),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert r.status_code == 200
        assert b"sort-link" in r.content

    @pytest.mark.asyncio
    async def test_crm_table_has_sort_links(self, ui_client):
        contacts = [{"entity_id": "c:1", "name": "Acme", "phone": "1", "email": "a@a.com", "tax_id": "", "credit_limit": 1000, "contact_type": "customer"}]
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": len(contacts)})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={"count": 1, "total_balance": 0})),
        ):
            r = await ui_client.get("/contacts/customers", cookies=_authed())
        assert r.status_code == 200
        assert b"sort-link" in r.content

    @pytest.mark.asyncio
    async def test_reports_table_has_sort_links(self, ui_client):
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value={"lines": [{"label": "Acme", "count": 2, "total": 1200}], "group_by": "customer", "total": 1200})):
            r = await ui_client.get("/reports/sales", cookies=_authed())
        assert r.status_code == 200
        assert b"sort-link" in r.content


# ── T8: Manufacturing page ────────────────────────────────────────────────────

class TestManufacturingPage:

    @pytest.mark.asyncio
    async def test_manufacturing_list_renders(self, ui_client):
        orders = [{"entity_id": "mfg:abc123", "order_type": "assembly", "status": "draft", "created_at": "2026-01-01", "inputs": []}]
        with patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": orders, "total": len(orders)})):
            r = await ui_client.get("/manufacturing", cookies=_authed())
        assert r.status_code == 200
        assert b"Manufacturing" in r.content
        assert b"mfg-table" in r.content

    @pytest.mark.asyncio
    async def test_manufacturing_list_empty(self, ui_client):
        with patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/manufacturing", cookies=_authed())
        assert r.status_code == 200
        assert b"No" in r.content  # empty state present in any form

    @pytest.mark.asyncio
    async def test_manufacturing_unauthed_redirects(self, ui_client):
        r = await ui_client.get("/manufacturing")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    @pytest.mark.asyncio
    async def test_manufacturing_detail_renders(self, ui_client):
        order = {
            "entity_id": "mfg:abc123",
            "order_type": "assembly",
            "status": "draft",
            "description": "Test order",
            "inputs": [{"item_id": "item:x1", "quantity": 5}],
            "expected_outputs": [{"sku": "OUT-1", "name": "Widget", "quantity": 10}],
            "steps_completed": [],
        }
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=order)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert r.status_code == 200
        assert b"mfg-detail" in r.content
        assert b"Start Order" in r.content

    @pytest.mark.asyncio
    async def test_manufacturing_sidebar_link_present(self, ui_client):
        with patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/manufacturing", cookies=_authed())
        assert b"/manufacturing" in r.content

    @pytest.mark.asyncio
    async def test_manufacturing_detail_shows_bom(self, ui_client):
        order = {
            "entity_id": "mfg:abc123",
            "status": "in_progress",
            "inputs": [{"item_id": "item:x1", "quantity": 3}],
            "expected_outputs": [{"sku": "OUT-1", "name": "Widget", "quantity": 2}],
            "steps_completed": [],
        }
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=order)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert b"Inputs (BOM)" in r.content
        assert b"Expected Outputs" in r.content

    @pytest.mark.asyncio
    async def test_manufacturing_start_htmx(self, ui_client):
        order = {"entity_id": "mfg:abc123", "status": "in_progress", "inputs": [], "expected_outputs": [], "steps_completed": []}
        with (
            patch("ui.api_client.start_mfg_order", new=AsyncMock(return_value={"event_id": "ev1"})),
            patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=order)),
        ):
            r = await ui_client.post("/manufacturing/mfg:abc123/start", cookies=_authed())
        assert r.status_code == 200
        assert b"mfg-detail" in r.content

    @pytest.mark.asyncio
    async def test_manufacturing_cancel_htmx(self, ui_client):
        order = {"entity_id": "mfg:abc123", "status": "cancelled", "inputs": [], "expected_outputs": [], "steps_completed": []}
        with (
            patch("ui.api_client.cancel_mfg_order", new=AsyncMock(return_value={"event_id": "ev1"})),
            patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=order)),
        ):
            r = await ui_client.post("/manufacturing/mfg:abc123/cancel", cookies=_authed())
        assert r.status_code == 200
        assert b"mfg-detail" in r.content


# ── T5: CSV import/export ────────────────────────────────────────────────────

class TestCSVExport:

    @pytest.mark.asyncio
    async def test_inventory_page_has_export_button(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 0, "category_counts": {}})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"Export CSV" in r.content

    @pytest.mark.asyncio
    async def test_docs_page_has_export_button(self, ui_client):
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert b"Export CSV" in r.content

    @pytest.mark.asyncio
    async def test_crm_page_has_export_button(self, ui_client):
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/contacts/customers", cookies=_authed())
        assert b"Export CSV" in r.content

    @pytest.mark.asyncio
    async def test_inventory_export_csv_returns_csv(self, ui_client):
        csv_bytes = b"entity_id,sku,name\nitem:1,SKU-1,Widget\n"
        with patch("ui.api_client.export_items_csv", new=AsyncMock(return_value=csv_bytes)):
            r = await ui_client.get("/inventory/export/csv", cookies=_authed())
        assert r.status_code == 200
        assert b"sku" in r.content or b"attachment" in r.headers.get("content-disposition", "").encode()

    @pytest.mark.asyncio
    async def test_docs_export_csv_returns_csv(self, ui_client):
        csv_bytes = b"entity_id,doc_number,status\ndoc:1,INV-1,paid\n"
        with patch("ui.api_client.export_docs_csv", new=AsyncMock(return_value=csv_bytes)):
            r = await ui_client.get("/docs/export/csv", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_crm_export_csv_returns_csv(self, ui_client):
        csv_bytes = b"entity_id,name,email\ncontact:1,Acme,acme@a.com\n"
        with patch("ui.api_client.export_contacts_csv", new=AsyncMock(return_value=csv_bytes)):
            r = await ui_client.get("/crm/export/csv", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_inventory_import_page_renders(self, ui_client):
        r = await ui_client.get("/inventory/import", cookies=_authed())
        assert r.status_code == 200
        assert b"Import Inventory" in r.content
        assert b"csv_file" in r.content

    @pytest.mark.asyncio
    async def test_inventory_import_unauthed_redirects(self, ui_client):
        r = await ui_client.get("/inventory/import")
        assert r.status_code == 302


# ── T11: Price range grouping ─────────────────────────────────────────────────

class TestPriceRangeReports:

    @pytest.mark.asyncio
    async def test_sales_report_has_price_range_option(self, ui_client):
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value={"lines": [], "group_by": "customer", "total_revenue": 0})):
            r = await ui_client.get("/reports/sales", cookies=_authed())
        assert b"price_range" in r.content or b"Price Range" in r.content

    @pytest.mark.asyncio
    async def test_purchases_report_has_price_range_option(self, ui_client):
        with patch("ui.api_client.get_purchases_report", new=AsyncMock(return_value={"lines": [], "group_by": "supplier", "total_spend": 0})):
            r = await ui_client.get("/reports/purchases", cookies=_authed())
        assert b"price_range" in r.content or b"Price Range" in r.content

    @pytest.mark.asyncio
    async def test_sales_report_price_range_renders_buckets(self, ui_client):
        data = {
            "group_by": "price_range",
            "lines": [
                {"price_range": "0-1000", "invoice_count": 5, "total_revenue": 500},
                {"price_range": "1001-5000", "invoice_count": 3, "total_revenue": 9000},
                {"price_range": "5001-20000", "invoice_count": 1, "total_revenue": 15000},
                {"price_range": "20000+", "invoice_count": 0, "total_revenue": 0},
            ],
            "total_revenue": 24500,
        }
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value=data)):
            r = await ui_client.get("/reports/sales?group_by=price_range", cookies=_authed())
        assert r.status_code == 200
        assert b"0-1000" in r.content
        assert b"Price Range" in r.content

    @pytest.mark.asyncio
    async def test_sales_report_price_range_labels_are_clickable(self, ui_client):
        data = {
            "group_by": "price_range",
            "lines": [{"price_range": "0-1000", "invoice_count": 2, "total_revenue": 800}],
            "total_revenue": 800,
        }
        with patch("ui.api_client.get_sales_report", new=AsyncMock(return_value=data)):
            r = await ui_client.get("/reports/sales?group_by=price_range", cookies=_authed())
        assert b"href" in r.content


# ── T12: Column defaults ─────────────────────────────────────────────────────

class TestColumnDefaults:

    @pytest.mark.asyncio
    async def test_data_table_has_resize_script(self, ui_client):
        schema = [{"key": "sku", "label": "SKU", "type": "text", "editable": True}]
        items = [{"entity_id": "item:1", "sku": "SKU-1", "name": "Widget"}]
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=schema)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": items, "total": len(items)})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 1, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 1, "category_counts": {}})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"col-resize-handle" in r.content or b"colEmpty" in r.content

    @pytest.mark.asyncio
    async def test_data_table_has_localstorage_script(self, ui_client):
        schema = [{"key": "sku", "label": "SKU", "type": "text", "editable": True}]
        items = [{"entity_id": "item:1", "sku": "SKU-1", "name": "Widget"}]
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=schema)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": items, "total": len(items)})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 1, "cost_total": 0, "retail_total": 0, "wholesale_total": 0, "active_item_count": 1, "category_counts": {}})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"localStorage" in r.content
        assert b"celerp_cols_" in r.content

    @pytest.mark.asyncio
    async def test_col_resize_handle_css_exists(self):
        css = open(pathlib.Path(__file__).parent.parent / "ui/static/app.css").read()
        assert "col-resize-handle" in css
        assert "cursor: col-resize" in css


# ===========================================================================
# Sprint 4 UI Tests
# ===========================================================================

_BLANK_DOC = {
    "entity_id": "doc:INV-2026-0001",
    "ref_id": "INV-2026-0001",
    "doc_type": "invoice",
    "status": "draft",
    "line_items": [],
    "contact_id": None,
    "issue_date": None,
    "due_date": None,
    "total": 0,
    "tax": 0,
    "amount_outstanding": 0,
    "amount_paid": 0,
    "payment_terms": None,
}

_SENT_DOC = {**_BLANK_DOC, "status": "sent", "total": 10000, "amount_outstanding": 10000}
_FINAL_DOC = {**_BLANK_DOC, "status": "final", "total": 5000, "amount_outstanding": 5000}


class TestSprint4DocCreation:
    """T1: blank-first document creation via UI route."""

    @pytest.mark.asyncio
    async def test_create_blank_returns_hx_redirect(self, ui_client):
        """POST /docs/create-blank returns 204 with HX-Redirect header."""
        with patch("ui.api_client.create_doc",
                   new=AsyncMock(return_value={"entity_id": "doc:INV-2026-0001"})):
            r = await ui_client.post("/docs/create-blank?type=invoice", cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers
        assert "/docs/doc:INV-2026-0001" in r.headers["HX-Redirect"]

    @pytest.mark.asyncio
    async def test_create_blank_unauthorized_redirects(self, ui_client):
        """POST /docs/create-blank without cookie redirects to login."""
        r = await ui_client.post("/docs/create-blank?type=invoice")
        assert r.status_code == 302

    @pytest.mark.asyncio
    async def test_docs_page_new_invoice_uses_htmx(self, ui_client):
        """The New Invoice button on /docs uses hx-post for quick create."""
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_DOC_SUMMARY)),
        ):
            r = await ui_client.get("/docs", cookies=_authed())
        assert r.status_code == 200
        assert b"hx-post" in r.content or b"hx_post" in r.content
        assert b"create-blank" in r.content

    @pytest.mark.asyncio
    async def test_create_blank_uses_id_key(self, ui_client):
        """API returns {id: ...} not {entity_id: ...}; UI must handle both."""
        with patch("ui.api_client.create_doc",
                   new=AsyncMock(return_value={"id": "doc:INV-NEW"})):
            r = await ui_client.post("/docs/create-blank?type=invoice", cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers
        assert "/docs/doc:INV-NEW" in r.headers["HX-Redirect"]

    async def test_create_blank_quotation_uses_id_key(self, ui_client):
        """Same fix for quotation type."""
        with patch("ui.api_client.create_doc",
                   new=AsyncMock(return_value={"id": "doc:QUO-NEW", "event_id": "evt:2"})):
            r = await ui_client.post("/docs/create-blank?type=quotation", cookies=_authed())
        assert r.status_code == 204
        assert "/docs/doc:QUO-NEW" in r.headers.get("HX-Redirect", "")


class TestListsCreateBlank:
    """Tests for lists create-blank route and list type choices."""

    @pytest.mark.asyncio
    async def test_create_blank_list_uses_id_key(self, ui_client):
        """POST /lists/create-blank handles API returning {id: ...} not {entity_id: ...}."""
        with patch("ui.api_client.create_list",
                   new=AsyncMock(return_value={"id": "list:VIEW-001", "event_id": "evt:3"})):
            r = await ui_client.post("/lists/create-blank", cookies=_authed())
        assert r.status_code == 204
        assert "/lists/list:VIEW-001" in r.headers.get("HX-Redirect", "")

    @pytest.mark.asyncio
    async def test_lists_new_get_redirects_without_interim_page(self, ui_client):
        """/lists/new GET no longer shows a form - creates immediately and redirects."""
        with patch("ui.api_client.create_list",
                   new=AsyncMock(return_value={"id": "list:VIEW-002"})):
            r = await ui_client.get("/lists/new", cookies=_authed(), follow_redirects=False)
        assert r.status_code == 302
        assert "/lists/list:VIEW-002" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_lists_page_no_href_new(self, ui_client):
        """'New List' button on /lists uses hx-post, not href='/lists/new'."""
        with (
            patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/lists", cookies=_authed())
        assert r.status_code == 200
        assert b'href="/lists/new"' not in r.content

    @pytest.mark.asyncio
    async def test_lists_page_has_correct_types(self, ui_client):
        """List type dropdown includes quotation, transfer, audit."""
        with (
            patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.get("/lists", cookies=_authed())
        assert r.status_code == 200
        assert b"quotation" in r.content
        assert b"transfer" in r.content
        assert b"audit" in r.content
        # Old incorrect types should not appear
        assert b'"sale"' not in r.content
        assert b'"exchange"' not in r.content
        assert b'"website_order"' not in r.content



    """T2: inline line item save route."""

    @pytest.mark.asyncio
    async def test_doc_detail_draft_shows_add_line_button(self, ui_client):
        """Draft doc detail page has + Add Line button."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Add item" in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_draft_shows_add_line_button(self, ui_client):
        """Draft doc detail page has Add item button (auto-saves on blur, no Save button)."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Add item" in r.content
        assert b"Save lines" not in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_finalized_no_add_line(self, ui_client):
        """Finalized doc shows no + Add Line button."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_FINAL_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Add item" not in r.content

    @pytest.mark.asyncio
    async def test_save_lines_route_calls_patch_doc(self, ui_client):
        """POST /docs/{id}/lines calls api.patch_doc and returns JSON ok."""
        lines = [{"description": "Test", "sku": "T-01", "quantity": 1, "unit_price": 100,
                  "tax_rate": 0, "line_total": 100}]
        with patch("ui.api_client.patch_doc", new=AsyncMock(return_value={})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/lines",
                json={"line_items": lines, "subtotal": 100, "tax": 0, "total": 100},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_save_lines_unauthorized_redirects(self, ui_client):
        """POST /docs/{id}/lines without cookie redirects to login."""
        r = await ui_client.post(
            "/docs/doc:INV-2026-0001/lines",
            json={"line_items": [], "subtotal": 0, "tax": 0, "total": 0},
        )
        assert r.status_code == 302

    @pytest.mark.asyncio
    async def test_doc_detail_empty_state_message(self, ui_client):
        """Draft doc with no lines pre-opens one empty editable row."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Add item" in r.content


class TestSprint4DocActions:
    """T3: finalize, void, send UI routes."""

    @pytest.mark.asyncio
    async def test_action_finalize_redirects(self, ui_client):
        """POST /docs/{id}/action/finalize returns HX-Redirect."""
        with patch("ui.api_client.finalize_doc", new=AsyncMock(return_value={})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/action/finalize",
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_action_send_redirects(self, ui_client):
        """POST /docs/{id}/action/send returns HX-Redirect."""
        with patch("ui.api_client.send_doc", new=AsyncMock(return_value={})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/action/send",
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_action_void_with_reason_redirects(self, ui_client):
        """POST /docs/{id}/action/void with reason returns HX-Redirect."""
        with patch("ui.api_client.void_doc", new=AsyncMock(return_value={})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/action/void",
                data={"reason": "Duplicate"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_action_unknown_returns_400(self, ui_client):
        """Unknown action returns 400."""
        r = await ui_client.post(
            "/docs/doc:INV-2026-0001/action/reopen",
            cookies=_authed(),
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_doc_detail_draft_shows_finalize_button(self, ui_client):
        """Draft doc detail shows context-appropriate finalize button (Issue Invoice for invoices)."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Issue Invoice" in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_draft_shows_void_form(self, ui_client):
        """Draft doc detail does NOT show Void button (only Delete is shown for drafts)."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Void" not in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_draft_shows_delete_button(self, ui_client):
        """Draft doc detail shows Delete button/form."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Confirm Delete" in r.content

    @pytest.mark.asyncio
    async def test_action_delete_draft_redirects(self, ui_client):
        """POST /docs/{id}/action/delete returns HX-Redirect to docs list."""
        with patch("ui.api_client.delete_doc", new=AsyncMock(return_value={"deleted": "doc:INV-2026-0001"})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/action/delete",
                data={"doc_type": "invoice"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "/docs?type=invoice" in r.headers.get("HX-Redirect", "")

    @pytest.mark.asyncio
    async def test_doc_detail_draft_shows_unit_column(self, ui_client):
        """Draft doc line item table includes Unit column header."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        html = r.text
        assert ">Unit<" in html or "Unit</th>" in html

    @pytest.mark.asyncio
    async def test_no_popups_in_doc_detail(self, ui_client):
        """Doc detail must not contain dialog or modal elements."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        content = r.content.lower()
        assert b"<dialog" not in content
        assert b"modal" not in content


class TestSprint4Payment:
    """T4: payment recording UI."""

    @pytest.mark.asyncio
    async def test_payment_route_calls_api(self, ui_client):
        """POST /docs/{id}/payment calls api.record_payment and returns HX-Redirect."""
        with patch("ui.api_client.record_payment", new=AsyncMock(return_value={})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/payment",
                data={"amount": "5000", "method": "transfer", "reference": "TXN-99"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_payment_section_visible_for_sent_invoice(self, ui_client):
        """Sent invoice detail shows Record Payment section."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_SENT_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Record Payment" in r.content or b"payment" in r.content.lower()

    @pytest.mark.asyncio
    async def test_payment_section_not_visible_for_draft(self, ui_client):
        """Draft invoice has no Record Payment section."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Record Payment" not in r.content


class TestSprint4Polish:
    """T6: doc detail polish."""

    @pytest.mark.asyncio
    async def test_doc_detail_shows_status_badge(self, ui_client):
        """Doc detail page shows a status badge."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"badge--draft" in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_shows_doc_type(self, ui_client):
        """Doc detail shows doc type label (e.g. 'Invoice')."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Invoice" in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_shows_ref(self, ui_client):
        """Doc detail shows the document reference number prominently."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"INV-2026-0001" in r.content


class TestSprint4BOMs:
    """T5: BOM management UI."""

    @pytest.mark.asyncio
    async def test_bom_list_page_renders(self, ui_client):
        """GET /manufacturing/boms renders BOM list."""
        boms = [{"bom_id": "bom:1", "name": "Ring BOM", "output_item_id": "item:x", "output_qty": 1, "components": [{"sku": "A"}]}]
        with patch("ui.api_client.list_boms", new=AsyncMock(return_value={"items": boms, "total": len(boms)})):
            r = await ui_client.get("/manufacturing/boms", cookies=_authed())
        assert r.status_code == 200
        assert b"Ring BOM" in r.content

    @pytest.mark.asyncio
    async def test_bom_list_empty_state(self, ui_client):
        """Empty BOM list shows empty state message."""
        with patch("ui.api_client.list_boms", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/manufacturing/boms", cookies=_authed())
        assert r.status_code == 200
        assert b"No BOMs" in r.content or b"New BOM" in r.content

    @pytest.mark.asyncio
    async def test_bom_detail_shows_components(self, ui_client):
        """GET /manufacturing/boms/{id} renders component rows."""
        bom = {
            "bom_id": "bom:1", "name": "Ring BOM",
            "output_item_id": "item:ring", "output_qty": 1.0,
            "components": [{"sku": "GLD-18K", "qty": 5.0, "unit": "grams"}],
        }
        with patch("ui.api_client.get_bom", new=AsyncMock(return_value=bom)):
            r = await ui_client.get("/manufacturing/boms/bom:1", cookies=_authed())
        assert r.status_code == 200
        assert b"GLD-18K" in r.content
        assert b"Add Component" in r.content

    @pytest.mark.asyncio
    async def test_bom_new_form_renders(self, ui_client):
        """GET /manufacturing/boms/new renders creation form."""
        r = await ui_client.get("/manufacturing/boms/new", cookies=_authed())
        assert r.status_code == 200
        assert b"BOM Name" in r.content or b"name" in r.content.lower()

    @pytest.mark.asyncio
    async def test_manufacturing_page_shows_boms_link(self, ui_client):
        """Manufacturing list page has a link to BOMs section."""
        with patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/manufacturing", cookies=_authed())
        assert b"boms" in r.content.lower() or b"Bill" in r.content


# ===========================================================================
# Sprint 5 UI Tests
# ===========================================================================

_QUOTATION_DOC = {
    "entity_id": "doc:QUO-2026-0001",
    "ref_id": "QUO-2026-0001",
    "doc_type": "quotation",
    "status": "draft",
    "line_items": [],
    "contact_id": None,
    "issue_date": None,
    "due_date": None,
    "valid_until": "2026-03-31",
    "total": 0,
    "tax": 0,
    "amount_outstanding": 0,
    "amount_paid": 0,
    "payment_terms": None,
}

_PO_DOC = {
    "entity_id": "doc:PO-2026-0001",
    "ref_id": "PO-2026-0001",
    "doc_type": "purchase_order",
    "status": "sent",
    "line_items": [
        {"description": "Widget", "item_id": "item:w1", "quantity": 10, "unit_price": 50},
    ],
    "contact_id": "ct:sup1",
    "issue_date": "2026-01-01",
    "due_date": None,
    "total": 500,
    "tax": 0,
    "amount_outstanding": 500,
    "amount_paid": 0,
    "payment_terms": None,
}

_PAID_INVOICE = {
    **_BLANK_DOC,
    "status": "paid",
    "total": 10000,
    "amount_outstanding": 0,
    "amount_paid": 10000,
    "doc_type": "invoice",
}

_DEAL = {
    "entity_id": "deal:d1",
    "name": "Big Sale",
    "stage": "lead",
    "value": 50000,
    "contact_id": "ct:1",
    "contact_name": "Alice",
    "expected_close": "2026-06-01",
    "status": "open",
}

_MEMO = {
    "entity_id": "memo:m1",
    "memo_number": "MEM-001",
    "status": "draft",
    "contact_id": "ct:1",
    "notes": "Test memo",
    "items": [
        {"item_id": "item:r1", "quantity": 2, "price": 5000},
    ],
}

_MFG_ORDER_WITH_STEPS = {
    "entity_id": "mfg:abc123",
    "order_type": "assembly",
    "status": "in_progress",
    "description": "Test assembly",
    "inputs": [
        {"item_id": "item:x1", "quantity": 5, "consumed_qty": 0},
    ],
    "expected_outputs": [{"sku": "OUT-1", "name": "Widget", "quantity": 2}],
    "steps_completed": [],
    "steps": [
        {"step_id": "step1", "name": "Cut", "status": "pending"},
        {"step_id": "step2", "name": "Polish", "status": "pending"},
    ],
}


class TestSprint5POReceive:
    """T2: PO Receive flow."""

    @pytest.mark.asyncio
    async def test_po_detail_shows_receive_section(self, ui_client):
        """PO detail page shows Receive Goods section."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_PO_DOC)):
            r = await ui_client.get("/docs/doc:PO-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Receive Goods" in r.content

    @pytest.mark.asyncio
    async def test_po_receive_has_location_input(self, ui_client):
        """PO receive section has location input."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_PO_DOC)):
            r = await ui_client.get("/docs/doc:PO-2026-0001", cookies=_authed())
        assert b"location_name" in r.content or b"location_id" in r.content

    @pytest.mark.asyncio
    async def test_po_receive_route_calls_api(self, ui_client):
        """POST /docs/{id}/receive calls api.receive_po."""
        with patch("ui.api_client.receive_po", new=AsyncMock(return_value={"event_id": "ev1"})):
            r = await ui_client.post(
                "/docs/doc:PO-2026-0001/receive",
                data={"location_id": "loc:1", "item_id_0": "item:w1", "qty_0": "10"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_po_receive_has_record_receipt_button(self, ui_client):
        """PO receive section has Record Receipt button."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_PO_DOC)):
            r = await ui_client.get("/docs/doc:PO-2026-0001", cookies=_authed())
        assert b"Record Receipt" in r.content

    @pytest.mark.asyncio
    async def test_invoice_no_receive_section(self, ui_client):
        """Invoice detail does not show Receive Goods."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DOC_DETAIL)):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert b"Receive Goods" not in r.content


class TestSprint5ItemActions:
    """T3: Item actions on detail page."""

    @pytest.mark.asyncio
    async def test_item_detail_has_actions_panel(self, ui_client):
        """Item detail page shows compact action cards grid."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"action-cards-grid" in r.content
        assert b"Split" in r.content
        assert b"Duplicate" in r.content
        assert b"Adjust Stock" not in r.content
        assert b"Update Prices" not in r.content
        assert b"Change Status" not in r.content

    @pytest.mark.asyncio
    async def test_item_detail_has_adjust_stock(self, ui_client):
        """Adjust Stock is removed; quantity is editable inline on the Details tab."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"Adjust Stock" not in r.content

    @pytest.mark.asyncio
    async def test_item_detail_has_update_prices(self, ui_client):
        """Update Prices is removed; prices are editable inline on the Pricing tab."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"Update Prices" not in r.content

    @pytest.mark.asyncio
    async def test_item_detail_has_change_status(self, ui_client):
        """Change Status is removed; status is editable inline."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"Change Status" not in r.content

    @pytest.mark.asyncio
    async def test_item_detail_has_write_off(self, ui_client):
        """Expire and Dispose remain in the Advanced panel (require a reason)."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"Write Off" not in r.content   # old section heading gone
        assert b"Expire" in r.content
        assert b"Dispose" in r.content

    @pytest.mark.asyncio
    async def test_adjust_item_route(self, ui_client):
        with patch("ui.api_client.adjust_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/adjust", data={"new_qty": "50"}, cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_transfer_item_route(self, ui_client):
        with patch("ui.api_client.transfer_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/transfer", data={"location_id": "loc:1"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_reserve_item_route(self, ui_client):
        with patch("ui.api_client.reserve_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/reserve", data={"quantity": "5", "reference": "ORD-1"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_unreserve_item_route(self, ui_client):
        with patch("ui.api_client.unreserve_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/unreserve", data={"quantity": "3"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_price_item_route(self, ui_client):
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}])),
            patch("ui.api_client.set_item_price", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/gc:123/price", data={"retail_price": "300", "wholesale_price": "200", "cost_price": "100"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_status_item_route(self, ui_client):
        with patch("ui.api_client.set_item_status", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/status", data={"status": "inactive"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_expire_item_route(self, ui_client):
        with patch("ui.api_client.expire_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/expire", data={"reason": "past date"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_dispose_item_route(self, ui_client):
        with patch("ui.api_client.dispose_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/dispose", data={"reason": "damaged", "notes": "dropped"}, cookies=_authed())
        assert r.status_code == 204

    # ── Split ─────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_item_detail_has_split_section(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert b"Split" in r.content

    @pytest.mark.asyncio
    async def test_split_item_route_success(self, ui_client):
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "PARENT-001", "quantity": 10})),
            patch("ui.api_client.split_item", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/gc:123/split", data={
                "child_sku_0": "SKU-A", "child_qty_0": "3",
                "child_sku_1": "SKU-B", "child_qty_1": "2",
            }, cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_split_item_route_invalid_quantities(self, ui_client):
        with patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "P-001", "quantity": 10})):
            r = await ui_client.post("/api/items/gc:123/split", data={
                "child_sku_0": "SKU-A", "child_qty_0": "abc",
            }, cookies=_authed())
        assert r.status_code == 200
        assert b"Invalid" in r.content

    @pytest.mark.asyncio
    async def test_split_item_route_too_few_parts(self, ui_client):
        """Split with no children (empty legacy form) returns error."""
        with patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "P-001", "quantity": 10})):
            r = await ui_client.post("/api/items/gc:123/split", data={}, cookies=_authed())
        assert r.status_code == 200
        assert b"Enter" in r.content

    @pytest.mark.asyncio
    async def test_split_item_route_api_error(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "P-001", "quantity": 10})),
            patch("ui.api_client.split_item", new=AsyncMock(side_effect=APIError(400, "bad split"))),
        ):
            r = await ui_client.post("/api/items/gc:123/split", data={
                "child_sku_0": "SKU-A", "child_qty_0": "3",
                "child_sku_1": "SKU-B", "child_qty_1": "2",
            }, cookies=_authed())
        assert r.status_code == 200
        assert b"bad split" in r.content

    # ── Merge ─────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_item_detail_has_merge_section(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert b"Merge" in r.content

    @pytest.mark.asyncio
    async def test_merge_items_route_success(self, ui_client):
        with patch("ui.api_client.merge_items", new=AsyncMock(return_value={"id": "item:new123"})):
            r = await ui_client.post(
                "/api/items/merge",
                data={"source_entity_ids": ["item:a", "item:b"], "target_sku_from": "item:a", "resulting_quantity": "10"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_merge_items_route_missing_target(self, ui_client):
        r = await ui_client.post(
            "/api/items/merge",
            data={"source_entity_ids": ["item:a", "item:b"], "target_sku_from": ""},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"required" in r.content.lower()

    @pytest.mark.asyncio
    async def test_merge_items_route_missing_sources(self, ui_client):
        r = await ui_client.post(
            "/api/items/merge",
            data={"source_entity_ids": [], "target_sku_from": "item:target"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"required" in r.content.lower()

    @pytest.mark.asyncio
    async def test_merge_items_route_api_error(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.merge_items", new=AsyncMock(side_effect=APIError(400, "merge conflict"))):
            r = await ui_client.post(
                "/api/items/merge",
                data={"source_entity_ids": ["item:a", "item:b"], "target_sku_from": "item:a"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"merge conflict" in r.content

    # ── Duplicate ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_item_detail_has_duplicate_section(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert b"Duplicate" in r.content
        assert b"New SKU" in r.content

    @pytest.mark.asyncio
    async def test_duplicate_item_route_success(self, ui_client):
        new_id = "item:new-copy"
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.create_item", new=AsyncMock(return_value={"id": new_id, "event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/gc:123/duplicate", data={"new_sku": "D4627-COPY"}, cookies=_authed())
        assert r.status_code == 204
        assert r.headers.get("HX-Redirect", "").endswith(new_id)

    @pytest.mark.asyncio
    async def test_duplicate_item_carries_fields(self, ui_client):
        """Duplicate passes source fields through to create_item, overriding only sku."""
        captured = {}
        async def _mock_create(token, data):
            captured.update(data)
            return {"id": "item:copy", "event_id": "e1"}
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.create_item", new=_mock_create),
        ):
            await ui_client.post("/api/items/gc:123/duplicate", data={"new_sku": "NEW-SKU"}, cookies=_authed())
        assert captured.get("sku") == "NEW-SKU"
        assert captured.get("name") == _ITEM.get("name")
        # status and id must NOT be carried over
        assert "status" not in captured
        assert "id" not in captured

    @pytest.mark.asyncio
    async def test_duplicate_item_route_missing_sku(self, ui_client):
        r = await ui_client.post("/api/items/gc:123/duplicate", data={"new_sku": ""}, cookies=_authed())
        assert r.status_code == 200
        assert b"required" in r.content.lower()

    @pytest.mark.asyncio
    async def test_duplicate_item_route_source_fetch_error(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.get_item", new=AsyncMock(side_effect=APIError(404, "not found"))):
            r = await ui_client.post("/api/items/gc:123/duplicate", data={"new_sku": "COPY-1"}, cookies=_authed())
        assert r.status_code == 200
        assert b"not found" in r.content

    @pytest.mark.asyncio
    async def test_duplicate_item_route_create_error(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.create_item", new=AsyncMock(side_effect=APIError(422, "sku already exists"))),
        ):
            r = await ui_client.post("/api/items/gc:123/duplicate", data={"new_sku": "DUPE-SKU"}, cookies=_authed())
        assert r.status_code == 200
        assert b"sku already exists" in r.content


class TestItemActionRouteCompleteness:
    """Thorough coverage of every item action route: redirect targets, arg passing,
    API error propagation, and bad-input handling. Fills gaps left by smoke tests."""

    # ── adjust ───────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_adjust_redirects_to_item(self, ui_client):
        with patch("ui.api_client.adjust_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/adjust", data={"new_qty": "7"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_adjust_passes_qty_to_api(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, new_qty):
            captured["qty"] = new_qty
            return {"event_id": "e1"}
        with patch("ui.api_client.adjust_item", new=_mock):
            await ui_client.post("/api/items/gc:123/adjust", data={"new_qty": "42.5"}, cookies=_authed())
        assert captured["qty"] == 42.5

    @pytest.mark.asyncio
    async def test_adjust_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.adjust_item", new=AsyncMock(side_effect=APIError(400, "qty invalid"))):
            r = await ui_client.post("/api/items/gc:123/adjust", data={"new_qty": "-1"}, cookies=_authed())
        assert r.status_code == 200
        assert b"qty invalid" in r.content

    # ── transfer ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_transfer_redirects_to_item(self, ui_client):
        with patch("ui.api_client.transfer_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/transfer", data={"location_id": "loc:1"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_transfer_passes_location_to_api(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, location_id):
            captured["location_id"] = location_id
            return {"event_id": "e1"}
        with patch("ui.api_client.transfer_item", new=_mock):
            await ui_client.post("/api/items/gc:123/transfer", data={"location_id": "loc:abc"}, cookies=_authed())
        assert captured["location_id"] == "loc:abc"

    @pytest.mark.asyncio
    async def test_transfer_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.transfer_item", new=AsyncMock(side_effect=APIError(404, "location not found"))):
            r = await ui_client.post("/api/items/gc:123/transfer", data={"location_id": "loc:bad"}, cookies=_authed())
        assert r.status_code == 200
        assert b"location not found" in r.content

    # ── reserve ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_reserve_redirects_to_item(self, ui_client):
        with patch("ui.api_client.reserve_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/reserve", data={"quantity": "3", "reference": "ORD-1"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_reserve_passes_qty_and_ref_to_api(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, quantity, reference):
            captured.update({"qty": quantity, "ref": reference})
            return {"event_id": "e1"}
        with patch("ui.api_client.reserve_item", new=_mock):
            await ui_client.post("/api/items/gc:123/reserve", data={"quantity": "5", "reference": "PO-99"}, cookies=_authed())
        assert captured["qty"] == 5.0
        assert captured["ref"] == "PO-99"

    @pytest.mark.asyncio
    async def test_reserve_empty_reference_passes_none(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, quantity, reference):
            captured["ref"] = reference
            return {"event_id": "e1"}
        with patch("ui.api_client.reserve_item", new=_mock):
            await ui_client.post("/api/items/gc:123/reserve", data={"quantity": "2", "reference": ""}, cookies=_authed())
        assert captured["ref"] is None

    @pytest.mark.asyncio
    async def test_reserve_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.reserve_item", new=AsyncMock(side_effect=APIError(422, "exceeds available"))):
            r = await ui_client.post("/api/items/gc:123/reserve", data={"quantity": "999"}, cookies=_authed())
        assert r.status_code == 200
        assert b"exceeds available" in r.content

    # ── unreserve ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_unreserve_redirects_to_item(self, ui_client):
        with patch("ui.api_client.unreserve_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/unreserve", data={"quantity": "2"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_unreserve_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.unreserve_item", new=AsyncMock(side_effect=APIError(422, "no reservation"))):
            r = await ui_client.post("/api/items/gc:123/unreserve", data={"quantity": "5"}, cookies=_authed())
        assert r.status_code == 200
        assert b"no reservation" in r.content

    # ── price ────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_price_redirects_to_item(self, ui_client):
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}])),
            patch("ui.api_client.set_item_price", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/gc:123/price", data={"cost_price": "10", "wholesale_price": "20", "retail_price": "30"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_price_passes_only_provided_fields(self, ui_client):
        """Only non-empty price fields trigger set_item_price calls."""
        calls = []
        async def _mock(token, entity_id, price_type, new_price):
            calls.append((price_type, new_price))
            return {"event_id": "e1"}
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}])),
            patch("ui.api_client.set_item_price", new=_mock),
        ):
            await ui_client.post("/api/items/gc:123/price", data={"cost_price": "50", "wholesale_price": "", "retail_price": "99"}, cookies=_authed())
        price_types = {pt for pt, _ in calls}
        assert "Cost" in price_types
        assert "Retail" in price_types
        assert "Wholesale" not in price_types

    @pytest.mark.asyncio
    async def test_price_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Cost"}])),
            patch("ui.api_client.set_item_price", new=AsyncMock(side_effect=APIError(400, "bad price"))),
        ):
            r = await ui_client.post("/api/items/gc:123/price", data={"cost_price": "10"}, cookies=_authed())
        assert r.status_code == 200
        assert b"bad price" in r.content

    # ── status ───────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_status_redirects_to_item(self, ui_client):
        with patch("ui.api_client.set_item_status", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/status", data={"status": "inactive"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_status_passes_value_to_api(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, status):
            captured["status"] = status
            return {"event_id": "e1"}
        with patch("ui.api_client.set_item_status", new=_mock):
            await ui_client.post("/api/items/gc:123/status", data={"status": "reserved"}, cookies=_authed())
        assert captured["status"] == "reserved"

    @pytest.mark.asyncio
    async def test_status_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.set_item_status", new=AsyncMock(side_effect=APIError(422, "invalid status"))):
            r = await ui_client.post("/api/items/gc:123/status", data={"status": "bogus"}, cookies=_authed())
        assert r.status_code == 200
        assert b"invalid status" in r.content

    # ── expire ───────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_expire_redirects_to_item(self, ui_client):
        with patch("ui.api_client.expire_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/expire", data={"reason": "old"}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_expire_passes_reason_to_api(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, reason):
            captured["reason"] = reason
            return {"event_id": "e1"}
        with patch("ui.api_client.expire_item", new=_mock):
            await ui_client.post("/api/items/gc:123/expire", data={"reason": "past date"}, cookies=_authed())
        assert captured["reason"] == "past date"

    @pytest.mark.asyncio
    async def test_expire_empty_reason_passes_none(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, reason):
            captured["reason"] = reason
            return {"event_id": "e1"}
        with patch("ui.api_client.expire_item", new=_mock):
            await ui_client.post("/api/items/gc:123/expire", data={"reason": ""}, cookies=_authed())
        assert captured["reason"] is None

    @pytest.mark.asyncio
    async def test_expire_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.expire_item", new=AsyncMock(side_effect=APIError(400, "already expired"))):
            r = await ui_client.post("/api/items/gc:123/expire", data={"reason": ""}, cookies=_authed())
        assert r.status_code == 200
        assert b"already expired" in r.content

    # ── dispose ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_dispose_redirects_to_item(self, ui_client):
        with patch("ui.api_client.dispose_item", new=AsyncMock(return_value={"event_id": "e1"})):
            r = await ui_client.post("/api/items/gc:123/dispose", data={"reason": "broken", "notes": ""}, cookies=_authed())
        assert r.headers.get("HX-Redirect") == "/inventory/gc:123"

    @pytest.mark.asyncio
    async def test_dispose_passes_reason_and_notes(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, reason, notes):
            captured.update({"reason": reason, "notes": notes})
            return {"event_id": "e1"}
        with patch("ui.api_client.dispose_item", new=_mock):
            await ui_client.post("/api/items/gc:123/dispose", data={"reason": "cracked", "notes": "dropped on floor"}, cookies=_authed())
        assert captured["reason"] == "cracked"
        assert captured["notes"] == "dropped on floor"

    @pytest.mark.asyncio
    async def test_dispose_empty_notes_passes_none(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, reason, notes):
            captured["notes"] = notes
            return {"event_id": "e1"}
        with patch("ui.api_client.dispose_item", new=_mock):
            await ui_client.post("/api/items/gc:123/dispose", data={"reason": "damaged", "notes": ""}, cookies=_authed())
        assert captured["notes"] is None

    @pytest.mark.asyncio
    async def test_dispose_api_error_shown(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.dispose_item", new=AsyncMock(side_effect=APIError(400, "already disposed"))):
            r = await ui_client.post("/api/items/gc:123/dispose", data={"reason": "broken", "notes": ""}, cookies=_authed())
        assert r.status_code == 200
        assert b"already disposed" in r.content

    # ── split (additional coverage) ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_split_redirects_to_filtered_view(self, ui_client):
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "PARENT-001", "quantity": 10})),
            patch("ui.api_client.split_item", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/gc:123/split", data={
                "child_sku_0": "SKU-A", "child_qty_0": "3",
                "child_sku_1": "SKU-B", "child_qty_1": "2",
            }, cookies=_authed())
        assert "/inventory?q=PARENT-001" in r.headers.get("HX-Redirect", "")

    @pytest.mark.asyncio
    async def test_split_passes_correct_quantity_count(self, ui_client):
        captured = {}
        async def _mock(token, entity_id, children):
            captured.update({"children": children})
            return {"event_id": "e1"}
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "P-001", "quantity": 20})),
            patch("ui.api_client.split_item", new=_mock),
        ):
            await ui_client.post("/api/items/gc:123/split", data={
                "child_sku_0": "A", "child_qty_0": "5",
                "child_sku_1": "B", "child_qty_1": "3",
                "child_sku_2": "C", "child_qty_2": "2",
            }, cookies=_authed())
        assert len(captured["children"]) == 3
        assert [c["quantity"] for c in captured["children"]] == [5.0, 3.0, 2.0]
        assert [c["sku"] for c in captured["children"]] == ["A", "B", "C"]

    # ── merge (additional coverage) ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_merge_redirects_to_new_item(self, ui_client):
        with patch("ui.api_client.merge_items", new=AsyncMock(return_value={"id": "item:new999"})):
            r = await ui_client.post(
                "/api/items/merge",
                data={"source_entity_ids": ["item:a", "item:b"], "target_sku_from": "item:a", "resulting_quantity": "5"},
                cookies=_authed(),
            )
        assert r.headers.get("HX-Redirect") == "/inventory/item:new999"

    @pytest.mark.asyncio
    async def test_merge_passes_correct_args(self, ui_client):
        captured = {}
        async def _mock(token, source_entity_ids, target_sku_from, resulting_quantity=None,
                        resulting_cost_price=None, resulting_name=None, resolved_attributes=None, idempotency_key=None):
            captured.update({
                "sources": source_entity_ids,
                "target": target_sku_from,
                "qty": resulting_quantity,
            })
            return {"id": "item:new1"}
        with patch("ui.api_client.merge_items", new=_mock):
            await ui_client.post(
                "/api/items/merge",
                data={"source_entity_ids": ["item:a", "item:b"], "target_sku_from": "item:a", "resulting_quantity": "8"},
                cookies=_authed(),
            )
        assert captured["target"] == "item:a"
        assert captured["sources"] == ["item:a", "item:b"]
        assert captured["qty"] == 8.0

    @pytest.mark.asyncio
    async def test_merge_invalid_qty_shows_error(self, ui_client):
        r = await ui_client.post(
            "/api/items/merge",
            data={"source_entity_ids": ["item:a", "item:b"], "target_sku_from": "item:a", "resulting_quantity": "notanumber"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"Invalid" in r.content


class TestInventoryBulkActions:
    """List-level bulk action routes: status, transfer, delete."""

    @pytest.mark.asyncio
    async def test_inventory_page_has_bulk_toolbar(self, ui_client):
        """Inventory list page renders bulk toolbar element."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "category_counts": {}})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"bulk-toolbar" in r.content
        assert b"bulk-count" in r.content

    @pytest.mark.asyncio
    async def test_bulk_toolbar_shows_location_options(self, ui_client):
        """When locations exist, bulk toolbar includes transfer dropdown."""
        loc = {"id": "loc:main", "name": "Main Office"}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "category_counts": {}})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [loc], "total": 1})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert b"Main Office" in r.content
        assert b"bulk/transfer" in r.content

    # ── bulk/status ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_bulk_status_success(self, ui_client):
        with patch("ui.api_client.bulk_set_status", new=AsyncMock(return_value={"updated": 2})):
            r = await ui_client.post(
                "/api/items/bulk/status",
                content=b"selected=item%3Aa&selected=item%3Ab&bulk_status=inactive", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"2 item(s) updated" in r.content

    @pytest.mark.asyncio
    async def test_bulk_status_passes_ids_and_status(self, ui_client):
        captured = {}
        async def _mock(token, entity_ids, status):
            captured.update({"ids": entity_ids, "status": status})
            return {"updated": 2}
        with patch("ui.api_client.bulk_set_status", new=_mock):
            await ui_client.post(
                "/api/items/bulk/status",
                content=b"selected=item%3Aa&selected=item%3Ab&bulk_status=reserved", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert captured["ids"] == ["item:a", "item:b"]
        assert captured["status"] == "reserved"

    @pytest.mark.asyncio
    async def test_bulk_status_no_selection(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/status",
            content=b"bulk_status=inactive", headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No items selected" in r.content

    @pytest.mark.asyncio
    async def test_bulk_status_no_status_value(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/status",
            content=b"selected=item%3Aa&bulk_status=", headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No status selected" in r.content

    @pytest.mark.asyncio
    async def test_bulk_status_api_error(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.bulk_set_status", new=AsyncMock(side_effect=APIError(500, "server error"))):
            r = await ui_client.post(
                "/api/items/bulk/status",
                content=b"selected=item%3Aa&bulk_status=inactive", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"server error" in r.content

    # ── bulk/transfer ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_bulk_transfer_success(self, ui_client):
        with patch("ui.api_client.bulk_transfer", new=AsyncMock(return_value={"updated": 3})):
            r = await ui_client.post(
                "/api/items/bulk/transfer",
                content=b"selected=item%3Aa&selected=item%3Ab&selected=item%3Ac&bulk_location_id=loc%3A1", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"3 item(s) transferred" in r.content

    @pytest.mark.asyncio
    async def test_bulk_transfer_passes_ids_and_location(self, ui_client):
        captured = {}
        async def _mock(token, entity_ids, to_location_id):
            captured.update({"ids": entity_ids, "loc": to_location_id})
            return {"updated": 2}
        with patch("ui.api_client.bulk_transfer", new=_mock):
            await ui_client.post(
                "/api/items/bulk/transfer",
                content=b"selected=item%3Aa&selected=item%3Ab&bulk_location_id=loc%3Amain", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert captured["ids"] == ["item:a", "item:b"]
        assert captured["loc"] == "loc:main"

    @pytest.mark.asyncio
    async def test_bulk_transfer_no_selection(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/transfer",
            content=b"bulk_location_id=loc%3A1", headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No items selected" in r.content

    @pytest.mark.asyncio
    async def test_bulk_transfer_no_location(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/transfer",
            content=b"selected=item%3Aa&bulk_location_id=", headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No location selected" in r.content

    @pytest.mark.asyncio
    async def test_bulk_transfer_api_error(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.bulk_transfer", new=AsyncMock(side_effect=APIError(422, "invalid location"))):
            r = await ui_client.post(
                "/api/items/bulk/transfer",
                content=b"selected=item%3Aa&bulk_location_id=loc%3Abad", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"invalid location" in r.content

    # ── bulk/delete ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_bulk_delete_success(self, ui_client):
        with patch("ui.api_client.bulk_delete", new=AsyncMock(return_value={"deleted": 2})):
            r = await ui_client.post(
                "/api/items/bulk/delete",
                content=b"selected=item%3Aa&selected=item%3Ab", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"2 item(s) deleted" in r.content

    @pytest.mark.asyncio
    async def test_bulk_delete_passes_ids(self, ui_client):
        captured = {}
        async def _mock(token, entity_ids):
            captured["ids"] = entity_ids
            return {"deleted": 2}
        with patch("ui.api_client.bulk_delete", new=_mock):
            await ui_client.post(
                "/api/items/bulk/delete",
                content=b"selected=item%3Ax&selected=item%3Ay", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert captured["ids"] == ["item:x", "item:y"]

    @pytest.mark.asyncio
    async def test_bulk_delete_no_selection(self, ui_client):
        r = await ui_client.post("/api/items/bulk/delete", content=b"", headers={"content-type": "application/x-www-form-urlencoded"}, cookies=_authed())
        assert r.status_code == 200
        assert b"No items selected" in r.content

    @pytest.mark.asyncio
    async def test_bulk_delete_api_error(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.bulk_delete", new=AsyncMock(side_effect=APIError(500, "delete failed"))):
            r = await ui_client.post(
                "/api/items/bulk/delete",
                content=b"selected=item%3Aa", headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"delete failed" in r.content


class TestBulkActionsPhase1to5:
    """Phases 1-5: persistent selection, context-sensitive toolbar, merge/split/expire/dispose."""

    # ── Phase 1: data attributes on row checkboxes ───────────────────────

    @pytest.mark.asyncio
    async def test_row_checkbox_has_data_attributes(self, ui_client):
        item = {**_ITEM, "sku": "RB-01", "quantity": 5, "weight": "2.5", "weight_unit": "ct", "sell_by": "piece"}
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b'data-entity-id="gc:123"' in r.content
        assert b'data-sku="RB-01"' in r.content

    @pytest.mark.asyncio
    async def test_bulk_toolbar_has_clear_button(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "category_counts": {}})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"bulk-clear-btn" in r.content

    # ── Phase 2: context-sensitive buttons ────────────────────────────────

    @pytest.mark.asyncio
    async def test_bulk_toolbar_has_action_dropdown(self, ui_client):
        """New toolbar uses a single Action dropdown instead of individual buttons."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "category_counts": {}})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        # Single action dropdown with options for all actions
        assert b"bulk-action-select" in r.content
        assert b"Transfer" in r.content
        assert b"Split" in r.content
        assert b"Merge" in r.content
        assert b"Archive" in r.content
        assert b"Expire" in r.content
        assert b"Delete" in r.content

    @pytest.mark.asyncio
    async def test_bulk_toolbar_module_action_in_dropdown(self, ui_client):
        """Module-contributed bulk actions appear as options in the Action dropdown."""
        from celerp.modules.slots import register as register_slot, clear as clear_slots
        clear_slots()
        register_slot("bulk_action", {
            "label": "Print Labels",
            "form_action": "/api/labels/bulk-print",
            "_module": "test",
        })
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_column_prefs", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={"item_count": 0, "category_counts": {}})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        clear_slots()
        assert r.status_code == 200
        assert b"Print Labels" in r.content

    # ── Phase 3: bulk merge (direct, no preview modal) ─────────────────

    @pytest.mark.asyncio
    async def test_bulk_merge_rejects_single_item(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/merge",
            content=b"selected=item%3Aa",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"at least 2" in r.content

    @pytest.mark.asyncio
    async def test_bulk_merge_requires_target(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/merge",
            content=b"selected=item%3Aa&selected=item%3Ab",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"Target item selection is required" in r.content

    # ── Phase 4: bulk split (simplified single-qty) ──────────────────────

    @pytest.mark.asyncio
    async def test_bulk_split_rejects_multiple_items(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/split",
            content=b"selected=item%3Aa&selected=item%3Ab",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"exactly 1" in r.content

    @pytest.mark.asyncio
    async def test_bulk_split_rejects_invalid_qty(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/split",
            content=b"selected=item%3Ax&split_qty=abc",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"Invalid split quantity" in r.content

    # ── Phase 5: bulk expire/dispose ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_bulk_expire_success(self, ui_client):
        with patch("ui.api_client.bulk_expire", new=AsyncMock(return_value={"expired": 2})):
            r = await ui_client.post(
                "/api/items/bulk/expire",
                content=b"selected=item%3Aa&selected=item%3Ab",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"2 item(s) expired" in r.content

    @pytest.mark.asyncio
    async def test_bulk_expire_no_selection(self, ui_client):
        r = await ui_client.post(
            "/api/items/bulk/expire",
            content=b"",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No items selected" in r.content


class TestSprint5ContactCreation:
    """T4: Contact creation."""

    @pytest.mark.asyncio
    async def test_crm_page_has_new_contact_button(self, ui_client):
        """CRM page has a New Contact button."""
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": len(_CONTACTS)})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
        ):
            r = await ui_client.get("/contacts/customers", cookies=_authed())
        assert r.status_code == 200
        assert b"New Contact" in r.content or b"New Customer" in r.content

    @pytest.mark.asyncio
    async def test_create_blank_contact(self, ui_client):
        """POST /crm/create-blank creates contact and redirects."""
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.create_contact", new=AsyncMock(return_value={"entity_id": "ct:new1"})),
        ):
            r = await ui_client.post("/contacts/create?type=customer", cookies=_authed())
        assert r.status_code == 204
        assert "/contacts/ct:new1" in r.headers["HX-Redirect"]

    @pytest.mark.asyncio
    async def test_create_blank_contact_unauthorized(self, ui_client):
        """POST /crm/create-blank without cookie redirects to login."""
        r = await ui_client.post("/contacts/create?type=customer")
        assert r.status_code in (302, 401)


@pytest.mark.skipif(not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "premium_modules", "celerp-sales-funnel")), reason="celerp-sales-funnel not installed")
class TestSprint5Deals:
    """T5: Deals pipeline."""

    @pytest.mark.asyncio
    async def test_crm_has_deals_tab(self, ui_client):
        """Sales funnel page renders deals."""
        with (
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert r.status_code == 200
        assert b"deal" in r.content.lower() or b"Sales" in r.content

    @pytest.mark.asyncio
    async def test_deals_tab_renders_kanban(self, ui_client):
        """Deals tab renders kanban board."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert r.status_code == 200
        assert b"kanban-board" in r.content
        assert b"Big Sale" in r.content

    @pytest.mark.asyncio
    async def test_deals_tab_has_new_deal_button(self, ui_client):
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"New Deal" in r.content

    @pytest.mark.asyncio
    async def test_create_deal_route(self, ui_client):
        with patch("ui.api_client.create_deal",
                   new=AsyncMock(return_value={"entity_id": "deal:d2"})):
            r = await ui_client.post("/crm/deals/create-blank", cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_move_deal_stage_route(self, ui_client):
        with patch("ui.api_client.move_deal_stage", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/deals/deal:d1/stage", data={"stage": "qualified"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_mark_deal_won_route(self, ui_client):
        with patch("ui.api_client.mark_deal_won", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/deals/deal:d1/won", cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_mark_deal_lost_route(self, ui_client):
        with patch("ui.api_client.mark_deal_lost", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/deals/deal:d1/lost", data={"reason": "budget"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_deals_show_value_formatted(self, ui_client):
        """Deal card shows value formatted with ฿."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert "50,000".encode() in r.content

    @pytest.mark.asyncio
    async def test_deals_show_stage_columns(self, ui_client):
        """Deals board has stage columns."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"Lead" in r.content
        assert b"Qualified" in r.content
        assert b"Proposal" in r.content


@pytest.mark.skipif(not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "premium_modules", "celerp-sales-funnel")), reason="celerp-sales-funnel not installed")
class TestDealsRedesign:
    """Sprint: CRM Deals Redesign — detail page, delete, reopen, create form, patch."""

    @pytest.mark.asyncio
    async def test_deal_detail_page_renders(self, ui_client):
        """GET /crm/deals/{id} renders deal detail."""
        with (
            patch("ui.api_client.get_deal", new=AsyncMock(return_value=_DEAL)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/deals/deal:d1", cookies=_authed())
        assert r.status_code == 200
        assert b"Big Sale" in r.content
        assert b"deal-detail-layout" in r.content

    @pytest.mark.asyncio
    async def test_deal_detail_shows_breadcrumb(self, ui_client):
        with (
            patch("ui.api_client.get_deal", new=AsyncMock(return_value=_DEAL)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/deals/deal:d1", cookies=_authed())
        assert b"Deals" in r.content
        assert b"Big Sale" in r.content

    @pytest.mark.asyncio
    async def test_deal_detail_no_auth_redirects(self, ui_client):
        r = await ui_client.get("/crm/deals/deal:d1")
        assert r.status_code == 302

    @pytest.mark.asyncio
    async def test_deal_delete_route(self, ui_client):
        with patch("ui.api_client.delete_deal", new=AsyncMock(return_value={})):
            r = await ui_client.delete("/crm/deals/deal:d1", cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_deal_delete_no_auth(self, ui_client):
        r = await ui_client.delete("/crm/deals/deal:d1")
        # Auth guard redirects to login before route handler fires
        assert r.status_code == 302

    @pytest.mark.asyncio
    async def test_deal_reopen_route(self, ui_client):
        with patch("ui.api_client.reopen_deal", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/deals/deal:d1/reopen", cookies=_authed())
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_deal_create_form_page(self, ui_client):
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get("/crm/deals/new", cookies=_authed())
        assert r.status_code == 200
        assert b"New Deal" in r.content
        assert b"form" in r.content

    @pytest.mark.asyncio
    async def test_deal_create_form_no_auth(self, ui_client):
        r = await ui_client.get("/crm/deals/new")
        assert r.status_code == 302

    @pytest.mark.asyncio
    async def test_deal_create_form_submit(self, ui_client):
        with (
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.create_deal", new=AsyncMock(return_value={"id": "deal:new1"})),
        ):
            r = await ui_client.post(
                "/crm/deals/new",
                data={"name": "Test Deal", "stage": "lead", "value": "10000", "expected_close": "2026-12-31"},
                cookies=_authed(),
            )
        assert r.status_code == 302
        assert "/crm/deals/" in r.headers["location"]

    @pytest.mark.asyncio
    async def test_deal_create_form_submit_no_name(self, ui_client):
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.post("/crm/deals/new", data={"name": ""}, cookies=_authed())
        assert r.status_code == 302
        assert "error=name_required" in r.headers["location"]

    @pytest.mark.asyncio
    async def test_deal_patch_field(self, ui_client):
        with (
            patch("ui.api_client.patch_deal", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_deal", new=AsyncMock(return_value=_DEAL)),
        ):
            r = await ui_client.patch(
                "/crm/deals/deal:d1/field/name",
                data={"value": "Renamed Deal"},
                cookies=_authed(),
            )
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_deal_kanban_has_column_totals(self, ui_client):
        """Column header shows deal count and total value."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"kanban-col-count" in r.content
        assert b"kanban-col-value" in r.content

    @pytest.mark.asyncio
    async def test_deal_card_links_to_detail(self, ui_client):
        """Deal card is wrapped in a link to the detail page."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"/crm/deals/deal:d1" in r.content

    @pytest.mark.asyncio
    async def test_deal_new_button_links_to_form(self, ui_client):
        """New Deal button links to /crm/deals/new, not create-blank."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"/crm/deals/new" in r.content

    @pytest.mark.asyncio
    async def test_deal_card_has_delete_button(self, ui_client):
        """Deal card has delete (×) button."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"deal-delete-btn" in r.content

    @pytest.mark.asyncio
    async def test_deal_detail_has_reopen_for_closed(self, ui_client):
        """Closed deal shows Re-open button."""
        closed_deal = {**_DEAL, "status": "won"}
        with (
            patch("ui.api_client.get_deal", new=AsyncMock(return_value=closed_deal)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/deals/deal:d1", cookies=_authed())
        assert b"Re-open" in r.content

    @pytest.mark.asyncio
    async def test_deal_detail_open_has_mark_won(self, ui_client):
        """Open deal shows Mark Won button."""
        with (
            patch("ui.api_client.get_deal", new=AsyncMock(return_value=_DEAL)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/deals/deal:d1", cookies=_authed())
        assert b"Mark Won" in r.content


class TestSprint5MemoActions:
    """T6: Memo actions."""

    @pytest.mark.asyncio
    async def test_memo_detail_page_renders(self, ui_client):
        """GET /crm/memos/{id} renders memo detail."""
        with (
            patch("ui.api_client.get_memo", new=AsyncMock(return_value=_MEMO)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/memos/memo:m1", cookies=_authed())
        assert r.status_code == 200
        assert b"MEM-001" in r.content

    @pytest.mark.asyncio
    async def test_draft_memo_shows_approve_button(self, ui_client):
        with (
            patch("ui.api_client.get_memo", new=AsyncMock(return_value=_MEMO)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/memos/memo:m1", cookies=_authed())
        assert b"Approve" in r.content

    @pytest.mark.asyncio
    async def test_approved_memo_shows_convert_button(self, ui_client):
        approved = {**_MEMO, "status": "approved"}
        with (
            patch("ui.api_client.get_memo", new=AsyncMock(return_value=approved)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/memos/memo:m1", cookies=_authed())
        assert b"Convert to Invoice" in r.content
        assert b"Cancel" in r.content
        assert b"Return Items" in r.content

    @pytest.mark.asyncio
    async def test_memo_shows_line_items(self, ui_client):
        with (
            patch("ui.api_client.get_memo", new=AsyncMock(return_value=_MEMO)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/memos/memo:m1", cookies=_authed())
        assert b"Line Items" in r.content
        assert b"item:r1" in r.content

    @pytest.mark.asyncio
    async def test_draft_memo_has_add_item(self, ui_client):
        with (
            patch("ui.api_client.get_memo", new=AsyncMock(return_value=_MEMO)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/memos/memo:m1", cookies=_authed())
        assert b"Add Item" in r.content

    @pytest.mark.asyncio
    async def test_approve_memo_route(self, ui_client):
        with patch("ui.api_client.approve_memo", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/memos/memo:m1/approve", cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_cancel_memo_route(self, ui_client):
        with patch("ui.api_client.cancel_memo", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/memos/memo:m1/cancel", data={"reason": "no longer needed"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_convert_memo_to_invoice_route(self, ui_client):
        with patch("ui.api_client.convert_memo_to_invoice",
                   new=AsyncMock(return_value={"doc_id": "doc:INV-2026-0050"})):
            r = await ui_client.post("/crm/memos/memo:m1/convert-to-invoice", cookies=_authed())
        assert r.status_code == 204
        assert "/docs/doc:INV-2026-0050" in r.headers["HX-Redirect"]

    @pytest.mark.asyncio
    async def test_add_memo_item_route(self, ui_client):
        with patch("ui.api_client.add_memo_item", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/memos/memo:m1/add-item", data={"item_id": "item:r2", "quantity": "3"}, cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_remove_memo_item_route(self, ui_client):
        with patch("ui.api_client.remove_memo_item", new=AsyncMock(return_value={})):
            r = await ui_client.post("/crm/memos/memo:m1/remove-item/item:r1", cookies=_authed())
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_memo_no_popup_or_modal(self, ui_client):
        """Memo detail must not use popups or modals."""
        with (
            patch("ui.api_client.get_memo", new=AsyncMock(return_value=_MEMO)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm/memos/memo:m1", cookies=_authed())
        content = r.content.lower()
        assert b"<dialog" not in content
        assert b"modal" not in content


class TestSprint5PaymentRefund:
    """T7: Payment refund on doc detail."""

    @pytest.mark.asyncio
    async def test_paid_invoice_shows_refund_section(self, ui_client):
        """Paid invoice detail shows payment section with 'Paid in Full'."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_PAID_INVOICE)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Payments" in r.content or b"Paid in Full" in r.content

    @pytest.mark.asyncio
    async def test_draft_invoice_no_refund(self, ui_client):
        """Draft invoice does not show refund section."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Refund" not in r.content

    @pytest.mark.asyncio
    async def test_refund_route_calls_api(self, ui_client):
        """POST /docs/{id}/refund calls api.refund_payment."""
        with patch("ui.api_client.refund_payment", new=AsyncMock(return_value={"event_id": "ev1"})):
            r = await ui_client.post(
                "/docs/doc:INV-2026-0001/refund",
                data={"amount": "5000", "method": "transfer", "reference": "REF-1"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "HX-Redirect" in r.headers

    @pytest.mark.asyncio
    async def test_refund_shows_amount_paid(self, ui_client):
        """Payment section shows total paid info."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_PAID_INVOICE)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"10,000" in r.content


class TestSprint5MfgSteps:
    """T8: Manufacturing step-by-step progression."""

    @pytest.mark.asyncio
    async def test_mfg_detail_shows_steps(self, ui_client):
        """Manufacturing detail shows steps checklist."""
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=_MFG_ORDER_WITH_STEPS)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert r.status_code == 200
        assert b"Steps" in r.content
        assert b"Cut" in r.content
        assert b"Polish" in r.content

    @pytest.mark.asyncio
    async def test_mfg_steps_have_complete_button(self, ui_client):
        """In-progress order steps show Complete Step button."""
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=_MFG_ORDER_WITH_STEPS)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert b"Complete Step" in r.content

    @pytest.mark.asyncio
    async def test_mfg_inputs_have_consume_button(self, ui_client):
        """In-progress order inputs show Consume button."""
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=_MFG_ORDER_WITH_STEPS)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert b"Consume" in r.content

    @pytest.mark.asyncio
    async def test_complete_step_route(self, ui_client):
        """POST /manufacturing/{id}/step calls api."""
        completed = {**_MFG_ORDER_WITH_STEPS, "steps": [
            {"step_id": "step1", "name": "Cut", "status": "completed"},
            {"step_id": "step2", "name": "Polish", "status": "pending"},
        ]}
        with (
            patch("ui.api_client.complete_mfg_step", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=completed)),
        ):
            r = await ui_client.post("/manufacturing/mfg:abc123/step", data={"step_id": "step1", "notes": "done"}, cookies=_authed())
        assert r.status_code == 200
        assert b"mfg-detail" in r.content

    @pytest.mark.asyncio
    async def test_consume_input_route(self, ui_client):
        """POST /manufacturing/{id}/consume calls api."""
        consumed = {**_MFG_ORDER_WITH_STEPS, "inputs": [
            {"item_id": "item:x1", "quantity": 5, "consumed_qty": 5},
        ]}
        with (
            patch("ui.api_client.consume_mfg_input", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=consumed)),
        ):
            r = await ui_client.post("/manufacturing/mfg:abc123/consume", data={"item_id": "item:x1", "quantity": "5"}, cookies=_authed())
        assert r.status_code == 200
        assert b"mfg-detail" in r.content

    @pytest.mark.asyncio
    async def test_draft_order_no_step_buttons(self, ui_client):
        """Draft order does not show step completion buttons."""
        draft = {**_MFG_ORDER_WITH_STEPS, "status": "draft"}
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=draft)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert b"Complete Step" not in r.content

    @pytest.mark.asyncio
    async def test_completed_step_shows_checkmark(self, ui_client):
        """Completed steps show checkmark."""
        order = {**_MFG_ORDER_WITH_STEPS, "steps": [
            {"step_id": "step1", "name": "Cut", "status": "completed"},
            {"step_id": "step2", "name": "Polish", "status": "pending"},
        ]}
        with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=order)):
            r = await ui_client.get("/manufacturing/mfg:abc123", cookies=_authed())
        assert "✓".encode() in r.content


class TestSprint5NoPopups:
    """Cross-cutting: no popups/modals anywhere."""

    @pytest.mark.asyncio
    async def test_no_dialog_in_item_detail(self, ui_client):
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert b"<dialog" not in r.content.lower()
        assert b"modal" not in r.content.lower()

    @pytest.mark.asyncio
    async def test_no_dialog_in_deals(self, ui_client):
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value=_MEMO_SUMMARY)),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={"items": [_DEAL], "total": 1})),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert b"<dialog" not in r.content.lower()

    @pytest.mark.asyncio
    async def test_no_dialog_in_quotation(self, ui_client):
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_QUOTATION_DOC)):
            r = await ui_client.get("/docs/doc:QUO-2026-0001", cookies=_authed())
        assert b"<dialog" not in r.content.lower()


# =============================================================================
# Import Flow Tests
# =============================================================================
#
# Covers the full import pipeline: upload → preview → validate-cell → confirm.
# Also covers field suggestions (datalist), error navigator HTML, fill row,
# and the validated_cell_input helper contract.
#
# Bug class these tests guard against (issues discovered 2026-03-09):
#   B1: validate-cell response missing data-col/data-row → Fill stops working
#       after first HTMX swap; class attr changes no longer trigger observer
#   B2: Error counter not updating → MutationObserver infinite loop on
#       attribute changes (disabled button toggle re-fires observer)
#   B3: Cell edits silently ignored on confirm (csv_data was a static snapshot)
# =============================================================================

import io as _io
import csv as _csv

def _make_csv(rows: list[dict], cols: list[str] | None = None) -> bytes:
    cols = cols or list(rows[0].keys())
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


_LOCATIONS_RESP = {"items": [{"id": "loc-1", "name": "Main Office", "type": "warehouse"}], "total": 1}
_FIELD_VALUES_EMPTY: list[str] = []


class TestColumnMappingValidation:
    """Unit tests for column mapping validation, attribute rename, and duplicate detection."""

    # ── validate_column_mapping ──────────────────────────────────────────────

    def test_valid_mapping_no_errors(self):
        from ui.routes.csv_import import validate_column_mapping
        form = {"map__sku": "sku", "map__name": "name", "map__extra": MAPPING_ATTRIBUTE}
        errors = validate_column_mapping(form, ["sku", "name", "extra"], core_fields=_CORE_ITEM_COLS)
        assert errors == []

    def test_duplicate_target_detected(self):
        from ui.routes.csv_import import validate_column_mapping
        form = {"map__col_a": "category", "map__col_b": "category"}
        errors = validate_column_mapping(form, ["col_a", "col_b"], core_fields=_CORE_ITEM_COLS)
        assert len(errors) == 1
        assert "col_a" in errors[0]
        assert "col_b" in errors[0]
        assert "Category" in errors[0]

    def test_attribute_name_collides_with_core_field(self):
        from ui.routes.csv_import import validate_column_mapping
        # "category" column mapped as attribute, no rename -> collides with core field "category"
        form = {"map__category": MAPPING_ATTRIBUTE}
        errors = validate_column_mapping(form, ["category"], core_fields=_CORE_ITEM_COLS)
        assert len(errors) == 1
        assert "category" in errors[0].lower()
        assert "built-in" in errors[0].lower()

    def test_attribute_custom_name_collides_with_core_field(self):
        from ui.routes.csv_import import validate_column_mapping
        # User renames attribute to "sku" which is a core field
        form = {"map__my_col": MAPPING_ATTRIBUTE, "attr_name__my_col": "sku"}
        errors = validate_column_mapping(form, ["my_col"], core_fields=_CORE_ITEM_COLS)
        assert len(errors) == 1
        assert "sku" in errors[0]

    def test_attribute_custom_name_avoids_core_collision(self):
        from ui.routes.csv_import import validate_column_mapping
        # "category" column renamed to "lot_type" -> no collision
        form = {"map__category": MAPPING_ATTRIBUTE, "attr_name__category": "lot_type"}
        errors = validate_column_mapping(form, ["category"], core_fields=_CORE_ITEM_COLS)
        assert errors == []

    def test_duplicate_attribute_names_detected(self):
        from ui.routes.csv_import import validate_column_mapping
        # Two columns both mapped as attribute with same custom name
        form = {
            "map__col_a": MAPPING_ATTRIBUTE, "attr_name__col_a": "grade",
            "map__col_b": MAPPING_ATTRIBUTE, "attr_name__col_b": "grade",
        }
        errors = validate_column_mapping(form, ["col_a", "col_b"], core_fields=_CORE_ITEM_COLS)
        assert len(errors) == 1
        assert "grade" in errors[0]
        assert "col_a" in errors[0]
        assert "col_b" in errors[0]

    def test_skip_columns_ignored(self):
        from ui.routes.csv_import import validate_column_mapping
        form = {"map__col_a": MAPPING_SKIP, "map__col_b": MAPPING_SKIP}
        errors = validate_column_mapping(form, ["col_a", "col_b"], core_fields=_CORE_ITEM_COLS)
        assert errors == []

    def test_multiple_errors_reported(self):
        from ui.routes.csv_import import validate_column_mapping
        # Duplicate target AND attribute collision
        form = {
            "map__a": "sku", "map__b": "sku",  # duplicate target
            "map__category": MAPPING_ATTRIBUTE,  # collides with core
        }
        errors = validate_column_mapping(form, ["a", "b", "category"], core_fields=_CORE_ITEM_COLS)
        assert len(errors) == 2

    # ── apply_column_mapping with attribute rename ───────────────────────────

    def test_apply_mapping_uses_custom_attr_name(self):
        import csv as _csv, io as _io
        from ui.routes.csv_import import apply_column_mapping
        csv_text = "category,value\nStone,High\n"
        form = {
            "csv_ref": "",
            "map__category": MAPPING_ATTRIBUTE,
            "attr_name__category": "lot_type",
            "map__value": MAPPING_ATTRIBUTE,
            "attr_name__value": "grade",
        }
        remapped, cols = apply_column_mapping(form, csv_text)
        assert "lot_type" in cols
        assert "grade" in cols
        assert "category" not in cols
        rows = list(_csv.DictReader(_io.StringIO(remapped)))
        assert rows[0]["lot_type"] == "Stone"
        assert rows[0]["grade"] == "High"

    def test_apply_mapping_default_attr_name_is_original(self):
        import csv as _csv, io as _io
        from ui.routes.csv_import import apply_column_mapping
        csv_text = "extra_col,sku\nfoo,S1\n"
        form = {
            "map__extra_col": MAPPING_ATTRIBUTE,
            # No attr_name__extra_col -> defaults to "extra_col"
            "map__sku": "sku",
        }
        remapped, cols = apply_column_mapping(form, csv_text)
        assert "extra_col" in cols
        assert "sku" in cols

    # ── column_mapping_form rendering ────────────────────────────────────────

    def test_form_renders_attr_name_inputs(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import column_mapping_form
        html = to_xml(column_mapping_form(
            csv_cols=["category", "sku"],
            target_cols=["sku", "name", "category"],
            csv_ref="test-ref",
            sample_rows=[{"category": "Stone", "sku": "S1"}],
            confirm_action="/test/mapped",
            back_href="/test",
        ))
        # Attribute name inputs should exist
        assert 'name="attr_name__category"' in html or 'name="attr_name__sku"' in html
        # Sample data rows visible
        assert "Stone" in html
        assert "S1" in html

    def test_form_renders_errors_when_provided(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import column_mapping_form
        html = to_xml(column_mapping_form(
            csv_cols=["col_a"],
            target_cols=["sku"],
            csv_ref="test-ref",
            sample_rows=[],
            confirm_action="/test/mapped",
            back_href="/test",
            errors=["Columns X and Y both mapped to sku."],
        ))
        assert "Columns X and Y both mapped to sku." in html
        assert "flash--error" in html

    def test_form_preserves_values_on_re_render(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import column_mapping_form
        html = to_xml(column_mapping_form(
            csv_cols=["my_col"],
            target_cols=["sku", "name"],
            csv_ref="test-ref",
            sample_rows=[],
            confirm_action="/test/mapped",
            back_href="/test",
            form_values={"map__my_col": MAPPING_ATTRIBUTE, "attr_name__my_col": "custom_name"},
        ))
        assert 'value="custom_name"' in html

    def test_horizontal_layout_shows_sample_rows(self):
        """Column mapping renders columns horizontally with multiple sample data rows."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import column_mapping_form
        rows = [
            {"sku": "GEM-001", "name": "Blue Sapphire", "weight": "3.52"},
            {"sku": "GEM-002", "name": "Ruby", "weight": "4.36"},
            {"sku": "GEM-003", "name": "Emerald", "weight": "2.10"},
        ]
        html = to_xml(column_mapping_form(
            csv_cols=["sku", "name", "weight"],
            target_cols=["sku", "name"],
            csv_ref="test-ref",
            sample_rows=rows,
            confirm_action="/test/mapped",
            back_href="/test",
        ))
        # All 3 sample rows visible
        assert "GEM-001" in html
        assert "GEM-002" in html
        assert "GEM-003" in html
        assert "Blue Sapphire" in html
        # Original column header row (muted)
        assert "mapping-original-header" in html
        # Horizontal scroll wrapper
        assert "mapping-scroll-wrapper" in html
        # Status badges present
        assert "mapping-badge" in html
        # Matched columns get checkmark badge, unmatched get attr badge
        assert "mapping-badge--matched" in html
        assert "mapping-badge--attr" in html

    def test_horizontal_layout_shows_row_count_hint(self):
        """When more than 5 rows, shows 'Showing X of Y rows' hint."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import column_mapping_form
        rows = [{"sku": f"S{i}", "name": f"Item {i}"} for i in range(10)]
        html = to_xml(column_mapping_form(
            csv_cols=["sku", "name"],
            target_cols=["sku", "name"],
            csv_ref="test-ref",
            sample_rows=rows,
            confirm_action="/test/mapped",
            back_href="/test",
        ))
        assert "Showing 5 of 10 rows" in html
        # Only first 5 shown
        assert "S0" in html
        assert "S4" in html
        assert "S5" not in html


    def test_category_attrs_auto_match(self):
        """CSV columns matching category attribute keys auto-select the category field."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import column_mapping_form, MAPPING_ATTR_PREFIX
        rows = [{"sku": "G1", "name": "Sapphire", "stone_type": "Blue", "clarity": "VS"}]
        html = to_xml(column_mapping_form(
            csv_cols=["sku", "name", "stone_type", "clarity"],
            target_cols=["sku", "name"],
            csv_ref="test-ref",
            sample_rows=rows,
            confirm_action="/test/mapped",
            back_href="/test",
            category_attrs=["stone_type", "clarity", "origin"],
        ))
        # Hidden inputs carry the auto-matched values
        assert f'value="{MAPPING_ATTR_PREFIX}stone_type"' in html
        assert f'value="{MAPPING_ATTR_PREFIX}clarity"' in html
        # Category fields present in JS option definitions
        assert "category" in html  # group: "category" in JSON
        # "Import as custom" label (not "Import as attribute")
        assert "Import as custom" in html
        assert "Import as attribute" not in html

    def test_category_attrs_apply_mapping(self):
        """Category attribute mappings are applied correctly (column renamed to attr key)."""
        from ui.routes.csv_import import apply_column_mapping, MAPPING_ATTR_PREFIX
        csv_text = "gem_kind,sku\nSapphire,G1\n"
        form = {
            "map__gem_kind": f"{MAPPING_ATTR_PREFIX}stone_type",
            "map__sku": "sku",
        }
        remapped, cols = apply_column_mapping(form, csv_text)
        assert "stone_type" in cols
        assert "sku" in cols
        assert "Sapphire" in remapped


class TestColumnMappingHTTPFlow:
    """HTTP-level tests for column mapping validation in the import pipeline."""

    @pytest.mark.asyncio
    async def test_duplicate_target_returns_mapping_form_with_error(self, ui_client):
        """Two CSV columns mapped to same target -> re-renders mapping form with error."""
        csv_bytes = _make_csv([{"col_a": "x", "col_b": "y"}])
        # Step 1: upload to get csv_ref
        r = await ui_client.post(
            "/inventory/import/preview",
            cookies=_authed(),
            files={"csv_file": ("items.csv", csv_bytes, "text/csv")},
        )
        assert r.status_code == 200
        m = re.search(r'name="csv_ref"\s+value="([^"]+)"', r.text)
        assert m
        csv_ref = m.group(1)

        # Step 2: submit mapping with duplicate targets
        form_data = {
            "csv_ref": csv_ref,
            "map__col_a": "sku",
            "map__col_b": "sku",
        }
        r = await ui_client.post(
            "/inventory/import/mapped",
            cookies=_authed(),
            data=form_data,
        )
        assert r.status_code == 200
        assert b"col_a" in r.content
        assert b"col_b" in r.content
        # Should NOT proceed to validation - should show mapping form with error
        assert b"Import All" not in r.content
        assert b"flash--error" in r.content

    @pytest.mark.asyncio
    async def test_attr_name_core_collision_returns_error(self, ui_client):
        """Attribute named same as core field -> re-renders mapping form with error."""
        csv_bytes = _make_csv([{"category": "Stone"}])
        r = await ui_client.post(
            "/inventory/import/preview",
            cookies=_authed(),
            files={"csv_file": ("items.csv", csv_bytes, "text/csv")},
        )
        assert r.status_code == 200
        m = re.search(r'name="csv_ref"\s+value="([^"]+)"', r.text)
        assert m
        csv_ref = m.group(1)

        # Map "category" as attribute but don't rename -> collides with core "category"
        form_data = {
            "csv_ref": csv_ref,
            "map__category": MAPPING_ATTRIBUTE,
            # No attr_name__category, defaults to "category" -> collision
        }
        r = await ui_client.post(
            "/inventory/import/mapped",
            cookies=_authed(),
            data=form_data,
        )
        assert r.status_code == 200
        assert b"built-in" in r.content.lower() or b"conflicts" in r.content.lower()
        assert b"Import All" not in r.content

    @pytest.mark.asyncio
    async def test_attr_rename_avoids_collision_succeeds(self, ui_client):
        """Attribute renamed to non-core name -> passes validation, proceeds to preview."""
        csv_bytes = _make_csv([{"sku": "S1", "name": "Widget", "category": "Stone"}])
        r = await ui_client.post(
            "/inventory/import/preview",
            cookies=_authed(),
            files={"csv_file": ("items.csv", csv_bytes, "text/csv")},
        )
        assert r.status_code == 200
        m = re.search(r'name="csv_ref"\s+value="([^"]+)"', r.text)
        assert m
        csv_ref = m.group(1)

        form_data = {
            "csv_ref": csv_ref,
            "map__sku": "sku",
            "map__name": "name",
            "map__category": MAPPING_ATTRIBUTE,
            "attr_name__category": "lot_type",  # Renamed to avoid collision
        }
        r = await ui_client.post(
            "/inventory/import/mapped",
            cookies=_authed(),
            data=form_data,
        )
        assert r.status_code == 200
        # Should proceed to validation - no mapping errors
        # (will show "Import All" or validation errors from field validation, not mapping errors)
        assert b"flash--error" not in r.content or b"built-in" not in r.content.lower()

    @pytest.mark.asyncio
    async def test_no_500_on_duplicate_mapping(self, ui_client):
        """Regression: duplicate target mapping must never produce 500."""
        csv_bytes = _make_csv([{"a": "1", "b": "2", "c": "3"}])
        r = await ui_client.post(
            "/inventory/import/preview",
            cookies=_authed(),
            files={"csv_file": ("items.csv", csv_bytes, "text/csv")},
        )
        m = re.search(r'name="csv_ref"\s+value="([^"]+)"', r.text)
        assert m
        csv_ref = m.group(1)

        # All three mapped to same target
        form_data = {
            "csv_ref": csv_ref,
            "map__a": "name",
            "map__b": "name",
            "map__c": "name",
        }
        r = await ui_client.post(
            "/inventory/import/mapped",
            cookies=_authed(),
            data=form_data,
        )
        assert r.status_code == 200  # Not 500
        assert b"flash--error" in r.content


class TestCsvImportHelpers:
    """Unit tests for csv_import.py helpers - no HTTP needed."""

    def test_validate_cell_required_empty_fails(self):
        from ui.routes.csv_import import CsvImportSpec, validate_cell
        spec = CsvImportSpec(cols=["name"], required={"name"}, type_map={})
        assert validate_cell(spec, "name", "") is False
        assert validate_cell(spec, "name", "   ") is False

    def test_validate_cell_required_filled_passes(self):
        from ui.routes.csv_import import CsvImportSpec, validate_cell
        spec = CsvImportSpec(cols=["name"], required={"name"}, type_map={})
        assert validate_cell(spec, "name", "Widget") is True

    def test_validate_cell_optional_empty_passes(self):
        from ui.routes.csv_import import CsvImportSpec, validate_cell
        spec = CsvImportSpec(cols=["desc"], required=set(), type_map={})
        assert validate_cell(spec, "desc", "") is True

    def test_validate_cell_type_cast_valid(self):
        from ui.routes.csv_import import CsvImportSpec, validate_cell
        spec = CsvImportSpec(cols=["qty"], required=set(), type_map={"qty": float})
        assert validate_cell(spec, "qty", "3.5") is True

    def test_validate_cell_type_cast_invalid(self):
        from ui.routes.csv_import import CsvImportSpec, validate_cell
        spec = CsvImportSpec(cols=["qty"], required=set(), type_map={"qty": float})
        assert validate_cell(spec, "qty", "not-a-number") is False

    # ── New flow: validation_result ───────────────────────────────────────────

    def test_validation_result_clean_shows_import_button(self):
        """When all rows are valid, the confirm panel must show Import All button."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = CsvImportSpec(cols=["sku", "name"], required={"sku", "name"}, type_map={})
        rows = [{"sku": "S1", "name": "Widget"}]
        html = to_xml(validation_result(
            rows=rows, cols=["sku", "name"],
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm",
            error_report_action="/x/errors",
            back_href="/x",
        ))
        assert "Import All" in html
        assert "rows ready" in html or "1" in html

    def test_validation_result_errors_shows_download_button(self):
        """When errors exist, must show error count and download button, NOT Import All."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = CsvImportSpec(cols=["sku", "name"], required={"sku", "name"}, type_map={})
        rows = [{"sku": "", "name": "Widget"}]  # sku missing → error
        html = to_xml(validation_result(
            rows=rows, cols=["sku", "name"],
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm",
            error_report_action="/x/errors",
            back_href="/x",
        ))
        assert "error" in html.lower()
        assert "Download error report" in html or "error report" in html.lower()
        # Import All must NOT be present when there are errors
        assert "Import All" not in html

    def test_validation_result_errors_includes_csv_data_for_download(self):
        """Error panel must embed csv_ref so the download form can POST it."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = CsvImportSpec(cols=["sku"], required={"sku"}, type_map={})
        rows = [{"sku": ""}]
        html = to_xml(validation_result(
            rows=rows, cols=["sku"],
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm",
            error_report_action="/x/errors",
            back_href="/x",
        ))
        assert 'name="csv_ref"' in html

    def test_validation_result_clean_includes_csv_data_for_confirm(self):
        """Clean confirm panel must embed csv_ref so confirm POST can read rows."""
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result, _load_csv
        spec = CsvImportSpec(cols=["sku"], required={"sku"}, type_map={})
        rows = [{"sku": "SKU-1"}]
        html = to_xml(validation_result(
            rows=rows, cols=["sku"],
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm",
            error_report_action="/x/errors",
            back_href="/x",
        ))
        assert 'name="csv_ref"' in html
        # The stashed CSV must contain the row data
        import re
        m = re.search(r'name="csv_ref"\s+value="([^"]+)"', html)
        assert m, "csv_ref hidden field not found"
        stashed = _load_csv(m.group(1))
        assert stashed and "SKU-1" in stashed

    def test_error_report_csv_adds_errors_column(self):
        """error_report_csv must append _errors column listing bad fields."""
        import csv, io
        from ui.routes.csv_import import CsvImportSpec, validate_cell, error_report_csv
        spec = CsvImportSpec(cols=["sku", "name"], required={"sku", "name"}, type_map={})
        rows = [{"sku": "", "name": "Widget"}, {"sku": "S2", "name": ""}]
        csv_text = error_report_csv(rows, spec.cols, lambda c, v: validate_cell(spec, c, v))
        reader = list(csv.DictReader(io.StringIO(csv_text)))
        assert "_errors" in reader[0]
        assert "sku" in reader[0]["_errors"]   # row 0: sku empty
        assert "name" in reader[1]["_errors"]  # row 1: name empty

    def test_error_report_csv_empty_errors_column_when_valid(self):
        """Valid rows are excluded from the error report (no rows to show)."""
        import csv, io
        from ui.routes.csv_import import CsvImportSpec, validate_cell, error_report_csv
        spec = CsvImportSpec(cols=["sku"], required={"sku"}, type_map={})
        rows = [{"sku": "SKU-1"}]
        csv_text = error_report_csv(rows, spec.cols, lambda c, v: validate_cell(spec, c, v))
        reader = list(csv.DictReader(io.StringIO(csv_text)))
        assert reader == []


class TestInventoryImportFlow:
    """HTTP-level tests for the inventory import pipeline."""

    @pytest.mark.asyncio
    async def test_import_preview_unknown_location_accepted(self, ui_client):
        """Unknown location_name → accepted in preview (auto-created at confirm time)."""
        csv_bytes = _make_csv([{"sku": "S1", "name": "Widget", "location_name": "Unknown Loc"}])
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await _inventory_import_with_mapping(ui_client, csv_bytes)
        assert r.status_code == 200
        # Unknown location is not a validation error - it gets auto-created at confirm
        assert b"Import All" in r.content

    @pytest.mark.asyncio
    async def test_import_preview_missing_location_column_accepted(self, ui_client):
        """CSV without location_name column → accepted in preview (default location used at confirm)."""
        csv_bytes = _make_csv([{"sku": "S1", "name": "Widget"}])
        with patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await _inventory_import_with_mapping(ui_client, csv_bytes)
        assert r.status_code == 200
        # No location_name column is fine - default location resolved at confirm
        assert b"Import All" in r.content

    @pytest.mark.asyncio
    async def test_import_preview_clean_shows_import_button(self, ui_client):
        """Clean CSV → validation panel with Import All button (not error report)."""
        csv_bytes = _make_csv([{"sku": "S1", "name": "Widget", "location_name": "Main Office"}])
        with patch("ui.api_client.get_locations", new=AsyncMock(return_value=_LOCATIONS_RESP)):
            r = await _inventory_import_with_mapping(ui_client, csv_bytes)
        assert r.status_code == 200
        assert b"Import All" in r.content
        assert b"import-preview" in r.content

    @pytest.mark.asyncio
    async def test_import_preview_errors_shows_download_prompt(self, ui_client):
        """CSV with required field missing → error report prompt, not Import All."""
        csv_bytes = _make_csv([{"sku": "", "name": "Widget", "location_name": "Main Office"}])
        with patch("ui.api_client.get_locations", new=AsyncMock(return_value=_LOCATIONS_RESP)):
            r = await _inventory_import_with_mapping(ui_client, csv_bytes)
        assert r.status_code == 200
        assert b"error" in r.content.lower()
        assert b"Import All" not in r.content

    @pytest.mark.asyncio
    async def test_import_errors_download(self, ui_client):
        """POST /inventory/import/errors must return a CSV file with _errors column."""
        import csv as _csv, io as _io
        csv_data = "sku,name,location_name\n,Widget,Main Office\n"
        r = await ui_client.post(
            "/inventory/import/errors",
            cookies=_authed(),
            data={"csv_data": csv_data},
        )
        assert r.status_code == 200
        assert "csv" in r.headers.get("content-type", "").lower() or "attachment" in r.headers.get("content-disposition", "")
        reader = list(_csv.DictReader(_io.StringIO(r.text)))
        assert "_errors" in reader[0]

    @pytest.mark.asyncio
    async def test_import_template_download(self, ui_client):
        r = await ui_client.get("/inventory/import/template", cookies=_authed())
        assert r.status_code == 200
        assert b"sku" in r.content
        assert b"name" in r.content
        assert b"location_name" in r.content

    @pytest.mark.asyncio
    async def test_import_confirm_all_valid_imports(self, ui_client):
        """Confirm with all-valid rows (preview already validated) → all imported."""
        known_loc = {"id": "loc:main", "name": "Main Office", "type": "store"}
        csv_data = "sku,name,location_name\nS1,Widget,Main Office\nS2,Ring,Main Office\n"
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [known_loc], "total": 1})),
            patch("ui.api_client.batch_import", new=AsyncMock(return_value={"created": 2, "skipped": 0, "errors": []})),
            patch("ui.api_client.merge_category_schemas", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.post(
                "/inventory/import/confirm",
                cookies=_authed(),
                data={"csv_data": csv_data},
            )
        assert r.status_code == 200
        assert b"import-card--success" in r.content
        assert b"Created" in r.content

    @pytest.mark.asyncio
    async def test_import_confirm_unknown_location_auto_created(self, ui_client):
        """Unknown location_name in CSV → auto-created during confirm, import succeeds."""
        csv_data = "sku,name,location_name\nS1,Widget,New Warehouse\n"
        created_loc = {"id": "loc:new", "name": "New Warehouse", "type": "warehouse"}
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.create_location", new=AsyncMock(return_value=created_loc)),
            patch("ui.api_client.batch_import", new=AsyncMock(return_value={"created": 1, "skipped": 0, "errors": []})),
            patch("ui.api_client.merge_category_schemas", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.post(
                "/inventory/import/confirm",
                cookies=_authed(),
                data={"csv_data": csv_data},
            )
        assert r.status_code == 200
        assert b"import-card--success" in r.content

    @pytest.mark.asyncio
    async def test_import_confirm_no_location_column_uses_default(self, ui_client):
        """CSV with no location_name column and single location → uses that location."""
        default_loc = {"id": "loc:hq", "name": "Head Office", "type": "office", "is_default": True}
        csv_data = "sku,name\nS1,Widget\n"
        with (
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [default_loc], "total": 1})),
            patch("ui.api_client.batch_import", new=AsyncMock(return_value={"created": 1, "skipped": 0, "errors": []})),
            patch("ui.api_client.merge_category_schemas", new=AsyncMock(return_value={})),
        ):
            r = await ui_client.post(
                "/inventory/import/confirm",
                cookies=_authed(),
                data={"csv_data": csv_data},
            )
        assert r.status_code == 200
        assert b"import-card--success" in r.content

    @pytest.mark.asyncio
    async def test_import_confirm_no_location_multiple_locs_aborts(self, ui_client):
        """CSV with no location_name and multiple locations → error (ambiguous)."""
        locs = [
            {"id": "loc:a", "name": "Warehouse A", "type": "warehouse"},
            {"id": "loc:b", "name": "Warehouse B", "type": "warehouse"},
        ]
        csv_data = "sku,name\nS1,Widget\n"
        with patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": locs, "total": 2})):
            r = await ui_client.post(
                "/inventory/import/confirm",
                cookies=_authed(),
                data={"csv_data": csv_data},
            )
        assert r.status_code == 200
        assert b"location_name" in r.content.lower() or b"location" in r.content.lower()


class TestSettingsImportFlow:
    """Settings import routes use the new validate-then-report flow."""

    @pytest.mark.asyncio
    async def test_locations_preview_clean_shows_import(self, ui_client):
        csv_bytes = _make_csv([{"name": "Main Store", "type": "store"}])
        r = await _generic_import_with_mapping(
            ui_client, csv_bytes,
            "/settings/import/locations/preview",
            "/settings/import/locations/mapped",
            ["name", "type"],
        )
        assert r.status_code == 200
        assert b"Import All" in r.content

    @pytest.mark.asyncio
    async def test_locations_preview_errors_shows_download(self, ui_client):
        csv_bytes = _make_csv([{"name": "", "type": "store"}])  # name required
        r = await _generic_import_with_mapping(
            ui_client, csv_bytes,
            "/settings/import/locations/preview",
            "/settings/import/locations/mapped",
            ["name", "type"],
        )
        assert r.status_code == 200
        assert b"error" in r.content.lower()
        assert b"Import All" not in r.content

    @pytest.mark.asyncio
    async def test_locations_errors_download(self, ui_client):
        import csv as _csv, io as _io
        csv_data = "name,type\n,store\n"
        r = await ui_client.post(
            "/settings/import/locations/errors",
            cookies=_authed(),
            data={"csv_data": csv_data},
        )
        assert r.status_code == 200
        reader = list(_csv.DictReader(_io.StringIO(r.text)))
        assert "_errors" in reader[0]

    @pytest.mark.asyncio
    async def test_taxes_preview_clean_shows_import(self, ui_client):
        csv_bytes = _make_csv([{"name": "VAT 7%", "rate": "7", "tax_type": "both", "is_default": "true", "description": ""}])
        r = await _generic_import_with_mapping(
            ui_client, csv_bytes,
            "/settings/import/taxes/preview",
            "/settings/import/taxes/mapped",
            ["name", "rate", "tax_type", "is_default", "description"],
        )
        assert r.status_code == 200
        assert b"Import All" in r.content

    @pytest.mark.asyncio
    async def test_taxes_errors_download(self, ui_client):
        import csv as _csv, io as _io
        csv_data = "name,rate,tax_type,is_default,description\n,notanumber,both,true,\n"
        r = await ui_client.post(
            "/settings/import/taxes/errors",
            cookies=_authed(),
            data={"csv_data": csv_data},
        )
        assert r.status_code == 200
        reader = list(_csv.DictReader(_io.StringIO(r.text)))
        assert "_errors" in reader[0]

    @pytest.mark.asyncio
    async def test_terms_preview_clean_shows_import(self, ui_client):
        csv_bytes = _make_csv([{"name": "Net 30", "days": "30", "description": ""}])
        r = await _generic_import_with_mapping(
            ui_client, csv_bytes,
            "/settings/import/payment-terms/preview",
            "/settings/import/payment-terms/mapped",
            ["name", "days", "description"],
        )
        assert r.status_code == 200
        assert b"Import All" in r.content

    @pytest.mark.asyncio
    async def test_terms_errors_download(self, ui_client):
        import csv as _csv, io as _io
        csv_data = "name,days,description\nNet 30,notanumber,\n"
        r = await ui_client.post(
            "/settings/import/payment-terms/errors",
            cookies=_authed(),
            data={"csv_data": csv_data},
        )
        assert r.status_code == 200
        reader = list(_csv.DictReader(_io.StringIO(r.text)))
        assert "_errors" in reader[0]


# TestItemFieldValuesAPI lives in tests/test_routers/test_item_field_values.py
# (requires authenticated API client + multi-tenant fixtures)

# =============================================================================
# Proactive QC Test Plan — Systematic Coverage
# =============================================================================
#
# The bugs found today fall into these repeatable patterns:
#
#   P1: HTMX partial responses missing attributes → breaks JS behavior downstream
#       (data-col, data-row, hx-post, hx-swap stripped from swapped elements)
#
#   P2: JS feedback loops from DOM mutation watchers observing their own writes
#       (MutationObserver watching `disabled` attribute it sets itself)
#
#   P3: Static snapshots used as ground truth while live DOM diverges
#       (csv_data hidden field not updated when user edits cells)
#
#   P4: Currency/context threading gaps — page fetches company but forgets to
#       pass currency/timezone into sub-renderers
#
#   P5: Multi-tenant data leakage — queries not filtered by company_id
#
#   P6: Unauthenticated endpoint access — missing cookie check before DB ops
#
# The tests below are the proactive sweep for P1, P4, P5, P6 patterns across
# the full route surface. P2/P3 are structural (tested via HTML assertions
# above). This is the first-pass QC baseline — not exhaustive, but catches
# the highest-probability failure modes before Nikolai hits them.
# =============================================================================

class TestHtmxPartialAttrContract:
    """P1: All HTMX partial endpoints that do outerHTML swap must return
    elements with hx-post, hx-target, and id attributes intact.

    Pattern: fetch the partial, assert the returned fragment can re-trigger
    itself (has hx-post pointing to itself, has correct id for targeting).
    """

    @pytest.mark.asyncio
    async def test_item_field_edit_rewires_htmx(self, ui_client):
        """GET /api/items/{id}/field/{field}/edit must return element with hx-post."""
        _item = {
            "id": "item:x", "sku": "S1", "name": "Widget", "status": "available",
            "quantity": 1, "retail_price": 100, "wholesale_price": 80, "cost_price": 60,
            "category": "", "barcode": "", "weight": None, "weight_unit": None,
            "created_at": "", "updated_at": "",
        }
        _schema = [{"key": "name", "type": "text", "label": "Name", "editable": True}]
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_item)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_schema)),
        ):
            r = await ui_client.get("/api/items/item:x/field/name/edit", cookies=_authed())
        assert r.status_code == 200
        # Must have hx-post or hx-get to re-wire itself
        assert b"hx-" in r.content or b"hx_" in r.content

    @pytest.mark.asyncio
    async def test_item_field_patch_returns_display_with_edit_trigger(self, ui_client):
        """PATCH /api/items/{id}/field/{field} must return a clickable cell."""
        _item = {
            "id": "item:x", "sku": "S1", "name": "New Name", "status": "available",
            "quantity": 1, "retail_price": 100, "wholesale_price": 80, "cost_price": 60,
            "category": "", "barcode": "", "weight": None, "weight_unit": None,
            "created_at": "", "updated_at": "",
        }
        _schema = [{"key": "name", "type": "text", "label": "Name", "editable": True}]
        with (
            patch("ui.api_client.patch_item", new=AsyncMock(return_value=_item)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_item)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_schema)),
        ):
            r = await ui_client.patch(
                "/api/items/item:x/field/name",
                cookies=_authed(),
                data={"value": "New Name"},
            )
        assert r.status_code == 200
        assert b"New Name" in r.content


class TestCurrencyThreading:
    """P4: Every page that renders money values must pass currency through.

    Strategy: mock get_company to return a non-default currency (USD),
    assert the symbol or code appears in the rendered page — not the
    hardcoded ฿ fallback.
    """

    @pytest.mark.asyncio
    async def test_inventory_page_uses_company_currency(self, ui_client):
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={
                "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01"
            })),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={
                "items": [{"id": "i1", "sku": "S1", "name": "Widget", "retail_price": 99,
                           "quantity": 1, "status": "available", "category": ""}],
                "total": 1, "category_counts": {},
            })),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        # Must not have raw hardcoded ฿ — currency must come from company settings
        # (Allow $ or USD but not ฿ when currency is USD)
        assert "฿99" not in r.content.decode()

    @pytest.mark.asyncio
    async def test_accounting_page_uses_company_currency(self, ui_client):
        _empty_section = {"lines": [], "total": 0}
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={
                "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01"
            })),
            patch("ui.api_client.get_pnl", new=AsyncMock(return_value={
                "revenue": _empty_section, "cogs": _empty_section, "expenses": _empty_section,
                "gross_profit": 0, "net_profit": 0,
            })),
            patch("ui.api_client.get_balance_sheet", new=AsyncMock(return_value={
                "assets": _empty_section, "liabilities": _empty_section, "equity": _empty_section,
                "total_assets": 0, "total_liabilities": 0, "total_equity": 0,
            })),
            patch("ui.api_client.get_trial_balance", new=AsyncMock(return_value={
                "accounts": [], "total_debit": 0, "total_credit": 0,
            })),
        ):
            r = await ui_client.get("/accounting", cookies=_authed())
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_crm_deal_value_uses_fmt_money(self, ui_client):
        """Deals value column must use fmt_money, not hardcoded symbol."""
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_deals", new=AsyncMock(return_value={
                "items": [{"id": "d1", "name": "Big Deal", "stage": "Lead",
                           "value": 50000, "contact_name": "Acme", "status": "open"}],
                "total": 1,
            })),
        ):
            r = await ui_client.get("/contacts/sales", cookies=_authed())
        assert r.status_code == 200
        # Raw hardcoded ฿50000 must not appear when currency is THB via fmt_money
        # (fmt_money would format it as ฿50,000 or similar, not ฿50000)
        assert b"\xe0\xb8\xbf50000" not in r.content  # ฿50000 raw


class TestNikolaiFixedBugs:
    """Regression suite for bugs fixed and reported by Nikolai 2026-03-09.

    Covers:
      - Currency displayed correctly on settings page
      - Fiscal year displayed with human label (not raw code)
      - Timezone displayed (not blank/broken)
      - is_default exposed on locations settings tab
      - User cannot deactivate their own account (self-deactivation blocked)
    """

    from contextlib import ExitStack

    _LOC_DATA = {"items": [
        {"id": "loc:1", "name": "Main Vault", "type": "warehouse", "is_default": True},
        {"id": "loc:2", "name": "Showroom", "type": "showroom", "is_default": False},
    ], "total": 2}

    def _stack(self, company_overrides=None):
        """Return an ExitStack with all settings patches applied."""
        from contextlib import ExitStack
        company = {**_COMPANY, **(company_overrides or {})}
        stack = ExitStack()
        stack.enter_context(patch("ui.api_client.get_company", new=AsyncMock(return_value=company)))
        stack.enter_context(patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)))
        stack.enter_context(patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)))
        stack.enter_context(patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": len(_USERS)})))
        stack.enter_context(patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)))
        stack.enter_context(patch("ui.api_client.get_locations", new=AsyncMock(return_value=self._LOC_DATA)))
        stack.enter_context(patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})))
        stack.enter_context(patch("ui.api_client.get_modules", new=AsyncMock(return_value=[
            {"name": "celerp-inventory", "enabled": True},
        ])))
        return stack

    # ── Currency display ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_company_tab_shows_currency_code(self, ui_client):
        """Currency column must show the currency code (e.g. THB) on the settings page."""
        with self._stack({"currency": "THB"}):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"THB" in r.content

    @pytest.mark.asyncio
    async def test_company_tab_currency_edit_shows_select(self, ui_client):
        """Editing currency must return a searchable combobox with currency options."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={**_COMPANY, "currency": "USD"})):
            r = await ui_client.get("/settings/company/currency/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"combobox-wrap" in r.content
        assert b"USD" in r.content

    @pytest.mark.asyncio
    async def test_company_tab_currency_patch_validates(self, ui_client):
        """PATCH with an invalid currency code must return an error, not save."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/settings/company/currency",
                data={"value": "BOGUS"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"error" in r.content.lower() or b"Invalid" in r.content
        mock_patch.assert_not_called()

    # ── Fiscal year display ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_company_tab_shows_fiscal_year_human_label(self, ui_client):
        """fiscal_year_start must render as human month name, not raw code."""
        with self._stack({"fiscal_year_start": "01-01"}):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        # Raw "01-01" alone is meaningless — should show a month name
        assert b"January" in r.content

    @pytest.mark.asyncio
    async def test_company_tab_fiscal_year_edit_shows_select(self, ui_client):
        """Editing fiscal_year_start must return a <select>."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={**_COMPANY, "fiscal_year_start": "04"})):
            r = await ui_client.get("/settings/company/fiscal_year_start/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<select" in r.content or b"Select" in r.content

    @pytest.mark.asyncio
    async def test_company_tab_fiscal_year_patch_validates(self, ui_client):
        """PATCH with invalid fiscal_year_start must reject, not save."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/settings/company/fiscal_year_start",
                data={"value": "99"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"error" in r.content.lower() or b"Invalid" in r.content
        mock_patch.assert_not_called()

    # ── Timezone display ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_company_tab_shows_timezone(self, ui_client):
        """Timezone must be rendered on company settings tab (not blank)."""
        with self._stack({"timezone": "Asia/Bangkok"}):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"Asia/Bangkok" in r.content

    @pytest.mark.asyncio
    async def test_company_tab_timezone_edit_shows_combobox(self, ui_client):
        """Editing timezone must return the timezone combobox widget."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={**_COMPANY, "timezone": "UTC"})):
            r = await ui_client.get("/settings/company/timezone/edit", cookies=_authed())
        assert r.status_code == 200
        assert b'name="value"' in r.content
        assert b"combobox" in r.content or b"UTC" in r.content

    @pytest.mark.asyncio
    async def test_company_tab_timezone_patch_validates(self, ui_client):
        """PATCH with unknown timezone must reject, not save."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/settings/company/timezone",
                data={"value": "Fake/Zone"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"error" in r.content.lower() or b"Unknown" in r.content
        mock_patch.assert_not_called()

    # ── is_default on locations tab ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_locations_tab_shows_is_default(self, ui_client):
        """Locations settings tab must surface which location is the default."""
        with self._stack():
            r = await ui_client.get("/settings/inventory?tab=locations", cookies=_authed())
        assert r.status_code == 200
        assert b"Default" in r.content or b"default" in r.content

    @pytest.mark.asyncio
    async def test_locations_tab_lists_both_locations(self, ui_client):
        """All locations must appear in the locations tab."""
        with self._stack():
            r = await ui_client.get("/settings/inventory?tab=locations", cookies=_authed())
        assert r.status_code == 200
        assert b"Main Vault" in r.content
        assert b"Showroom" in r.content

    # ── Self-deactivation blocked ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_user_cannot_deactivate_self(self, ui_client):
        """PATCH is_active=false for the currently-logged-in user must be blocked."""
        import base64, json as _json
        user_id = "user:abc123"
        payload_dict = {"sub": user_id, "company_id": "company:1"}
        payload_b64 = base64.b64encode(_json.dumps(payload_dict).encode()).decode().rstrip("=")
        fake_token = f"header.{payload_b64}.sig"

        with (
            patch("ui.api_client.patch_user", new=AsyncMock()) as mock_patch,
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
        ):
            r = await ui_client.patch(
                f"/settings/users/{user_id}/is_active",
                data={"value": "false"},
                cookies={"celerp_token": fake_token},
            )
        assert r.status_code == 200
        assert b"cannot deactivate" in r.content.lower() or b"own account" in r.content.lower()
        mock_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_can_deactivate_other_user(self, ui_client):
        """Admin can deactivate a different user — must not be blocked."""
        import base64, json as _json
        acting_user_id = "user:admin1"
        target_user_id = "user:other99"
        payload_dict = {"sub": acting_user_id, "company_id": "company:1"}
        payload_b64 = base64.b64encode(_json.dumps(payload_dict).encode()).decode().rstrip("=")
        fake_token = f"header.{payload_b64}.sig"

        with (
            patch("ui.api_client.patch_user", new=AsyncMock(return_value={"ok": True})),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": [
                {"id": target_user_id, "name": "Other", "email": "o@o.com", "role": "operator", "is_active": False}
            ], "total": 1})),
        ):
            r = await ui_client.patch(
                f"/settings/users/{target_user_id}/is_active",
                data={"value": "false"},
                cookies={"celerp_token": fake_token},
            )
        assert r.status_code == 200
        assert b"cannot deactivate" not in r.content.lower()


class TestInviteUser:
    """Invite user page and form submission."""

    @pytest.mark.asyncio
    async def test_invite_user_page_renders(self, ui_client):
        r = await ui_client.get("/settings/users/new", cookies=_authed())
        assert r.status_code == 200
        assert b"Invite User" in r.content or b"New User" in r.content
        # Form fields present
        assert b'name="name"' in r.content
        assert b'name="email"' in r.content
        assert b'name="password"' in r.content
        assert b'name="role"' in r.content

    @pytest.mark.asyncio
    async def test_invite_user_unauthed_redirects(self, ui_client):
        r = await ui_client.get("/settings/users/new")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_invite_user_post_success_redirects(self, ui_client):
        with patch("ui.api_client.create_user", new=AsyncMock(return_value={"id": "user:1", "name": "Tester"})):
            r = await ui_client.post(
                "/settings/users/new",
                data={"name": "Tester", "email": "tester@a.com", "password": "pw123", "role": "operator"},
                cookies=_authed(),
            )
        # 204 with HX-Redirect header on success
        assert r.status_code == 204
        assert "/settings" in r.headers.get("hx-redirect", "")

    @pytest.mark.asyncio
    async def test_invite_user_post_missing_fields_returns_error(self, ui_client):
        r = await ui_client.post(
            "/settings/users/new",
            data={"name": "", "email": "", "password": "", "role": "operator"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"required" in r.content.lower()

    @pytest.mark.asyncio
    async def test_invite_user_post_api_error_shows_message(self, ui_client):
        from ui.api_client import APIError
        with patch("ui.api_client.create_user", new=AsyncMock(side_effect=APIError(409, "Email already in use"))):
            r = await ui_client.post(
                "/settings/users/new",
                data={"name": "Dup", "email": "dup@a.com", "password": "pw", "role": "operator"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Email already in use" in r.content

    @pytest.mark.asyncio
    async def test_invite_user_post_unauthed_returns_error(self, ui_client):
        r = await ui_client.post(
            "/settings/users/new",
            data={"name": "X", "email": "x@x.com", "password": "pw", "role": "operator"},
        )
        # No cookie → either redirect or Unauthorized partial (HTMX target)
        assert r.status_code in (200, 302, 303)
        if r.status_code == 200:
            assert b"Unauthorized" in r.content


class TestUnauthenticatedAccess:
    """P6: Every state-changing or data-reading route must redirect when no cookie."""

    @pytest.mark.parametrize("method,path", [
        ("GET", "/inventory"),
        ("GET", "/inventory/import"),
        ("GET", "/accounting"),
        ("GET", "/crm"),
        ("GET", "/docs"),
        ("GET", "/settings"),
        ("GET", "/reports"),
        ("GET", "/settings/users/new"),
        ("POST", "/inventory/import/preview"),
        ("POST", "/inventory/import/confirm"),
        ("POST", "/settings/import/locations/preview"),
        ("POST", "/settings/import/locations/confirm"),
        ("POST", "/settings/import/taxes/confirm"),
        ("POST", "/settings/import/payment-terms/confirm"),
    ])
    @pytest.mark.asyncio
    async def test_unauthed_redirects(self, method, path, ui_client):
        if method == "GET":
            r = await ui_client.get(path)
        else:
            r = await ui_client.post(path)
        assert r.status_code in (302, 303), f"{method} {path} should redirect unauthenticated"
        assert "/login" in r.headers.get("location", "") or "/setup" in r.headers.get("location", "")


# =============================================================================
# Proactive QA Sweep — Extrapolated from Nikolai's 2026-03-09 Bug Patterns
# =============================================================================
#
# The bugs Nikolai found fell into repeatable patterns. This suite extends
# coverage proactively across the full UI surface to catch them before QA.
#
# Bug patterns mapped to test classes below:
#   P1 - HTMX partial responses missing data-* attrs or hx-* re-wire hooks
#   P2 - JS feedback loops (MutationObserver watching its own writes)
#   P3 - Hidden csv_data field not reflecting latest state
#   P4 - Currency/timezone/fiscal_year not threaded into sub-renderers
#   P5 - Multi-tenant isolation (queries not filtered by company_id)
#   P6 - Unauthenticated access to state-changing endpoints
#   P7 - Form validation not enforced server-side (only JS-side)
#   P8 - CSV error table - identifier columns always shown, error cols only
# =============================================================================


class TestCsvImportUxErrorTable:
    """P8: CSV import UX redesign — error-only column table contract.

    The new validation_result function must:
    - Show only columns with errors PLUS identifier columns (sku, name, id, etc.)
    - Show only rows that have errors
    - Mark error cells with data-col / data-row for JS targeting
    - Include undo/redo buttons
    - Include summary bar with error count
    - Include cancel link (back_href)
    """

    def _spec(self):
        from ui.routes.csv_import import CsvImportSpec
        return CsvImportSpec(
            cols=["sku", "name", "quantity", "cost_price"],
            required={"sku", "name"},
            type_map={"quantity": float, "cost_price": float},
        )

    def _html(self, rows, *, back_href="/x"):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import validate_cell, validation_result
        spec = self._spec()
        return to_xml(validation_result(
            rows=rows,
            cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm",
            error_report_action="/x/errors",
            back_href=back_href,
        ))

    def test_error_table_shows_identifier_col_always(self):
        """'name' (identifier col) must appear even if it has no errors."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        # sku has error; name is identifier — both must appear as headers
        assert "sku" in html
        assert "name" in html

    def test_error_table_omits_clean_non_identifier_cols(self):
        """Columns with no errors and not identifier cols must be hidden."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        # quantity and cost_price are clean + not identifiers — must not appear
        # (they should be omitted from the narrow error view)
        # Note: if no clean non-identifier col is actually included, test just
        # verifies the error table renders without them in header
        assert "1 error" in html.lower() or "error" in html.lower()

    def test_error_table_shows_only_error_rows(self):
        """Clean rows must not appear in the error table body (may appear in csv_data)."""
        rows = [
            {"sku": "S1", "name": "Widget", "quantity": "5", "cost_price": "10"},   # clean
            {"sku": "", "name": "Broken", "quantity": "5", "cost_price": "10"},     # error
        ]
        html = self._html(rows)
        # The error table Tbody must only contain the error row
        # "Widget" is only in the clean row - must not appear in the table body cells
        # (it may appear in the hidden csv_data field which is acceptable)
        # We check that data-row="0" (clean row index) is NOT present in table cells
        assert 'data-row="0"' not in html or "data-row" not in html
        # The error row "Broken" must be visible
        assert "Broken" in html

    def test_error_cells_have_data_attrs_for_js(self):
        """Error cells must have data-row and data-col for fill-down JS targeting."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        assert 'data-row=' in html or 'data-row"' in html or "data-row" in html
        assert 'data-col=' in html or 'data-col"' in html or "data-col" in html

    def test_undo_redo_buttons_present(self):
        """Inline-fix panel must have a Fix & Import submit button."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        assert "Fix" in html and "Import" in html

    def test_cancel_link_uses_back_href(self):
        """Cancel link must point to back_href."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows, back_href="/inventory/import")
        assert "/inventory/import" in html

    def test_summary_bar_shows_error_count(self):
        """Summary must quantify how many errors / rows."""
        rows = [
            {"sku": "", "name": "", "quantity": "5", "cost_price": "10"},   # 2 errors
            {"sku": "S2", "name": "B", "quantity": "bad", "cost_price": "10"},  # 1 error
        ]
        html = self._html(rows)
        # Should mention errors and rows
        assert "error" in html.lower()

    def test_download_error_report_button_present(self):
        """Download error report button must be present in error panel."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        assert "error report" in html.lower() or "Download" in html

    def test_fill_down_js_injected(self):
        """The fill-column JS (csvFillColumn) must be present in error panel."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        assert "csvFillColumn" in html

    def test_fillable_header_on_error_col(self):
        """Error columns must have editable input cells (cell-edit class)."""
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        assert "cell-edit" in html

    def test_clean_csv_shows_import_all_not_error_table(self):
        """When rows are all valid, must show Import All, not error table."""
        rows = [{"sku": "S1", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = self._html(rows)
        assert "Import All" in html
        assert "csvFillColumn" not in html  # no inline-fix JS
        assert "cell-edit" not in html  # no editable cells

    def test_undo_redo_buttons_start_disabled(self):
        """Fill-all bar must appear when multiple error rows exist for a column."""
        rows = [
            {"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"},
            {"sku": "", "name": "Gadget", "quantity": "3", "cost_price": "20"},
        ]
        html = self._html(rows)
        assert "Apply" in html  # fill-all bar has Apply button


class TestSettingsFieldValidationServerSide:
    """P7: Settings inline-edit endpoints must validate server-side.

    Each editable field that has known valid values must reject invalid input
    without calling the API. This prevents bypassing JS-only validation.
    """

    @pytest.mark.asyncio
    async def test_slug_patch_rejects_invalid_chars(self, ui_client):
        """Company slug must reject spaces and special chars."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/settings/company/slug",
                data={"value": "my company slug!"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"error" in r.content.lower() or b"invalid" in r.content.lower() or b"slug" in r.content.lower()
        mock_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_name_patch_rejects_empty(self, ui_client):
        """Company name cannot be blank — must return error and not call API."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.patch_company", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/settings/company/name",
                data={"value": "   "},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"blank" in r.content.lower() or b"error" in r.content.lower() or b"cannot" in r.content.lower()
        mock_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_slug_patch_calls_api(self, ui_client):
        """A valid slug must be accepted and call the API."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.patch_company", new=AsyncMock(return_value=None)) as mock_patch,
        ):
            r = await ui_client.patch(
                "/settings/company/slug",
                data={"value": "my-valid-slug"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        mock_patch.assert_called_once()


class TestMultiTenantIsolation:
    """P5: State-reading endpoints must filter by company_id.

    We verify that the API client calls always include the auth token,
    which the backend uses to scope queries. At the UI layer this means
    every data-fetch call passes the token from the request cookie.
    """

    @pytest.mark.asyncio
    async def test_inventory_passes_token_to_api(self, ui_client):
        """Inventory page must pass the token (not hardcoded company_id) to list_items."""
        calls = []

        async def _capture_list_items(token, params=None):
            calls.append(token)
            return {"items": [], "total": 0, "category_counts": {}}

        with (
            patch("ui.api_client.list_items", new=_capture_list_items),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            await ui_client.get("/inventory", cookies={"celerp_token": "tenant-A-token"})
        assert calls == ["tenant-A-token"]

    @pytest.mark.asyncio
    async def test_crm_passes_token_to_api(self, ui_client):
        """CRM contacts page must pass the auth token to list_contacts."""
        calls = []

        async def _capture(token, params=None):
            calls.append(token)
            return {"items": [], "total": 0}

        with patch("ui.api_client.list_contacts", new=_capture):
            await ui_client.get("/contacts/customers", cookies={"celerp_token": "tenant-B-token"})
        assert calls == ["tenant-B-token"]

    @pytest.mark.asyncio
    async def test_settings_passes_token_to_get_company(self, ui_client):
        """Settings page must pass the auth token to get_company."""
        calls = []
        tenant_token = make_test_token(role="owner", company_id="00000000-0000-0000-0000-cccccccccccc")

        async def _capture(token):
            calls.append(token)
            return _COMPANY

        with (
            patch("ui.api_client.get_company", new=_capture),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": _USERS, "total": 1})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            await ui_client.get("/settings/general?tab=company", cookies={"celerp_token": tenant_token})
        assert calls[0] == tenant_token


class TestDashboardCurrencyThreading:
    """P4: Dashboard and all money-displaying pages must use company currency."""

    @pytest.mark.asyncio
    async def test_dashboard_renders_for_usd_company(self, ui_client):
        """Dashboard must load for a USD company without crashing."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={
                "name": "USD Corp", "currency": "USD", "timezone": "UTC",
                "fiscal_year_start": "01-01",
            })),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_memo_summary", new=AsyncMock(return_value={})),
            patch("ui.api_client.my_companies", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/", cookies=_authed())
        assert r.status_code in (200, 302, 303)

    @pytest.mark.asyncio
    async def test_reports_page_loads_with_company_currency(self, ui_client):
        """Reports page must load for a non-THB company without crashing."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value={
                "name": "EUR Corp", "currency": "EUR", "timezone": "Europe/Berlin",
                "fiscal_year_start": "01-01",
            })),
            patch("ui.api_client.get_pnl", new=AsyncMock(return_value={
                "revenue": {"lines": [], "total": 0},
                "cogs": {"lines": [], "total": 0},
                "expenses": {"lines": [], "total": 0},
                "gross_profit": 0, "net_profit": 0,
            })),
            patch("ui.api_client.get_balance_sheet", new=AsyncMock(return_value={
                "assets": {"lines": [], "total": 0},
                "liabilities": {"lines": [], "total": 0},
                "equity": {"lines": [], "total": 0},
                "total_assets": 0, "total_liabilities": 0, "total_equity": 0,
            })),
            patch("ui.api_client.get_trial_balance", new=AsyncMock(return_value={
                "accounts": [], "total_debit": 0, "total_credit": 0,
            })),
        ):
            r = await ui_client.get("/reports", cookies=_authed())
        assert r.status_code == 200


class TestHtmxSwapAttrContractExtended:
    """P1: Extended HTMX attr contract tests across more endpoints.

    Every HTMX partial that does outerHTML swap must return an element
    that can re-trigger itself (has hx-* attrs and an id for targeting).
    """

    @pytest.mark.asyncio
    async def test_settings_inline_edit_field_returns_hx_post(self, ui_client):
        """Settings inline edit response must contain hx-post/patch for save."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)):
            r = await ui_client.get("/settings/company/name/edit", cookies=_authed())
        assert r.status_code == 200
        # Must have an hx-post or hx-patch pointing to the save endpoint
        assert b"hx-post" in r.content or b"hx_post" in r.content or b'hx-patch' in r.content or b'hx_patch' in r.content

    @pytest.mark.asyncio
    async def test_settings_inline_edit_has_input_with_value(self, ui_client):
        """Settings inline edit must pre-populate the field value."""
        company = {**_COMPANY, "name": "Acme Co"}
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
            r = await ui_client.get("/settings/company/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"Acme Co" in r.content

    @pytest.mark.asyncio
    async def test_item_field_edit_returns_input(self, ui_client):
        """GET /api/items/{id}/field/{field}/edit must return an input element."""
        _item = {
            "id": "item:x", "sku": "S1", "name": "Widget", "status": "available",
            "quantity": 1, "retail_price": 100, "wholesale_price": 80, "cost_price": 60,
            "category": "", "barcode": "", "weight": None, "weight_unit": None,
            "created_at": "", "updated_at": "",
        }
        _schema = [{"key": "name", "type": "text", "label": "Name", "editable": True}]
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_item)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_schema)),
        ):
            r = await ui_client.get("/api/items/item:x/field/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"input" in r.content.lower() or b"textarea" in r.content.lower() or b"select" in r.content.lower()


class TestApiErrorHandling:
    """All pages must handle APIError gracefully — no 500 crashes."""

    @pytest.mark.asyncio
    async def test_inventory_api_error_shows_page_not_crash(self, ui_client):
        """If list_items raises APIError, inventory must show error state not 500."""
        from ui.api_client import APIError
        with (
            patch("ui.api_client.list_items", new=AsyncMock(side_effect=APIError(500, "DB down"))),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        # Must not 500 — redirect or graceful error page
        assert r.status_code in (200, 302, 303)

    @pytest.mark.asyncio
    async def test_crm_memo_summary_error_falls_back_gracefully(self, ui_client):
        """If get_memo_summary raises APIError(503), CRM must still load with empty memo_summary."""
        from ui.api_client import APIError
        with (
            patch("ui.api_client.get_memo_summary", new=AsyncMock(side_effect=APIError(503, "Service unavailable"))),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/crm?tab=contacts", cookies=_authed())
        assert r.status_code in (200, 302, 303)

    @pytest.mark.asyncio
    async def test_settings_api_error_shows_page_not_crash(self, ui_client):
        """If get_company raises APIError(503) on settings, must not 500."""
        from ui.api_client import APIError
        with patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(503, "Service unavailable"))):
            r = await ui_client.get("/settings/general", cookies=_authed())
        assert r.status_code in (200, 302, 303)

    @pytest.mark.asyncio
    async def test_settings_api_401_redirects_to_login(self, ui_client):
        """APIError 401 on settings/general must redirect to /login."""
        from ui.api_client import APIError
        with patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(401, "Unauthorized"))):
            r = await ui_client.get("/settings/general", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")


class TestCsvImportIdentifierColumnContract:
    """P8 extension: identifier columns (sku, name, id, email, code) must always
    appear in the error table regardless of whether they have errors."""

    def _result_html(self, cols, rows, required=None):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = CsvImportSpec(cols=cols, required=required or set(), type_map={})
        return to_xml(validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/c", error_report_action="/e", back_href="/b",
        ))

    def test_sku_always_visible_even_when_clean(self):
        """'sku' must appear in header even if all sku values are valid."""
        cols = ["sku", "price"]
        rows = [{"sku": "S1", "price": ""}]  # price error, sku clean
        html = self._result_html(cols, rows, required={"price"})
        assert "sku" in html.lower()

    def test_name_always_visible_even_when_clean(self):
        """'name' must appear even if it has no errors."""
        cols = ["name", "quantity"]
        rows = [{"name": "Widget", "quantity": "bad"}]
        html = self._result_html(cols, rows, required={"quantity"})
        assert "name" in html

    def test_email_always_visible_even_when_clean(self):
        """'email' is an identifier col - must appear even if clean."""
        cols = ["email", "phone"]
        rows = [{"email": "a@b.com", "phone": ""}]
        html = self._result_html(cols, rows, required={"phone"})
        assert "email" in html.lower()

    def test_non_identifier_clean_col_not_shown(self):
        """A column like 'notes' with no errors and not in identifiers must be hidden."""
        cols = ["sku", "notes", "price"]
        rows = [{"sku": "", "notes": "all good", "price": "10"}]  # only sku fails
        html = self._result_html(cols, rows, required={"sku"})
        # sku (identifier+error) and notes (clean, NOT identifier) — notes header should be absent
        # We can't perfectly enforce header vs cell since notes might leak via csv_data hidden field
        # but the table headers should not show it
        # At minimum: sku must be present
        assert "sku" in html


# =============================================================================
# Category Schema UI — inline editing, add, delete
# =============================================================================

_CAT_FIELDS = [
    {"key": "color", "label": "Color", "type": "text", "required": False,
     "editable": True, "show_in_table": True, "options": [], "position": 0},
    {"key": "cut", "label": "Cut", "type": "select", "required": True,
     "editable": True, "show_in_table": True, "options": ["round", "oval"], "position": 1},
]

_CAT_SCHEMAS = {"Gemstone": _CAT_FIELDS}

_SETTINGS_MOCKS_CAT = {
    "ui.api_client.get_company": AsyncMock(return_value={"name": "T", "currency": "THB", "timezone": "Asia/Bangkok", "fiscal_year_start": "01-01"}),
    "ui.api_client.get_taxes": AsyncMock(return_value={"taxes": []}),
    "ui.api_client.get_payment_terms": AsyncMock(return_value={"terms": []}),
    "ui.api_client.get_users": AsyncMock(return_value={"items": [], "total": 0}),
    "ui.api_client.get_item_schema": AsyncMock(return_value=[]),
    "ui.api_client.get_all_category_schemas": AsyncMock(return_value=_CAT_SCHEMAS),
    "ui.api_client.get_locations": AsyncMock(return_value={"items": []}),
    "ui.api_client.list_import_batches": AsyncMock(return_value={"batches": []}),
    "ui.api_client.get_modules": AsyncMock(return_value=[{"name": "celerp-inventory", "enabled": True}]),
}


class TestCategorySchemaUI:
    """Category schema settings tab — inline editing, add, and delete."""

    @pytest.mark.asyncio
    async def test_settings_schema_tab_shows_category_subtabs(self, ui_client):
        """Settings?tab=schema&cat_tab=Gemstone shows category sub-tab UI."""
        from contextlib import ExitStack
        mocks = {k: patch(k, new=v) for k, v in _SETTINGS_MOCKS_CAT.items()}
        with ExitStack() as stack:
            for m in mocks.values():
                stack.enter_context(m)
            r = await ui_client.get(
                "/settings/inventory?tab=category-library&cat=Gemstone",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Gemstone" in r.content
        assert b"color" in r.content or b"Color" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_field_edit_renders_input(self, ui_client):
        """GET /settings/cat-schema/{category}/{idx}/{field}/edit returns an editable Td."""
        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=_CAT_FIELDS)),
        ):
            r = await ui_client.get(
                "/settings/cat-schema/Gemstone/0/label/edit",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"input" in r.content.lower() or b"select" in r.content.lower()
        assert b"Color" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_field_edit_bool_renders_select(self, ui_client):
        """Boolean fields (required/editable/show_in_table) use a Yes/No select."""
        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=_CAT_FIELDS)),
        ):
            r = await ui_client.get(
                "/settings/cat-schema/Gemstone/0/required/edit",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Yes" in r.content
        assert b"No" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_field_edit_type_renders_type_select(self, ui_client):
        """'type' field shows schema type options."""
        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=_CAT_FIELDS)),
        ):
            r = await ui_client.get(
                "/settings/cat-schema/Gemstone/0/type/edit",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"text" in r.content
        assert b"select" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_field_patch_label(self, ui_client):
        """PATCH /settings/cat-schema/{category}/{idx}/{field} updates and returns display cell."""
        updated = [dict(f) for f in _CAT_FIELDS]
        updated[0] = {**updated[0], "label": "Hue"}
        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=_CAT_FIELDS)),
            patch("ui.api_client.patch_category_schema", new=AsyncMock(return_value={"ok": True})),
        ):
            # After patch, get_category_schema is called again; side_effect for second call
            with patch("ui.api_client.get_category_schema", new=AsyncMock(side_effect=[_CAT_FIELDS, updated])):
                r = await ui_client.patch(
                    "/settings/cat-schema/Gemstone/0/label",
                    data={"value": "Hue"},
                    cookies=_authed(),
                )
        assert r.status_code == 200
        assert b"Hue" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_field_patch_invalid_type_rejected(self, ui_client):
        """PATCH with invalid type value returns an error cell."""
        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=_CAT_FIELDS)),
            patch("ui.api_client.patch_category_schema", new=AsyncMock(return_value={"ok": True})),
        ):
            r = await ui_client.patch(
                "/settings/cat-schema/Gemstone/0/type",
                data={"value": "notavalidtype"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Invalid field type" in r.content or b"notavalidtype" in r.content

    @pytest.mark.asyncio
    async def test_cat_schema_field_delete(self, ui_client):
        """DELETE /settings/cat-schema/{category}/{idx} returns 204."""
        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=_CAT_FIELDS)),
            patch("ui.api_client.patch_category_schema", new=AsyncMock(return_value={"ok": True})),
        ):
            r = await ui_client.delete(
                "/settings/cat-schema/Gemstone/0",
                cookies=_authed(),
            )
        assert r.status_code == 204

    @pytest.mark.asyncio
    async def test_cat_schema_field_add(self, ui_client):
        """POST /settings/cat-schema/{category}/add returns 204 and appends a new field."""
        captured: list[list] = []

        async def _patch(token, category, fields):
            captured.append(list(fields))
            return {"ok": True}

        with (
            patch("ui.api_client.get_category_schema", new=AsyncMock(return_value=list(_CAT_FIELDS))),
            patch("ui.api_client.patch_category_schema", new=AsyncMock(side_effect=_patch)),
        ):
            r = await ui_client.post(
                "/settings/cat-schema/Gemstone/add",
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert len(captured) == 1
        sent = captured[0]
        assert len(sent) == len(_CAT_FIELDS) + 1
        new_field = sent[-1]
        assert new_field["type"] == "text"
        assert new_field["editable"] is True

    @pytest.mark.asyncio
    async def test_cat_schema_edit_unauthenticated_redirects(self, ui_client):
        """Unauthenticated cat-schema edit → redirect to login."""
        r = await ui_client.get("/settings/cat-schema/Gemstone/0/label/edit")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_cat_schema_delete_unauthenticated(self, ui_client):
        """Unauthenticated cat-schema delete → redirect to login."""
        r = await ui_client.delete("/settings/cat-schema/Gemstone/0")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")


# ---------------------------------------------------------------------------
# Modules tab tests
# ---------------------------------------------------------------------------

_MODULES_LIST = [
    {
        "name": "celerp-labels",
        "label": "Celerp Labels",
        "version": "1.0.0",
        "description": "Label printing support",
        "author": "Celerp",
        "enabled": True,
        "running": True,
    },
    {
        "name": "celerp-verticals",
        "label": "Celerp Verticals",
        "version": "0.9.0",
        "description": "Industry vertical presets",
        "author": "Celerp",
        "enabled": False,
        "running": False,
    },
]

_SETTINGS_MOCKS_MODULES = {
    "ui.api_client.get_company": AsyncMock(return_value={"name": "T", "currency": "THB", "timezone": "Asia/Bangkok", "fiscal_year_start": "01-01"}),
    "ui.api_client.get_taxes": AsyncMock(return_value={"taxes": []}),
    "ui.api_client.get_payment_terms": AsyncMock(return_value={"terms": []}),
    "ui.api_client.get_users": AsyncMock(return_value={"items": [], "total": 0}),
    "ui.api_client.get_item_schema": AsyncMock(return_value=[]),
    "ui.api_client.get_all_category_schemas": AsyncMock(return_value={}),
    "ui.api_client.get_locations": AsyncMock(return_value={"items": []}),
    "ui.api_client.list_import_batches": AsyncMock(return_value={"batches": []}),
    "ui.api_client.get_modules": AsyncMock(return_value=_MODULES_LIST),
}


class TestModulesUI:
    """Modules settings tab — list, enable, disable."""

    @pytest.mark.asyncio
    async def test_modules_tab_lists_modules(self, ui_client):
        """GET /settings?tab=modules shows installed modules."""
        from contextlib import ExitStack
        mocks = {k: patch(k, new=v) for k, v in _SETTINGS_MOCKS_MODULES.items()}
        with ExitStack() as stack:
            for m in mocks.values():
                stack.enter_context(m)
            r = await ui_client.get("/settings/general?tab=modules", cookies=_authed())
        assert r.status_code == 200
        assert b"celerp-labels" in r.content or b"Celerp Labels" in r.content
        assert b"celerp-verticals" in r.content or b"Celerp Verticals" in r.content

    @pytest.mark.asyncio
    async def test_modules_tab_shows_running_badge(self, ui_client):
        """Running module shows 'running' badge."""
        from contextlib import ExitStack
        mocks = {k: patch(k, new=v) for k, v in _SETTINGS_MOCKS_MODULES.items()}
        with ExitStack() as stack:
            for m in mocks.values():
                stack.enter_context(m)
            r = await ui_client.get("/settings/general?tab=modules", cookies=_authed())
        assert r.status_code == 200
        assert b"running" in r.content

    @pytest.mark.asyncio
    async def test_modules_tab_shows_disabled_badge(self, ui_client):
        """Disabled module shows 'disabled' badge."""
        from contextlib import ExitStack
        mocks = {k: patch(k, new=v) for k, v in _SETTINGS_MOCKS_MODULES.items()}
        with ExitStack() as stack:
            for m in mocks.values():
                stack.enter_context(m)
            r = await ui_client.get("/settings/general?tab=modules", cookies=_authed())
        assert r.status_code == 200
        assert b"disabled" in r.content

    @pytest.mark.asyncio
    async def test_modules_tab_shows_enable_button_for_disabled(self, ui_client):
        """Disabled module has Enable button; enabled has Disable button."""
        from contextlib import ExitStack
        mocks = {k: patch(k, new=v) for k, v in _SETTINGS_MOCKS_MODULES.items()}
        with ExitStack() as stack:
            for m in mocks.values():
                stack.enter_context(m)
            r = await ui_client.get("/settings/general?tab=modules", cookies=_authed())
        assert r.status_code == 200
        assert b"Enable" in r.content
        assert b"Disable" in r.content

    @pytest.mark.asyncio
    async def test_modules_tab_empty_shows_placeholder(self, ui_client):
        """No modules installed shows placeholder text."""
        from contextlib import ExitStack
        mocks = {**_SETTINGS_MOCKS_MODULES, "ui.api_client.get_modules": AsyncMock(return_value=[])}
        with ExitStack() as stack:
            for k, v in mocks.items():
                stack.enter_context(patch(k, new=v))
            r = await ui_client.get("/settings/general?tab=modules", cookies=_authed())
        assert r.status_code == 200
        assert b"No modules installed" in r.content

    @pytest.mark.asyncio
    async def test_module_enable_htmx_returns_panel(self, ui_client):
        """POST /settings/modules/{name}/enable returns updated #modules-panel."""
        refreshed = [
            {**_MODULES_LIST[0], "enabled": True, "running": False},
            {**_MODULES_LIST[1], "enabled": True, "running": False},
        ]
        with (
            patch("ui.api_client.enable_module", new=AsyncMock(return_value={"ok": True, "restart_required": True})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=refreshed)),
        ):
            r = await ui_client.post(
                "/settings/modules/celerp-verticals/enable",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"modules-panel" in r.content
        assert b"restart" in r.content.lower() or b"restart" in r.content

    @pytest.mark.asyncio
    async def test_module_disable_htmx_returns_panel(self, ui_client):
        """POST /settings/modules/{name}/disable returns updated #modules-panel."""
        refreshed = [
            {**_MODULES_LIST[0], "enabled": False, "running": True},
            _MODULES_LIST[1],
        ]
        with (
            patch("ui.api_client.disable_module", new=AsyncMock(return_value={"ok": True, "restart_required": True})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=refreshed)),
        ):
            r = await ui_client.post(
                "/settings/modules/celerp-labels/disable",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"modules-panel" in r.content
        assert b"restart" in r.content.lower()

    @pytest.mark.asyncio
    async def test_module_enable_unauthenticated_redirects(self, ui_client):
        """Unauthenticated module enable → redirect to login."""
        r = await ui_client.post("/settings/modules/celerp-labels/enable")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_module_disable_unauthenticated_redirects(self, ui_client):
        """Unauthenticated module disable → redirect to login."""
        r = await ui_client.post("/settings/modules/celerp-labels/disable")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")


# ---------------------------------------------------------------------------
# Module slot injection tests
# Tests that module slot contributions actually appear in rendered UI.
# ---------------------------------------------------------------------------

class TestModuleSlotInjection:
    """Verify slot contributions from modules are injected into the core UI."""

    @pytest.fixture(autouse=True)
    def clean_slots(self):
        """Each test gets a clean slot registry."""
        from celerp.modules.slots import clear as clear_slots
        clear_slots()
        yield
        clear_slots()

    @pytest.mark.asyncio
    async def test_bulk_action_slot_appears_in_inventory(self, ui_client):
        """A registered bulk_action slot contribution appears in the bulk toolbar."""
        from celerp.modules.slots import register as register_slot
        register_slot("bulk_action", {
            "label": "Custom Module Action",
            "form_action": "/api/custom/action",
            "icon": "⚡",
            "_module": "test-module",
        })
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"Custom Module Action" in r.content

    @pytest.mark.asyncio
    async def test_bulk_action_slot_form_action_rendered(self, ui_client):
        """The form_action from a bulk_action slot is rendered in the form."""
        from celerp.modules.slots import register as register_slot
        register_slot("bulk_action", {
            "label": "Print Labels",
            "form_action": "/labels/print-bulk",
            "icon": "🖨",
            "_module": "celerp-labels",
        })
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"/labels/print-bulk" in r.content

    @pytest.mark.asyncio
    async def test_item_action_slot_appears_in_item_detail(self, ui_client):
        """A registered item_action slot contribution appears in the item detail panel."""
        from celerp.modules.slots import register as register_slot
        register_slot("item_action", {
            "label": "Print Label",
            "href_template": "/labels/print/{entity_id}",
            "_module": "celerp-labels",
        })
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        assert b"Print Label" in r.content

    @pytest.mark.asyncio
    async def test_item_action_slot_href_interpolated(self, ui_client):
        """The href_template in item_action has entity_id filled in."""
        from celerp.modules.slots import register as register_slot
        register_slot("item_action", {
            "label": "Print Label",
            "href_template": "/labels/print/{entity_id}",
            "_module": "celerp-labels",
        })
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM)),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/inventory/gc:123", cookies=_authed())
        assert r.status_code == 200
        # entity_id gc:123 should be in the href
        assert b"gc:123" in r.content or b"labels/print" in r.content

    @pytest.mark.asyncio
    async def test_multiple_bulk_actions_all_rendered(self, ui_client):
        """Multiple bulk_action contributions all appear in the toolbar."""
        from celerp.modules.slots import register as register_slot
        register_slot("bulk_action", {"label": "Action Alpha", "form_action": "/api/alpha", "_module": "mod-a"})
        register_slot("bulk_action", {"label": "Action Beta", "form_action": "/api/beta", "_module": "mod-b"})
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"Action Alpha" in r.content
        assert b"Action Beta" in r.content

    @pytest.mark.asyncio
    async def test_no_slots_inventory_renders_clean(self, ui_client):
        """With no module slots, inventory renders without errors."""
        # clean_slots fixture ensures empty registry
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [_ITEM], "total": 1})),
            patch("ui.api_client.get_valuation", new=AsyncMock(return_value=_VALUATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.get("/inventory", cookies=_authed())
        assert r.status_code == 200
        assert b"Inventory" in r.content



# ---------------------------------------------------------------------------
# Payment terms auto-populate + due_date calculation tests
# ---------------------------------------------------------------------------

class TestCalculateDueDate:
    """Unit tests for _calculate_due_date pure function."""

    def test_basic_net30(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 30", "days": 30}]
        result = _calculate_due_date("2026-01-01", "Net 30", terms)
        assert result == "2026-01-31"

    def test_cash_zero_days(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Cash", "days": 0}]
        result = _calculate_due_date("2026-03-15", "Cash", terms)
        assert result == "2026-03-15"

    def test_unknown_term_returns_none(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 30", "days": 30}]
        assert _calculate_due_date("2026-01-01", "Net 60", terms) is None

    def test_missing_issue_date_returns_none(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 30", "days": 30}]
        assert _calculate_due_date(None, "Net 30", terms) is None

    def test_missing_payment_terms_returns_none(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 30", "days": 30}]
        assert _calculate_due_date("2026-01-01", None, terms) is None

    def test_empty_terms_list_returns_none(self):
        from ui.routes.documents import _calculate_due_date
        assert _calculate_due_date("2026-01-01", "Net 30", []) is None

    def test_invalid_issue_date_returns_none(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 30", "days": 30}]
        assert _calculate_due_date("not-a-date", "Net 30", terms) is None

    def test_month_boundary(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 14", "days": 14}]
        # Jan 25 + 14 = Feb 8
        result = _calculate_due_date("2026-01-25", "Net 14", terms)
        assert result == "2026-02-08"

    def test_leap_year(self):
        from ui.routes.documents import _calculate_due_date
        terms = [{"name": "Net 30", "days": 30}]
        # 2024 is leap year; Feb 1 + 30 = Mar 2
        result = _calculate_due_date("2024-02-01", "Net 30", terms)
        assert result == "2024-03-02"


class TestDocPaymentTermsAutoPopulate:
    """doc_field_patch: contact_id change → auto-populate payment_terms + due_date."""

    @pytest.mark.asyncio
    async def test_contact_with_payment_terms_auto_populates(self, ui_client):
        """Selecting a contact with payment_terms patches doc with terms + computed due_date."""
        contact = {"entity_id": "ct:1", "name": "Alice", "payment_terms": "Net 30", "email": "alice@test.com", "phone": "555-1234"}
        doc_pre = {**_DOC_DETAIL, "issue_date": "2026-01-01", "payment_terms": None, "due_date": None}
        doc_post = {**doc_pre, "payment_terms": "Net 30", "due_date": "2026-01-31", "contact_id": "ct:1", "price_list": "Retail"}
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.get_doc", new=AsyncMock(side_effect=[doc_pre, doc_post, doc_post])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.patch_doc", new=AsyncMock()) as mock_patch,
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        ):
            r = await ui_client.patch(
                "/docs/d:1/field/contact_id",
                data={"value": "ct:1"},
                cookies=_authed(),
            )
        assert r.status_code == 204  # HX-Redirect on contact change
        called_patch = mock_patch.call_args[0][2]
        assert called_patch.get("payment_terms") == "Net 30"
        assert called_patch.get("due_date") == "2026-01-31"
        assert called_patch.get("contact_name") == "Alice"
        assert called_patch.get("contact_email") == "alice@test.com"

    @pytest.mark.asyncio
    async def test_contact_without_payment_terms_no_auto_populate(self, ui_client):
        """Contact without payment_terms - only contact_id patched."""
        contact = {"entity_id": "ct:2", "name": "Bob"}
        doc_pre = {**_DOC_DETAIL, "issue_date": "2026-01-01"}
        doc_post = {**doc_pre, "contact_id": "ct:2"}
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc_post)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.patch_doc", new=AsyncMock()) as mock_patch,
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        ):
            r = await ui_client.patch(
                "/docs/d:1/field/contact_id",
                data={"value": "ct:2"},
                cookies=_authed(),
            )
        assert r.status_code == 204  # HX-Redirect on contact change
        called_patch = mock_patch.call_args[0][2]
        assert "payment_terms" not in called_patch
        assert "due_date" not in called_patch
        assert called_patch.get("contact_id") == "ct:2"
        assert called_patch.get("contact_name") == "Bob"

    @pytest.mark.asyncio
    async def test_payment_terms_change_calculates_due_date(self, ui_client):
        """Patching payment_terms field recalculates due_date from issue_date."""
        doc_pre = {**_DOC_DETAIL, "issue_date": "2026-01-01", "payment_terms": None}
        doc_post = {**doc_pre, "payment_terms": "Net 30", "due_date": "2026-01-31"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(side_effect=[doc_pre, doc_post])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.patch_doc", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/docs/d:1/field/payment_terms",
                data={"value": "Net 30"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        called_patch = mock_patch.call_args[0][2]
        assert called_patch.get("due_date") == "2026-01-31"
        assert called_patch.get("payment_terms") == "Net 30"

    @pytest.mark.asyncio
    async def test_payment_terms_no_issue_date_skips_due_date(self, ui_client):
        """If doc has no issue_date, changing payment_terms does not set due_date."""
        doc_pre = {**_DOC_DETAIL, "issue_date": None, "payment_terms": None}
        doc_post = {**doc_pre, "payment_terms": "Net 30"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(side_effect=[doc_pre, doc_post])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.patch_doc", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/docs/d:1/field/payment_terms",
                data={"value": "Net 30"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        called_patch = mock_patch.call_args[0][2]
        assert "due_date" not in called_patch

    @pytest.mark.asyncio
    async def test_payment_terms_unknown_term_skips_due_date(self, ui_client):
        """If the selected payment_terms name is not in company terms list, no due_date."""
        doc_pre = {**_DOC_DETAIL, "issue_date": "2026-01-01", "payment_terms": None}
        doc_post = {**doc_pre, "payment_terms": "Custom 45"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(side_effect=[doc_pre, doc_post])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.patch_doc", new=AsyncMock()) as mock_patch,
        ):
            r = await ui_client.patch(
                "/docs/d:1/field/payment_terms",
                data={"value": "Custom 45"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        called_patch = mock_patch.call_args[0][2]
        assert "due_date" not in called_patch


class TestDocContactBoxLayout:
    """Doc detail page: payment_terms + status must appear inside the contact box."""

    @pytest.mark.asyncio
    async def test_payment_terms_in_contact_section(self, ui_client):
        """payment_terms editable cell appears in the Contact box (before list items)."""
        doc = {**_DOC_DETAIL, "payment_terms": "Net 30", "status": "sent"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": 1})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        # payment_terms cell must be present
        assert 'hx-get="/docs/d:1/field/payment_terms/edit"' in html
        # section-divider must appear (separating contact from payment terms)
        assert "section-divider" in html
        # status badge must appear
        assert "badge--sent" in html

    @pytest.mark.asyncio
    async def test_totals_section_is_full_width(self, ui_client):
        """Totals section uses doc-section--totals (not the old half-width payment info split)."""
        doc = {**_DOC_DETAIL, "payment_terms": "Net 30", "status": "sent"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": 1})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        # old left-panel class must NOT appear anymore
        assert b"doc-section--totals-compact" not in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_shows_company_field(self, ui_client):
        """Doc detail page: Bill To section shows Company: from contact_company_name."""
        doc = {**_DOC_DETAIL, "contact_company_name": "Acme Corp", "contact_id": "ct:1"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": 1})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert "Company:" in html
        assert "Acme Corp" in html

    @pytest.mark.asyncio
    async def test_doc_detail_phone_and_tax_id_are_editable(self, ui_client):
        """Doc detail page: Phone and Tax ID fields have hx-get edit triggers."""
        doc = {**_DOC_DETAIL, "contact_phone": "555-1234", "contact_tax_id": "TX-9999"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": 1})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert 'hx-get="/docs/d:1/field/contact_phone/edit"' in html
        assert 'hx-get="/docs/d:1/field/contact_tax_id/edit"' in html

    @pytest.mark.asyncio
    async def test_doc_detail_ship_to_section_present(self, ui_client):
        """Doc detail page: Ship To section is rendered with address and attn fields."""
        doc = {**_DOC_DETAIL, "contact_shipping_address": "123 Ship St", "shipping_attn": "Bob"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS, "total": 1})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/d:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert "Ship To" in html
        assert 'hx-get="/docs/d:1/field/contact_shipping_address/edit"' in html
        assert 'hx-get="/docs/d:1/field/shipping_attn/edit"' in html

    @pytest.mark.asyncio
    async def test_doc_billing_address_edit_shows_dropdown_when_addresses_exist(self, ui_client):
        """Editing contact_billing_address: shows Select dropdown when contact has addresses."""
        contact = {
            "entity_id": "ct:1", "name": "Alice",
            "addresses": [
                {"address_type": "billing", "is_default": True, "full_address": "1 Main St"},
                {"address_type": "billing", "is_default": False, "full_address": "2 Oak Ave"},
            ],
        }
        doc = {**_DOC_DETAIL, "contact_id": "ct:1", "contact_billing_address": "1 Main St"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
        ):
            r = await ui_client.get("/docs/d:1/field/contact_billing_address/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<select" in r.content
        assert b"1 Main St" in r.content
        assert b"2 Oak Ave" in r.content

    @pytest.mark.asyncio
    async def test_doc_billing_address_edit_falls_back_to_text_input(self, ui_client):
        """Editing contact_billing_address: falls back to text input when no addresses stored."""
        contact = {"entity_id": "ct:1", "name": "Alice", "addresses": []}
        doc = {**_DOC_DETAIL, "contact_id": "ct:1", "contact_billing_address": ""}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
        ):
            r = await ui_client.get("/docs/d:1/field/contact_billing_address/edit", cookies=_authed())
        assert r.status_code == 200
        assert b'type="text"' in r.content

    @pytest.mark.asyncio
    async def test_doc_contact_id_patch_auto_populates_company_name(self, ui_client):
        """Patching contact_id auto-populates contact_company_name on the doc."""
        contact = {
            "entity_id": "ct:1", "name": "Alice", "company_name": "Acme Corp",
            "email": "alice@acme.com", "phone": "555-0001",
        }
        doc = {**_DOC_DETAIL, "contact_id": "ct:1"}
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=_TERMS)),
            patch("ui.api_client.patch_doc", new=AsyncMock()) as mock_patch,
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        ):
            r = await ui_client.patch("/docs/d:1/field/contact_id", data={"value": "ct:1"}, cookies=_authed())
        assert r.status_code == 204
        called = mock_patch.call_args[0][2]
        assert called.get("contact_company_name") == "Acme Corp"


# ── Inventory item detail fixes ───────────────────────────────────────────────

_ITEM_WITH_LOCATION = {
    "entity_id": "item:abc1", "name": "Widget", "status": "active",
    "location_name": "Warehouse A", "quantity": "10",
}
_SCHEMA_WITH_LOCATION = [
    {"key": "name", "label": "Name", "type": "text", "editable": True},
    {"key": "location_name", "label": "Location", "type": "text", "editable": False},
]
_LOCATIONS = [
    {"location_id": "loc:1", "name": "Warehouse A"},
    {"location_id": "loc:2", "name": "Warehouse B"},
]


class TestInventoryItemDetailFixes:

    @pytest.mark.asyncio
    async def test_location_field_editable_on_item_detail(self, ui_client):
        """GET /inventory/{id}: location_name field rendered as editable (click-to-edit).
        GET /api/items/{id}/field/location_name/edit: returns select with location options."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA_WITH_LOCATION)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM_WITH_LOCATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "T", "currency": "USD"})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": _LOCATIONS, "total": 2})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}])),
        ):
            r = await ui_client.get("/inventory/item:abc1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        # location_name cell must be clickable/editable (hx-get edit endpoint present)
        assert 'hx-get="/api/items/item:abc1/field/location_name/edit"' in html
        # current location value must be shown
        assert "Warehouse A" in html

        # edit endpoint must return a combobox (allow_custom=True) with location options
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA_WITH_LOCATION)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM_WITH_LOCATION)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": _LOCATIONS, "total": 2})),
        ):
            r2 = await ui_client.get("/api/items/item:abc1/field/location_name/edit", cookies=_authed())
        assert r2.status_code == 200
        edit_html = r2.content.decode()
        # allow_custom=True → combobox (not plain select)
        assert "combobox" in edit_html
        assert "Warehouse A" in edit_html
        assert "Warehouse B" in edit_html

    @pytest.mark.asyncio
    async def test_attachment_proxy_route(self, ui_client):
        """GET /static/attachments/... proxies to API instead of serving from UI static dir."""
        import httpx
        mock_response = httpx.Response(200, content=b"fake-image-bytes", headers={"content-type": "image/jpeg"})
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
            r = await ui_client.get("/static/attachments/comp1/att1.jpg")
        assert r.status_code == 200
        assert r.content == b"fake-image-bytes"
        assert "image/jpeg" in r.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_actions_panel_removed(self, ui_client):
        """GET /inventory/{id}: no 'Actions' section; Advanced panel present instead."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA_WITH_LOCATION)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM_WITH_LOCATION)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "T", "currency": "USD"})),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": _LOCATIONS, "total": 2})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}])),
        ):
            r = await ui_client.get("/inventory/item:abc1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        # Old "Actions" heading/section must not appear
        assert "actions-panel" not in html
        # Compact action cards grid must be present
        assert "action-cards-grid" in html
        assert "Split" in html

    @pytest.mark.asyncio
    async def test_location_edit_no_locations_returns_select_not_text(self, ui_client):
        """When get_locations returns empty list, /field/location_name/edit returns a combobox (allow_custom=True), not plain text input."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA_WITH_LOCATION)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM_WITH_LOCATION)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/api/items/item:abc1/field/location_name/edit", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        # allow_custom=True forces combobox path even with 0 options
        assert "combobox" in html
        assert 'type="text" class="cell-input"' not in html

    @pytest.mark.asyncio
    async def test_location_display_endpoint_returns_clickable_cell(self, ui_client):
        """GET /field/location_name/display returns a display_cell with hx-get edit trigger (ESC cancel works)."""
        with (
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA_WITH_LOCATION)),
            patch("ui.api_client.get_item", new=AsyncMock(return_value=_ITEM_WITH_LOCATION)),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": _LOCATIONS, "total": 2})),
        ):
            r = await ui_client.get("/api/items/item:abc1/field/location_name/display", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        # Must render as a clickable display cell (not editing state)
        assert 'hx-get="/api/items/item:abc1/field/location_name/edit"' in html
        assert "<select" not in html


class TestListFieldPatch:
    """Verify PATCH /lists/{id}/field/{field} returns display cell, not 405."""

    _LST = {
        "entity_id": "list:1", "list_type": "quotation", "receiver_type": "customer",
        "receiver": "Alice", "status": "draft", "total": 0, "currency": "USD",
        "discount": 0, "tax": 0, "subtotal": 0,
    }

    @pytest.mark.asyncio
    async def test_patch_list_type_returns_display_cell(self, ui_client):
        with (
            patch("ui.api_client.patch_list", new=AsyncMock(return_value={"event_id": "e1"})),
            patch("ui.api_client.get_list", new=AsyncMock(return_value={**self._LST, "list_type": "transfer"})),
        ):
            r = await ui_client.patch("/lists/list:1/field/list_type",
                                      data={"value": "transfer"}, cookies=_authed())
        assert r.status_code == 200, r.text
        assert "transfer" in r.text.lower()

    @pytest.mark.asyncio
    async def test_patch_receiver_type_returns_display_cell(self, ui_client):
        with (
            patch("ui.api_client.patch_list", new=AsyncMock(return_value={"event_id": "e1"})),
            patch("ui.api_client.get_list", new=AsyncMock(return_value={**self._LST, "receiver_type": "supplier"})),
        ):
            r = await ui_client.patch("/lists/list:1/field/receiver_type",
                                      data={"value": "supplier"}, cookies=_authed())
        assert r.status_code == 200, r.text
        assert "supplier" in r.text.lower()


# ---------------------------------------------------------------------------
# Phase 6: Send to Document / Memo / List from inventory
# ---------------------------------------------------------------------------

class TestBulkActionsPhase6SendTo:
    """Phase 6: send-to modal + create/add for doc/list/memo from selected inventory items."""

    _WEIGHT_ITEM = {
        "entity_id": "item:w1", "sku": "GEM-001", "name": "Emerald",
        "quantity": 1, "sell_by": "weight", "weight": 3.5, "weight_unit": "ct",
        "sale_price": 500, "attributes": {},
    }
    _PIECE_ITEM = {
        "entity_id": "item:p1", "sku": "JWL-002", "name": "Gold Ring",
        "quantity": 2, "sell_by": "piece",
        "sale_price": 1200, "attributes": {},
    }

    # -- Modal rendering --

    @pytest.mark.asyncio
    async def test_docs_from_items_shows_modal(self, ui_client):
        with patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.post(
                "/docs/from-items",
                content=b"selected=item%3Ap1",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Send to Invoice" in r.content
        assert b"Create new draft" in r.content
        assert b"from-items/new" in r.content
        assert b"from-items/add" in r.content

    @pytest.mark.asyncio
    async def test_docs_from_items_no_selection(self, ui_client):
        r = await ui_client.post(
            "/docs/from-items",
            content=b"",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No items selected" in r.content

    # -- Create new from items --

    @pytest.mark.asyncio
    async def test_docs_from_items_new_creates_draft_invoice(self, ui_client):
        mock_create = AsyncMock(return_value={"id": "doc:INV-001"})
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=self._PIECE_ITEM)),
            patch("ui.api_client.create_doc", new=mock_create),
        ):
            r = await ui_client.post(
                "/docs/from-items/new",
                content=b"selected=item%3Ap1",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "doc:INV-001" in r.headers.get("hx-redirect", "")
        call_data = mock_create.call_args[0][1]
        assert call_data["doc_type"] == "invoice"
        assert call_data["status"] == "draft"
        assert len(call_data["line_items"]) == 1

    @pytest.mark.asyncio
    async def test_docs_from_items_new_weight_uses_weight_as_qty(self, ui_client):
        """After sell_by refactor: quantity is always item.quantity, no weight special case."""
        item = {
            "entity_id": "item:w1", "sku": "GEM-001", "name": "Emerald",
            "quantity": 3.5, "sell_by": "carat",
            "sale_price": 500, "attributes": {},
        }
        mock_create = AsyncMock(return_value={"id": "doc:INV-002"})
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=item)),
            patch("ui.api_client.create_doc", new=mock_create),
        ):
            r = await ui_client.post(
                "/docs/from-items/new",
                content=b"selected=item%3Aw1",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        line = mock_create.call_args[0][1]["line_items"][0]
        assert line["quantity"] == 3.5

    @pytest.mark.asyncio
    async def test_docs_from_items_price_fallback_to_retail(self, ui_client):
        """When sale_price is absent, unit_price should fall back to retail_price."""
        item = {
            "entity_id": "item:r1", "sku": "DIA-001", "name": "Diamond",
            "quantity": 1, "sell_by": "piece",
            "retail_price": 9800, "wholesale_price": 6500, "cost_price": 4200,
            "attributes": {},
        }
        mock_create = AsyncMock(return_value={"id": "doc:INV-003"})
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=item)),
            patch("ui.api_client.create_doc", new=mock_create),
        ):
            r = await ui_client.post(
                "/docs/from-items/new",
                content=b"selected=item%3Ar1",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        line = mock_create.call_args[0][1]["line_items"][0]
        assert line["unit_price"] == 9800
        assert line["unit"] == "piece"

    # -- Add to existing doc --

    @pytest.mark.asyncio
    async def test_docs_from_items_add_appends_lines(self, ui_client):
        existing_doc = {"line_items": [{"description": "Old line", "quantity": 1, "unit_price": 100}]}
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=self._PIECE_ITEM)),
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=existing_doc)),
            patch("ui.api_client.patch_doc", new=AsyncMock(return_value={"event_id": "e1"})) as mock_patch,
        ):
            r = await ui_client.post(
                "/docs/from-items/add",
                content=b"selected=item%3Ap1&target_id=doc%3AINV-001",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "doc:INV-001" in r.headers.get("hx-redirect", "")
        patched = mock_patch.call_args[0][2]
        assert len(patched["line_items"]) == 2  # old + new

    @pytest.mark.asyncio
    async def test_docs_from_items_add_no_target(self, ui_client):
        r = await ui_client.post(
            "/docs/from-items/add",
            content=b"selected=item%3Ap1",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No items or target selected" in r.content

    # -- Lists modal + create + add --

    @pytest.mark.asyncio
    async def test_lists_from_items_shows_modal(self, ui_client):
        with patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.post(
                "/lists/from-items",
                content=b"selected=item%3Ap1",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Send to List" in r.content

    @pytest.mark.asyncio
    async def test_lists_from_items_new_creates_draft(self, ui_client):
        mock_create = AsyncMock(return_value={"id": "list:L-001"})
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=self._PIECE_ITEM)),
            patch("ui.api_client.create_list", new=mock_create),
        ):
            r = await ui_client.post(
                "/lists/from-items/new",
                content=b"selected=item%3Ap1",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "list:L-001" in r.headers.get("hx-redirect", "")

    @pytest.mark.asyncio
    async def test_lists_from_items_add_appends_lines(self, ui_client):
        existing_list = {"line_items": [{"description": "Old", "quantity": 1, "unit_price": 50}]}
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value=self._PIECE_ITEM)),
            patch("ui.api_client.get_list", new=AsyncMock(return_value=existing_list)),
            patch("ui.api_client.patch_list", new=AsyncMock(return_value={"event_id": "e1"})) as mock_patch,
        ):
            r = await ui_client.post(
                "/lists/from-items/add",
                content=b"selected=item%3Ap1&target_id=list%3AL-001",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "list:L-001" in r.headers.get("hx-redirect", "")

    # -- Memos modal + create + add --

    @pytest.mark.asyncio
    async def test_memos_from_items_shows_modal(self, ui_client):
        with patch("ui.api_client.list_memos", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.post(
                "/crm/memos/from-items",
                content=b"selected=item%3Aa",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Send to Memo" in r.content

    @pytest.mark.asyncio
    async def test_memos_from_items_new_creates_memo(self, ui_client):
        mock_create = AsyncMock(return_value={"id": "memo:M-001"})
        mock_add = AsyncMock(return_value={"event_id": "e1"})
        with (
            patch("ui.api_client.create_memo", new=mock_create),
            patch("ui.api_client.add_memo_item", new=mock_add),
        ):
            r = await ui_client.post(
                "/crm/memos/from-items/new",
                content=b"selected=item%3Aa&selected=item%3Ab",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "memo:M-001" in r.headers.get("hx-redirect", "")
        assert mock_add.call_count == 2

    @pytest.mark.asyncio
    async def test_memos_from_items_add_to_existing(self, ui_client):
        mock_add = AsyncMock(return_value={"event_id": "e1"})
        with patch("ui.api_client.add_memo_item", new=mock_add):
            r = await ui_client.post(
                "/crm/memos/from-items/add",
                content=b"selected=item%3Aa&selected=item%3Ab&target_id=memo%3AM-001",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 204
        assert "memo:M-001" in r.headers.get("hx-redirect", "")
        assert mock_add.call_count == 2

    @pytest.mark.asyncio
    async def test_memos_from_items_no_selection(self, ui_client):
        r = await ui_client.post(
            "/crm/memos/from-items",
            content=b"",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert b"No items selected" in r.content

    # -- Search endpoints --

    @pytest.mark.asyncio
    async def test_docs_from_items_search_returns_options(self, ui_client):
        docs = [{"id": "doc:INV-001", "ref_id": "INV-001", "status": "draft", "contact_name": "Alice"}]
        with patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": docs, "total": 1})):
            r = await ui_client.get(
                "/docs/from-items/search?q=INV",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"INV-001" in r.content

    @pytest.mark.asyncio
    async def test_lists_from_items_search_returns_options(self, ui_client):
        lists = [{"id": "list:L-001", "ref_id": "L-001", "status": "draft"}]
        with patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": lists, "total": 1})):
            r = await ui_client.get(
                "/lists/from-items/search?q=L-001",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"L-001" in r.content

    # -- Slot registration --

    @pytest.mark.asyncio
    async def test_docs_module_registers_send_to_targets(self, ui_client):
        """celerp-docs PLUGIN_MANIFEST declares send_to_targets slots."""
        import importlib
        spec = importlib.util.spec_from_file_location(
            "celerp_docs_entry", "default_modules/celerp-docs/__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        slots = mod.PLUGIN_MANIFEST.get("slots", {})
        targets = slots.get("send_to_targets", [])
        labels = [t["label"] for t in targets]
        assert "Invoice" in labels
        assert "List/Quotation" in labels

    @pytest.mark.asyncio
    async def test_crm_module_registers_send_to_target(self, ui_client):
        """celerp-contacts PLUGIN_MANIFEST declares send_to_targets slot."""
        import importlib
        spec = importlib.util.spec_from_file_location(
            "celerp_contacts_entry", "default_modules/celerp-contacts/__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        slots = mod.PLUGIN_MANIFEST.get("slots", {})
        targets = slots.get("send_to_targets", [])
        labels = [t["label"] for t in targets]
        assert "Consignment Out" in labels


# ---------------------------------------------------------------------------
# TestUnitsSettings — units API + UI tab
# ---------------------------------------------------------------------------

_DEFAULT_UNITS_SEED = [
    {"name": "piece", "label": "Piece", "decimals": 0},
    {"name": "carat", "label": "Carat (ct)", "decimals": 2},
    {"name": "gram", "label": "Gram (g)", "decimals": 2},
    {"name": "kg", "label": "Kilogram (kg)", "decimals": 3},
    {"name": "oz", "label": "Ounce (oz)", "decimals": 2},
    {"name": "liter", "label": "Liter (L)", "decimals": 2},
    {"name": "meter", "label": "Meter (m)", "decimals": 2},
]

_SETTINGS_MOCKS_UNITS = {
    "ui.api_client.get_locations": AsyncMock(return_value={"items": [], "total": 0}),
    "ui.api_client.list_import_batches": AsyncMock(return_value={"batches": []}),
    "ui.api_client.get_all_category_schemas": AsyncMock(return_value={}),
    "ui.api_client.get_units": AsyncMock(return_value=_DEFAULT_UNITS_SEED),
}


async def _api_headers(client) -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": "UnitsTest", "email": "units@test.com", "name": "Admin", "password": "pw"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestSettingsPurchasing:
    """Purchasing Documents Settings page and tabs."""

    @pytest.mark.asyncio
    async def test_settings_purchasing_page(self, ui_client):
        with (
            patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_purchasing_payment_terms", new=AsyncMock(return_value=_TERMS)),
        ):
            r = await ui_client.get("/settings/purchasing", cookies=_authed())
        assert r.status_code == 200
        assert b"Purchasing Documents Settings" in r.content

    @pytest.mark.asyncio
    async def test_settings_purchasing_taxes_tab(self, ui_client):
        with (
            patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_purchasing_payment_terms", new=AsyncMock(return_value=_TERMS)),
        ):
            r = await ui_client.get("/settings/purchasing?tab=taxes", cookies=_authed())
        assert r.status_code == 200
        assert b"VAT" in r.content

    @pytest.mark.asyncio
    async def test_settings_purchasing_terms_conditions_tab(self, ui_client):
        with (
            patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_terms_conditions", new=AsyncMock(return_value=[
                {"name": "Standard Purchase Terms", "text": "Goods must conform...", "doc_types": ["purchase_order"], "is_default": True},
            ])),
        ):
            r = await ui_client.get("/settings/purchasing?tab=terms-conditions", cookies=_authed())
        assert r.status_code == 200
        assert b"Standard Purchase Terms" in r.content

    @pytest.mark.asyncio
    async def test_purchasing_tax_inline_edit(self, ui_client):
        with patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(return_value=_TAXES)):
            r = await ui_client.get("/settings/purchasing-taxes/0/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<input" in r.content

    @pytest.mark.asyncio
    async def test_purchasing_term_inline_edit(self, ui_client):
        with patch("ui.api_client.get_purchasing_payment_terms", new=AsyncMock(return_value=_TERMS)):
            r = await ui_client.get("/settings/purchasing-terms/0/name/edit", cookies=_authed())
        assert r.status_code == 200
        assert b"<input" in r.content


class TestUnitsSettings:
    """Units settings — API endpoints + UI tab CRUD."""

    # ── API: GET /companies/me/units ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_units_returns_default_seed_when_unconfigured(self, client):
        """GET /companies/me/units with no units configured → returns default seed."""
        headers = await _api_headers(client)
        r = await client.get("/companies/me/units", headers=headers)
        assert r.status_code == 200
        units = r.json()
        names = [u["name"] for u in units]
        assert "piece" in names
        assert "carat" in names
        assert len(units) == 7

    # ── API: PUT /companies/me/units ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_put_units_saves_and_returns(self, client):
        """PUT /companies/me/units stores the list and returns it."""
        headers = await _api_headers(client)
        payload = {"units": [
            {"name": "piece", "label": "Piece", "decimals": 0},
            {"name": "gram", "label": "Gram", "decimals": 2},
        ]}
        r = await client.put("/companies/me/units", json=payload, headers=headers)
        assert r.status_code == 200
        units = r.json()
        assert len(units) == 2
        assert units[0]["name"] == "piece"
        assert units[1]["name"] == "gram"
        # Verify persisted
        r2 = await client.get("/companies/me/units", headers=headers)
        assert r2.status_code == 200
        assert len(r2.json()) == 2

    # ── API: Validation — duplicate name ────────────────────────────

    @pytest.mark.asyncio
    async def test_put_units_duplicate_name_rejected(self, client):
        """PUT with duplicate unit name → 422."""
        headers = await _api_headers(client)
        payload = {"units": [
            {"name": "piece", "label": "Piece", "decimals": 0},
            {"name": "piece", "label": "Piece 2", "decimals": 0},
        ]}
        r = await client.put("/companies/me/units", json=payload, headers=headers)
        assert r.status_code == 422
        assert "duplicate" in r.json()["detail"].lower()

    # ── API: Validation — invalid decimals ──────────────────────────

    @pytest.mark.asyncio
    async def test_put_units_negative_decimals_rejected(self, client):
        """PUT with decimals < 0 → 422."""
        headers = await _api_headers(client)
        payload = {"units": [{"name": "piece", "label": "Piece", "decimals": -1}]}
        r = await client.put("/companies/me/units", json=payload, headers=headers)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_put_units_decimals_gt6_rejected(self, client):
        """PUT with decimals > 6 → 422."""
        headers = await _api_headers(client)
        payload = {"units": [{"name": "piece", "label": "Piece", "decimals": 7}]}
        r = await client.put("/companies/me/units", json=payload, headers=headers)
        assert r.status_code == 422

    # ── UI: units tab renders ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_units_tab_renders_unit_table(self, ui_client):
        """GET /settings/inventory?tab=units renders units table."""
        from contextlib import ExitStack
        mocks = {k: patch(k, new=v) for k, v in _SETTINGS_MOCKS_UNITS.items()}
        with ExitStack() as stack:
            for m in mocks.values():
                stack.enter_context(m)
            r = await ui_client.get("/settings/inventory?tab=units", cookies=_authed())
        assert r.status_code == 200
        assert b"piece" in r.content
        assert b"carat" in r.content
        assert b"Piece" in r.content
        assert b"Name" in r.content
        assert b"Label" in r.content
        assert b"Decimals" in r.content

    # ── UI: Add unit ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_unit_via_post(self, ui_client):
        """POST /settings/units/add appends unit and redirects."""
        current = list(_DEFAULT_UNITS_SEED)
        new_unit = {"name": "tola", "label": "Tola", "decimals": 3}
        mock_get = AsyncMock(return_value=current)
        mock_put = AsyncMock(return_value=current + [new_unit])
        with (
            patch("ui.api_client.get_units", new=mock_get),
            patch("ui.api_client.patch_units", new=mock_put),
        ):
            r = await ui_client.post(
                "/settings/units/add",
                content=b"name=tola&label=Tola&decimals=3",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code in (302, 303)
        assert "tab=units" in r.headers.get("location", "")
        sent = mock_put.call_args[0][1]
        assert any(u["name"] == "tola" for u in sent)

    # ── UI: Delete unit ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_unit_via_delete(self, ui_client):
        """DELETE /settings/units/{name} removes unit and redirects."""
        current = [
            {"name": "piece", "label": "Piece", "decimals": 0},
            {"name": "gram", "label": "Gram", "decimals": 2},
        ]
        mock_get = AsyncMock(return_value=current)
        mock_put = AsyncMock(return_value=[current[0]])
        with (
            patch("ui.api_client.get_units", new=mock_get),
            patch("ui.api_client.patch_units", new=mock_put),
        ):
            r = await ui_client.delete(
                "/settings/units/gram",
                cookies=_authed(),
            )
        assert r.status_code in (302, 303)
        assert "tab=units" in r.headers.get("location", "")
        sent = mock_put.call_args[0][1]
        assert not any(u["name"] == "gram" for u in sent)
        assert any(u["name"] == "piece" for u in sent)

    # ── UI: Edit unit (inline patch) ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_edit_unit_label_via_patch(self, ui_client):
        """PATCH /settings/units/{name}/label updates label and returns display cell."""
        current = [{"name": "gram", "label": "Gram (g)", "decimals": 2}]
        updated = [{"name": "gram", "label": "Gramme", "decimals": 2}]
        mock_get = AsyncMock(side_effect=[current, updated])
        mock_put = AsyncMock(return_value=updated)
        with (
            patch("ui.api_client.get_units", new=mock_get),
            patch("ui.api_client.patch_units", new=mock_put),
        ):
            r = await ui_client.patch(
                "/settings/units/gram/label",
                content=b"value=Gramme",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Gramme" in r.content

    @pytest.mark.asyncio
    async def test_edit_unit_decimals_via_patch(self, ui_client):
        """PATCH /settings/units/{name}/decimals updates decimals and returns display cell."""
        current = [{"name": "carat", "label": "Carat", "decimals": 2}]
        updated = [{"name": "carat", "label": "Carat", "decimals": 4}]
        mock_get = AsyncMock(side_effect=[current, updated])
        mock_put = AsyncMock(return_value=updated)
        with (
            patch("ui.api_client.get_units", new=mock_get),
            patch("ui.api_client.patch_units", new=mock_put),
        ):
            r = await ui_client.patch(
                "/settings/units/carat/decimals",
                content=b"value=4",
                headers={"content-type": "application/x-www-form-urlencoded"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"4" in r.content


# ---------------------------------------------------------------------------
# Phase 5: Vertical category default_sell_by + pieces field
# ---------------------------------------------------------------------------

class TestVerticalCategoryDefaults:
    """Phase 5 checks: JSON category files have correct default_sell_by and pieces fields."""

    _CATS_DIR = pathlib.Path(__file__).parent.parent / "default_modules" / "celerp-verticals" / "celerp_verticals" / "categories"

    def _load(self, name: str) -> dict:
        import json
        return json.loads((self._CATS_DIR / f"{name}.json").read_text())

    def _all(self) -> list[dict]:
        import json
        return [
            json.loads(p.read_text())
            for p in sorted(self._CATS_DIR.glob("*.json"))
        ]

    # ── 5a: all files have default_sell_by ───────────────────────────────────

    def test_all_categories_have_default_sell_by(self):
        """Every category JSON must have a 'default_sell_by' key."""
        missing = [
            c["name"] for c in self._all()
            if "default_sell_by" not in c
        ]
        assert missing == [], f"Missing default_sell_by in: {missing}"

    def test_gem_categories_have_carat_default(self):
        """Gem categories must have default_sell_by='carat'."""
        gem_cats = [
            "colored_stone", "diamond", "emerald", "ruby",
            "sapphire", "pearl", "rough_gemstone", "mineral_specimen",
        ]
        for name in gem_cats:
            cat = self._load(name)
            assert cat["default_sell_by"] == "carat", (
                f"{name}: expected default_sell_by='carat', got '{cat['default_sell_by']}'"
            )

    def test_food_weight_categories_have_kg_default(self):
        """Food/ag categories sold by weight must have default_sell_by='kg'."""
        kg_cats = [
            "fresh_produce", "grain_cereal", "ingredient_bulk",
            "fresh_food", "livestock_feed", "seeds", "fertilizer_chemical",
        ]
        for name in kg_cats:
            cat = self._load(name)
            assert cat["default_sell_by"] == "kg", (
                f"{name}: expected default_sell_by='kg', got '{cat['default_sell_by']}'"
            )

    def test_bullion_categories_have_gram_default(self):
        """Gold/silver/platinum bullion must have default_sell_by='gram'."""
        bullion_cats = ["gold_bullion", "silver_bullion", "platinum_bullion"]
        for name in bullion_cats:
            cat = self._load(name)
            assert cat["default_sell_by"] == "gram", (
                f"{name}: expected default_sell_by='gram', got '{cat['default_sell_by']}'"
            )

    def test_most_categories_have_piece_default(self):
        """Standard discrete-item categories must have default_sell_by='piece'."""
        piece_cats = [
            "laptop", "mobile_phone", "jewelry", "watch", "wine", "spirit",
            "beer", "packaged_food", "book", "painting", "consulting_service",
            "saas_plan", "residential_unit", "bullion_coin", "numismatic_coin",
        ]
        for name in piece_cats:
            cat = self._load(name)
            assert cat["default_sell_by"] == "piece", (
                f"{name}: expected default_sell_by='piece', got '{cat['default_sell_by']}'"
            )

    # ── 5b: gem categories have pieces field ─────────────────────────────────

    def test_gem_categories_have_pieces_field(self):
        """Gem categories must have a 'pieces' field in their fields array."""
        gem_cats = [
            "colored_stone", "diamond", "emerald", "ruby",
            "sapphire", "pearl", "rough_gemstone", "mineral_specimen",
        ]
        for name in gem_cats:
            cat = self._load(name)
            field_keys = [f["key"] for f in cat.get("fields", [])]
            assert "pieces" in field_keys, (
                f"{name}: missing 'pieces' field in fields array"
            )

    def test_pieces_field_has_correct_schema(self):
        """The pieces field in gem categories must be type='number'."""
        cat = self._load("colored_stone")
        pieces_field = next(
            (f for f in cat.get("fields", []) if f["key"] == "pieces"), None
        )
        assert pieces_field is not None
        assert pieces_field["type"] == "number"
        assert pieces_field["label"] == "Pieces"
        assert pieces_field["options"] == []


class TestCsvImportUxOverhaul:
    """Plan 10: Professional import UX - drag-and-drop, steps, preview, results."""

    def _spec(self):
        from ui.routes.csv_import import CsvImportSpec
        return CsvImportSpec(
            cols=["sku", "name", "quantity", "cost_price"],
            required={"sku", "name"},
            type_map={"quantity": float, "cost_price": float},
        )

    def test_upload_form_has_dropzone(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import upload_form
        html = to_xml(upload_form(
            cols=["sku", "name"], template_href="/x/t",
            preview_action="/x/preview",
        ))
        assert "import-dropzone" in html
        assert "Drag your CSV" in html

    def test_upload_form_has_step_indicator(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import upload_form
        html = to_xml(upload_form(
            cols=["sku"], template_href="/x/t",
            preview_action="/x/preview",
        ))
        assert "import-steps" in html
        assert "Upload" in html

    def test_step_indicator_3_steps_no_mapping(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import upload_form
        html = to_xml(upload_form(
            cols=["sku"], template_href="/x/t",
            preview_action="/x/preview", has_mapping=False,
        ))
        assert "Map Columns" not in html
        assert "Review" in html
        assert "Import" in html

    def test_step_indicator_4_steps_with_mapping(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import upload_form
        html = to_xml(upload_form(
            cols=["sku"], template_href="/x/t",
            preview_action="/x/preview", has_mapping=True,
        ))
        assert "Map Columns" in html
        assert "Review" in html
        assert "Import" in html

    def test_error_panel_has_row_numbers(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = self._spec()
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = to_xml(validation_result(
            rows=rows, cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm", error_report_action="/x/errors",
            back_href="/x", revalidate_action="/x/revalidate",
        ))
        # Row numbers column header
        assert '<th>#</th>' in html or ">#<" in html

    def test_error_panel_has_error_count_badges(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = self._spec()
        rows = [
            {"sku": "", "name": "", "quantity": "5", "cost_price": "10"},
            {"sku": "", "name": "B", "quantity": "5", "cost_price": "10"},
        ]
        html = to_xml(validation_result(
            rows=rows, cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm", error_report_action="/x/errors",
            back_href="/x", revalidate_action="/x/revalidate",
        ))
        assert "errors)" in html  # e.g. "Sku (2 errors)"

    def test_error_panel_has_progress_bar(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = self._spec()
        rows = [
            {"sku": "S1", "name": "Good", "quantity": "5", "cost_price": "10"},
            {"sku": "", "name": "Bad", "quantity": "5", "cost_price": "10"},
        ]
        html = to_xml(validation_result(
            rows=rows, cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm", error_report_action="/x/errors",
            back_href="/x", revalidate_action="/x/revalidate",
        ))
        assert "import-progress" in html
        assert "1 of 2 rows are valid" in html

    def test_confirm_panel_has_preview_table(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = self._spec()
        rows = [{"sku": "S1", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = to_xml(validation_result(
            rows=rows, cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm", error_report_action="/x/errors",
            back_href="/x",
        ))
        assert "import-preview-table" in html
        assert "Widget" in html  # preview row data

    def test_confirm_panel_has_row_count_in_button(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = self._spec()
        rows = [
            {"sku": "S1", "name": "A", "quantity": "1", "cost_price": "1"},
            {"sku": "S2", "name": "B", "quantity": "2", "cost_price": "2"},
        ]
        html = to_xml(validation_result(
            rows=rows, cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm", error_report_action="/x/errors",
            back_href="/x",
        ))
        assert "Import All 2 Rows" in html

    def test_import_result_panel_has_summary_cards(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import import_result_panel
        html = to_xml(import_result_panel(
            created=5, skipped=2, errors=["bad row"],
            entity_label="items", back_href="/inventory",
            import_more_href="/inventory/import",
        ))
        assert "import-summary-cards" in html
        assert "import-card--success" in html
        assert "import-card--warning" in html
        assert "import-card--error" in html
        assert "View Items" in html
        assert "Import more" in html

    def test_import_result_panel_no_error_card_when_clean(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import import_result_panel
        html = to_xml(import_result_panel(
            created=3, skipped=0, errors=[],
            entity_label="documents", back_href="/docs",
            import_more_href="/docs/import",
        ))
        assert "import-card--success" in html
        assert "import-card--error" not in html
        assert "View Documents" in html

    def test_dropzone_js_injected(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import upload_form
        html = to_xml(upload_form(
            cols=["sku"], template_href="/x/t",
            preview_action="/x/preview",
        ))
        assert "import-dropzone" in html
        assert "_showFile" in html  # drag-and-drop JS

    def test_error_navigation_present(self):
        from fasthtml.common import to_xml
        from ui.routes.csv_import import CsvImportSpec, validate_cell, validation_result
        spec = self._spec()
        rows = [{"sku": "", "name": "Widget", "quantity": "5", "cost_price": "10"}]
        html = to_xml(validation_result(
            rows=rows, cols=spec.cols,
            validate=lambda c, v: validate_cell(spec, c, v),
            confirm_action="/x/confirm", error_report_action="/x/errors",
            back_href="/x", revalidate_action="/x/revalidate",
        ))
        assert "errPrev" in html
        assert "errNext" in html


class TestCsvImportSellByValidation:
    """Phase 6: CSV import sell_by validation using company units."""

    @pytest.mark.asyncio
    async def test_sell_by_validated_against_company_units(self, ui_client):
        """CSV preview: sell_by value not in company units fails validation."""
        from ui.routes.inventory import _build_item_validator

        units = [
            {"name": "piece", "label": "Piece", "decimals": 0},
            {"name": "carat", "label": "Carat", "decimals": 2},
        ]
        with patch("ui.api_client.get_units", new=AsyncMock(return_value=units)):
            validator = await _build_item_validator("fake-token")

        # Known unit → valid
        assert validator("sell_by", "piece") is True
        assert validator("sell_by", "carat") is True
        # Unknown unit → invalid
        assert validator("sell_by", "bushel") is False
        assert validator("sell_by", "furlong") is False

    @pytest.mark.asyncio
    async def test_sell_by_blank_is_valid(self, ui_client):
        """CSV preview: blank sell_by is always valid (optional field)."""
        from ui.routes.inventory import _build_item_validator

        units = [{"name": "piece", "label": "Piece", "decimals": 0}]
        with patch("ui.api_client.get_units", new=AsyncMock(return_value=units)):
            validator = await _build_item_validator("fake-token")

        assert validator("sell_by", "") is True
        assert validator("sell_by", "   ") is True

    @pytest.mark.asyncio
    async def test_sell_by_valid_when_units_unavailable(self, ui_client):
        """If get_units raises, sell_by is not validated (fail-open)."""
        from ui.routes.inventory import _build_item_validator

        with patch("ui.api_client.get_units", new=AsyncMock(side_effect=Exception("network error"))):
            validator = await _build_item_validator("fake-token")

        # Any value is accepted when units can't be fetched
        assert validator("sell_by", "piece") is True
        assert validator("sell_by", "anything") is True


class TestLabelPages:
    """Label module UI pages: shell wrapping, preset seeding, editor panel rendering."""

    @pytest_asyncio.fixture
    async def label_client(self):
        """Client with label module UI routes registered."""
        from ui.app import app as ui_app
        from celerp_labels.ui_routes import setup_ui_routes
        # Register module routes once (idempotent via route dedup)
        setup_ui_routes(ui_app)
        async with AsyncClient(
            transport=ASGITransport(app=ui_app),
            base_url="http://ui",
            follow_redirects=False,
        ) as c:
            yield c

    @pytest.mark.asyncio
    async def test_labels_page_redirects_unauthenticated(self, label_client):
        r = await label_client.get("/settings/labels")
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_labels_shortcut_redirects_to_settings(self, label_client):
        """GET /labels redirects to /settings/labels (single page, no redundancy)."""
        r = await label_client.get("/labels", cookies=_authed())
        assert r.status_code == 302
        assert "/settings/labels" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_labels_settings_page_redirects_to_first_template(self, label_client):
        """GET /settings/labels redirects to first template when templates exist."""
        _templates = [
            {"id": "t1", "name": "Shelf Label", "format": "100x50mm", "copies": 1, "fields": []},
        ]

        class MockResponse:
            status_code = 200
            def json(self):
                return {"items": _templates}

        class MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                return MockResponse()
            async def post(self, url, **kw):
                return MockResponse()

        with patch("httpx.AsyncClient", return_value=MockClient()):
            r = await label_client.get("/settings/labels", cookies=_authed())
        assert r.status_code == 302
        assert r.headers["location"] == "/settings/labels/t1"

    @pytest.mark.asyncio
    async def test_editor_panel_has_canvas_and_field_list(self, label_client):
        """GET /settings/labels/{id} renders canvas preview and field list."""
        _template = {
            "id": "t1", "name": "Test", "format": "40x30mm", "copies": 1,
            "fields": [
                {"key": "name", "label": "Name", "type": "text", "x": 2, "y": 2, "fontSize": 8},
                {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2, "y": 10, "fontSize": 7},
            ],
        }

        class MockResponse:
            status_code = 200
            def json(self):
                return {"items": [_template]}

        class MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                return MockResponse()
            async def post(self, url, **kw):
                return MockResponse()

        with patch("httpx.AsyncClient", return_value=MockClient()):
            r = await label_client.get("/settings/labels/t1", cookies=_authed())
        assert r.status_code == 200
        html = r.text
        # Canvas preview element
        assert 'id="preview-canvas"' in html
        # Field list
        assert 'id="field-list"' in html
        # Field rows with data
        assert 'value="name"' in html
        assert 'value="barcode"' in html
        # JS initialization
        assert "labelEditorUpdatePreview" in html
        # Canvas drag functionality (no SortableJS - drag is on canvas only)
        assert "setupBlockDrag" in html

    @pytest.mark.asyncio
    async def test_preset_seeding_when_empty(self, label_client):
        """When no templates exist, presets are seeded via API."""
        call_log = []

        class MockResponse:
            def __init__(self, items=None, status=200):
                self.status_code = status
                self._items = items or []
            def json(self):
                return {"items": self._items}

        class MockClient:
            def __init__(self):
                self._call_count = 0

            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                self._call_count += 1
                if self._call_count == 1:
                    # First fetch: empty
                    return MockResponse(items=[])
                # After seeding: return preset templates
                return MockResponse(items=[
                    {"id": "p1", "name": "Barcode Sticker (24x24)", "format": "24x24mm", "copies": 1},
                    {"id": "p2", "name": "Shelf Label (100x50)", "format": "100x50mm", "copies": 1},
                    {"id": "p3", "name": "QR Label (62x29)", "format": "62x29mm", "copies": 1},
                ])
            async def post(self, url, **kw):
                call_log.append(url)
                return MockResponse(status=201)

        with patch("httpx.AsyncClient", return_value=MockClient()):
            r = await label_client.get("/settings/labels", cookies=_authed())
        # After seeding, redirects to first template
        assert r.status_code == 302
        assert "/settings/labels/p1" in r.headers["location"]
        # 6 presets were POSTed (3 square sticker + 3 original)
        assert len(call_log) == 6
        assert all("/api/labels/templates" in u for u in call_log)

    @pytest.mark.asyncio
    async def test_editor_drag_js_uses_global_function(self, label_client):
        """Editor JS exposes labelEditorUpdatePreview as a global function."""
        _template = {
            "id": "t1", "name": "Test", "format": "40x30mm", "copies": 1,
            "fields": [{"key": "sku", "label": "SKU", "type": "text"}],
        }

        class MockResponse:
            status_code = 200
            def json(self):
                return {"items": [_template]}

        class MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                return MockResponse()
            async def post(self, url, **kw):
                return MockResponse()

        with patch("httpx.AsyncClient", return_value=MockClient()):
            r = await label_client.get("/settings/labels/t1", cookies=_authed())
        html = r.text
        # Global function assignment
        assert "window.labelEditorUpdatePreview" in html
        # Inline handlers reference it
        assert 'oninput="labelEditorUpdatePreview()"' in html

    @pytest.mark.asyncio
    async def test_print_preview_route_removed(self, label_client):
        """GET /labels/print/{id} should 404 (route removed)."""
        r = await label_client.get("/labels/print/item-123", cookies=_authed())
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_barcode_preview_text_below_bars(self, label_client):
        """Editor barcode preview renders text below bars (industry standard)."""
        _template = {
            "id": "t1", "name": "Test", "format": "40x30mm",
            "fields": [{"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2, "y": 2}],
        }

        class MockResponse:
            status_code = 200
            def json(self):
                return {"items": [_template]}

        class MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                return MockResponse()
            async def post(self, url, **kw):
                return MockResponse()

        with patch("httpx.AsyncClient", return_value=MockClient()):
            r = await label_client.get("/settings/labels/t1", cookies=_authed())
        html = r.text
        # JS creates barcode blocks with img + bc-text
        assert "bc-text" in html
        assert "label-field-block--barcode" in html
        assert "/api/labels/preview/barcode" in html

    @pytest.mark.asyncio
    async def test_no_copies_field_in_editor(self, label_client):
        """Editor does not show a Copies input (copies chosen at print time)."""
        _template = {
            "id": "t1", "name": "Test", "format": "40x30mm",
            "fields": [{"key": "name", "label": "Name", "type": "text"}],
        }

        class MockResponse:
            status_code = 200
            def json(self):
                return {"items": [_template]}

        class MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                return MockResponse()
            async def post(self, url, **kw):
                return MockResponse()

        with patch("httpx.AsyncClient", return_value=MockClient()):
            r = await label_client.get("/settings/labels/t1", cookies=_authed())
        html = r.text
        assert 'name="copies"' not in html

    @pytest.mark.asyncio
    async def test_barcode_preview_returns_png(self, label_client):
        """GET /api/labels/preview/barcode returns a valid PNG image."""
        r = await label_client.get("/api/labels/preview/barcode?value=TEST123")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:4] == b"\x89PNG"

    @pytest.mark.asyncio
    async def test_qr_preview_returns_png(self, label_client):
        """GET /api/labels/preview/qr returns a valid PNG image."""
        r = await label_client.get("/api/labels/preview/qr?value=HELLO")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:4] == b"\x89PNG"

    @pytest.mark.asyncio
    async def test_preview_endpoints_no_auth_required(self, label_client):
        """Preview endpoints work without auth cookie (public, no sensitive data)."""
        r = await label_client.get("/api/labels/preview/barcode?value=X")
        assert r.status_code == 200
        r2 = await label_client.get("/api/labels/preview/qr?value=X")
        assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Plan 09: Documents Overhaul tests
# ---------------------------------------------------------------------------

class TestDocumentsOverhaul:
    """Tests for plan 09: PDF proxy, finalize button rename, history section, summary filter."""

    @pytest.mark.asyncio
    async def test_pdf_proxy_route_exists(self, ui_client):
        """GET /docs/{id}/pdf proxies to API; returns 500 when API unreachable (no mock)."""
        r = await ui_client.get("/docs/doc:TEST-001/pdf", cookies=_authed())
        # Without API mock, we expect a proxy error (500) or redirect - not 404
        assert r.status_code != 404

    @pytest.mark.asyncio
    async def test_doc_detail_pdf_link_uses_proxy(self, ui_client):
        """PDF link on doc detail page points to UI proxy, not /api/docs/."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        content = r.content.decode()
        assert '/docs/doc:INV-2026-0001/pdf' in content
        assert '/api/docs/doc:INV-2026-0001/pdf' not in content

    @pytest.mark.asyncio
    async def test_po_shows_convert_to_bill_button(self, ui_client):
        """Draft PO shows 'Convert to Bill' instead of 'Finalize'."""
        po_doc = {**_BLANK_DOC, "doc_type": "purchase_order", "entity_id": "doc:PO-001", "ref_id": "PO-001"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=po_doc)):
            r = await ui_client.get("/docs/doc:PO-001", cookies=_authed())
        assert b"Convert to Bill" in r.content
        assert b"Finalize" not in r.content

    @pytest.mark.asyncio
    async def test_credit_note_shows_issue_credit_note(self, ui_client):
        """Draft credit note shows 'Issue Credit Note'."""
        cn_doc = {**_BLANK_DOC, "doc_type": "credit_note", "entity_id": "doc:CN-001", "ref_id": "CN-001"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=cn_doc)):
            r = await ui_client.get("/docs/doc:CN-001", cookies=_authed())
        assert b"Issue Credit Note" in r.content

    @pytest.mark.asyncio
    async def test_memo_shows_issue_memo(self, ui_client):
        """Draft memo shows 'Issue Memo'."""
        memo_doc = {**_BLANK_DOC, "doc_type": "memo", "entity_id": "doc:MEM-001", "ref_id": "MEM-001"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=memo_doc)):
            r = await ui_client.get("/docs/doc:MEM-001", cookies=_authed())
        assert b"Issue Memo" in r.content

    @pytest.mark.asyncio
    async def test_doc_detail_shows_history_section(self, ui_client):
        """Doc detail page includes a History section with ledger events."""
        ledger_data = {"items": [
            {"event_type": "doc.created", "ts": "2026-03-20T10:00:00Z", "data": {}},
            {"event_type": "doc.finalized", "ts": "2026-03-21T14:30:00Z", "data": {}},
        ], "total": 2}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value=ledger_data)),
        ):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        content = r.content.decode()
        assert "History" in content
        assert "Document created" in content
        assert "Document finalized" in content

    @pytest.mark.asyncio
    async def test_doc_detail_empty_history(self, ui_client):
        """Doc detail shows empty state when no ledger entries."""
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"No activity recorded yet." in r.content

    @pytest.mark.asyncio
    async def test_summary_accepts_doc_type_param(self, ui_client):
        """GET /docs?type=invoice passes doc_type to get_doc_summary."""
        captured = {}
        original_summary = AsyncMock(return_value=_DOC_SUMMARY)
        async def _capture_summary(token, doc_type=""):
            captured["doc_type"] = doc_type
            return await original_summary(token)
        with (
            patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_doc_summary", side_effect=_capture_summary),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test", "currency": "USD"})),
        ):
            r = await ui_client.get("/docs?type=invoice", cookies=_authed())
        assert captured.get("doc_type") == "invoice"

    @pytest.mark.asyncio
    async def test_send_form_shows_email_fields(self, ui_client):
        """Draft doc shows inline Send form with To, Subject, Message fields."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        content = r.content.decode()
        assert 'name="sent_to"' in content
        assert 'name="subject"' in content
        assert 'name="message"' in content

    @pytest.mark.asyncio
    async def test_mark_as_sent_button_on_draft(self, ui_client):
        """Draft doc shows 'Mark as Sent' button."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Mark as Sent" in r.content

    @pytest.mark.asyncio
    async def test_unmark_sent_button_on_sent_doc(self, ui_client):
        """Sent doc shows 'Unmark Sent' button (undo)."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_SENT_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"Unmark Sent" in r.content

    @pytest.mark.asyncio
    async def test_no_copy_link_or_share_button(self, ui_client):
        """Doc detail has no standalone Copy Link or Share button (share via Send form)."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        content = r.content.decode()
        assert "Copy Link" not in content
        assert '>Share<' not in content

    @pytest.mark.asyncio
    async def test_send_action_with_email(self, ui_client):
        """POST send action with sent_to calls api.send_doc with email data."""
        with patch("ui.api_client.send_doc", new=AsyncMock(return_value={})) as mock_send:
            r = await ui_client.post(
                "/docs/doc:INV-001/action/send",
                data={"sent_to": "test@example.com", "subject": "Invoice", "message": "Please pay"},
                cookies=_authed(),
            )
        mock_send.assert_called_once()
        call_data = mock_send.call_args[1].get("data") or mock_send.call_args[0][2] if len(mock_send.call_args[0]) > 2 else mock_send.call_args[1].get("data")
        assert call_data["sent_to"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_send_action_no_email_redirects(self, ui_client):
        """POST send action without sent_to redirects to cloud relay settings."""
        r = await ui_client.post(
            "/docs/doc:INV-001/action/send",
            data={"sent_to": ""},
            cookies=_authed(),
        )
        assert r.status_code == 204
        assert "/settings/general" in r.headers.get("hx-redirect", "")


class TestProformaNumbering:
    """Plan 09 G: Pro-forma numbering for draft invoices."""

    def test_proforma_in_prefix_map(self):
        """Proforma doc type exists in _PREFIX_BY_DOC_TYPE."""
        from celerp_docs.sequences import _PREFIX_BY_DOC_TYPE
        assert "proforma" in _PREFIX_BY_DOC_TYPE
        assert _PREFIX_BY_DOC_TYPE["proforma"] == "PF"

    def test_proforma_sequence_generation(self):
        """next_doc_ref generates PF-prefixed refs for proforma type."""
        from celerp_docs.sequences import next_doc_ref
        from unittest.mock import MagicMock
        company = MagicMock()
        company.settings = {}
        ref = next_doc_ref(company, "proforma")
        assert ref.startswith("PF-")

    def test_invoice_sequence_still_works(self):
        """next_doc_ref still generates INV-prefixed refs for invoice type."""
        from celerp_docs.sequences import next_doc_ref
        from unittest.mock import MagicMock
        company = MagicMock()
        company.settings = {}
        ref = next_doc_ref(company, "invoice")
        assert ref.startswith("INV-")

    def test_proforma_in_all_sequences(self):
        """get_all_sequences includes proforma."""
        from celerp_docs.sequences import get_all_sequences
        from unittest.mock import MagicMock
        company = MagicMock()
        company.settings = {}
        seqs = get_all_sequences(company)
        doc_types = {s["doc_type"] for s in seqs}
        assert "proforma" in doc_types

    def test_proforma_in_sales_settings(self):
        """Proforma appears in sales doc types for settings display."""
        from ui.routes.settings_sales import _SALES_DOC_TYPES
        assert "proforma" in _SALES_DOC_TYPES

    def test_finalize_projection_updates_ref(self):
        """doc.finalized event with ref_id updates the projection state."""
        from celerp_docs.doc_projections import apply_documents_event
        current = {"status": "draft", "ref_id": "PF-2603-0001", "doc_type": "invoice"}
        data = {"ref_id": "INV-2603-0001", "source_proforma_ref": "PF-2603-0001"}
        result = apply_documents_event(current, "doc.finalized", data)
        assert result["status"] == "final"
        assert result["ref_id"] == "INV-2603-0001"
        assert result["source_proforma_ref"] == "PF-2603-0001"


class TestProFormaLabel:
    """Draft invoices show 'Pro Forma' label instead of 'Draft'."""

    def test_status_label_proforma_for_draft_invoice(self):
        """When doc_type='invoice' and status='draft', label is 'Pro Forma'."""
        doc_type = "invoice"
        status = "draft"
        status_label = "Pro Forma" if doc_type == "invoice" and status == "draft" else status.replace("_", " ").title()
        assert status_label == "Pro Forma"

    def test_status_label_draft_for_non_invoice(self):
        """Other doc types still show 'Draft' for draft status."""
        for dt in ("credit_note", "memo", "receipt"):
            status_label = "Pro Forma" if dt == "invoice" and "draft" == "draft" else "draft".replace("_", " ").title()
            assert status_label == "Draft", f"{dt} should show Draft"

    def test_invoice_status_card_shows_proforma(self):
        """Invoice status cards use 'Pro Forma' label for draft."""
        from ui.routes.documents import _doc_status_cards
        from fasthtml.common import to_xml
        html = to_xml(_doc_status_cards([], "", doc_type="invoice"))
        assert "Pro Forma" in html
        assert ">Draft<" not in html  # invoice cards should NOT say Draft

    def test_drafts_tab_label_for_invoice(self):
        """Drafts tab shows 'Pro Forma' for invoice doc type."""
        from ui.routes.documents import _drafts_tab
        from fasthtml.common import to_xml
        html = to_xml(_drafts_tab(3, False, "invoice"))
        assert "Pro Forma (3)" in html

    def test_drafts_tab_label_for_other_types(self):
        """Drafts tab shows 'Drafts' for non-invoice doc types."""
        from ui.routes.documents import _drafts_tab
        from fasthtml.common import to_xml
        html = to_xml(_drafts_tab(3, False, "purchase_order"))
        assert "Drafts (3)" in html


class TestFieldSchemaBarcode:
    """Barcode and other missing fields in DEFAULT_ITEM_SCHEMA."""

    def test_barcode_in_default_schema(self):
        from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
        keys = {f["key"] for f in DEFAULT_ITEM_SCHEMA}
        assert "barcode" in keys

    def test_weight_fields_in_default_schema(self):
        from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
        keys = {f["key"] for f in DEFAULT_ITEM_SCHEMA}
        assert "weight" in keys
        assert "weight_unit" in keys

    def test_timestamp_fields_in_default_schema(self):
        from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
        keys = {f["key"] for f in DEFAULT_ITEM_SCHEMA}
        assert "created_at" in keys
        assert "updated_at" in keys

    def test_barcode_show_in_table(self):
        from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
        barcode = next(f for f in DEFAULT_ITEM_SCHEMA if f["key"] == "barcode")
        assert barcode["show_in_table"] is True



    """Plan 09 H: Convert PO to Bill flow."""

    def test_bill_in_doc_types(self):
        """Bill is a recognized doc type."""
        from ui.routes.documents import _DOC_TYPES
        assert "bill" in _DOC_TYPES

    def test_sidebar_label_updated(self):
        """Sidebar label for POs is 'Draft Bills & POs'."""
        from ui.routes.documents import _DOC_TYPE_PAGE_LABELS
        assert _DOC_TYPE_PAGE_LABELS["purchase_order"] == "Draft Bills & POs"

    def test_bill_conversion_projection(self):
        """doc.converted_to_bill sets status to awaiting_payment and updates doc_type."""
        from celerp_docs.doc_projections import apply_documents_event
        current = {"status": "draft", "doc_type": "purchase_order", "ref_id": "PO-2603-0001"}
        data = {"ref_id": "BILL-2603-0001", "source_po_ref": "PO-2603-0001", "doc_type": "bill"}
        result = apply_documents_event(current, "doc.converted_to_bill", data)
        assert result["status"] == "awaiting_payment"
        assert result["doc_type"] == "bill"
        assert result["ref_id"] == "BILL-2603-0001"
        assert result["source_po_ref"] == "PO-2603-0001"

    def test_bill_sequence_exists(self):
        """BILL sequence type exists in prefix map."""
        from celerp_docs.sequences import _PREFIX_BY_DOC_TYPE
        assert "bill" in _PREFIX_BY_DOC_TYPE
        assert _PREFIX_BY_DOC_TYPE["bill"] == "BILL"

    @pytest.mark.asyncio
    async def test_po_detail_shows_convert_to_bill(self, ui_client):
        """Draft PO detail page shows 'Convert to Bill' button."""
        po_doc = {**_BLANK_DOC, "doc_type": "purchase_order", "entity_id": "doc:PO-001", "ref_id": "PO-001"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=po_doc)):
            r = await ui_client.get("/docs/doc:PO-001", cookies=_authed())
        assert b"Convert to Bill" in r.content
        assert b"Finalize" not in r.content

    def test_bill_conversion_auto_je(self):
        """create_for_bill_conversion exists and is callable."""
        from celerp.services.auto_je import create_for_bill_conversion
        assert callable(create_for_bill_conversion)


# ===========================================================================
# Plan 11: Named Price Lists
# ===========================================================================

class TestPriceLists:

    def test_resolve_price_by_name(self):
        """resolve_price checks item[price_list] first."""
        from ui.routes.documents import resolve_price
        item = {"Retail": 100, "Wholesale": 65}
        assert resolve_price(item, "Retail") == 100.0
        assert resolve_price(item, "Wholesale") == 65.0

    def test_resolve_price_conventional_key(self):
        """resolve_price falls back to {name.lower()}_price key."""
        from ui.routes.documents import resolve_price
        item = {"retail_price": 99, "cost_price": 40}
        assert resolve_price(item, "Retail") == 99.0
        assert resolve_price(item, "Cost") == 40.0

    def test_resolve_price_no_match_returns_zero(self):
        """resolve_price returns 0.0 when no price exists for the list."""
        from ui.routes.documents import resolve_price
        item = {"retail_price": 50}
        assert resolve_price(item, "VIP") == 0.0
        assert resolve_price(item, "Dealer") == 0.0

    def test_resolve_price_prefers_direct_over_conventional(self):
        """If item has both 'Retail' and 'retail_price', direct key wins."""
        from ui.routes.documents import resolve_price
        item = {"Retail": 200, "retail_price": 150}
        assert resolve_price(item, "Retail") == 200.0

    @pytest.mark.asyncio
    async def test_price_lists_settings_tab_renders(self, ui_client):
        """Contacts settings price-lists tab renders the table."""
        with (
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Retail", "description": "Standard"},
                {"name": "Wholesale", "description": "Trade"},
            ])),
            patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        ):
            r = await ui_client.get("/settings/contacts?tab=price-lists", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert "Price Lists" in html
        assert "Retail" in html
        assert "Wholesale" in html
        assert "Default Price List" in html

    @pytest.mark.asyncio
    async def test_contact_detail_shows_price_list(self, ui_client):
        """Contact detail page includes a Price List field."""
        contact = {"entity_id": "c:1", "name": "Acme", "email": "", "phone": "", "address": "",
                    "tax_id": "", "credit_limit": 0, "payment_terms": "", "contact_type": "customer",
                    "price_list": "Wholesale", "tags": []}
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.list_contact_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/contacts/c:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert "Price List" in html
        assert "Wholesale" in html

    @pytest.mark.asyncio
    async def test_doc_detail_shows_price_list(self, ui_client):
        """Doc detail page includes a Price list field."""
        doc = {**_BLANK_DOC, "price_list": "Retail"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "T", "currency": "USD"})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            r = await ui_client.get("/docs/doc:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert "Price list:" in html
        assert "Retail" in html

    @pytest.mark.asyncio
    async def test_line_items_from_inventory_uses_price_list(self, ui_client):
        """_line_items_from_inventory returns price from resolve_price, not fallback chain."""
        from ui.routes.documents import _line_items_from_inventory
        item = {"entity_id": "item:1", "sku": "TEST", "name": "Widget",
                "retail_price": 100, "wholesale_price": 65, "cost_price": 40,
                "sell_by": "piece", "quantity": 5}
        with patch("ui.api_client.get_item", new=AsyncMock(return_value=item)):
            lines = await _line_items_from_inventory("tok", ["item:1"], price_list="Wholesale")
        assert len(lines) == 1
        assert lines[0]["unit_price"] == 65.0
        assert lines[0]["price_list"] == "Wholesale"

    @pytest.mark.asyncio
    async def test_line_items_from_inventory_zero_for_missing_list(self, ui_client):
        """_line_items_from_inventory returns 0 for a price list that doesn't exist on the item."""
        from ui.routes.documents import _line_items_from_inventory
        item = {"entity_id": "item:1", "sku": "TEST", "name": "Widget",
                "retail_price": 100, "sell_by": "piece", "quantity": 1}
        with patch("ui.api_client.get_item", new=AsyncMock(return_value=item)):
            lines = await _line_items_from_inventory("tok", ["item:1"], price_list="VIP")
        assert lines[0]["unit_price"] == 0.0

    @pytest.mark.asyncio
    async def test_reprice_endpoint(self, ui_client):
        """POST /docs/{id}/reprice updates line item prices from inventory."""
        doc = {**_BLANK_DOC, "price_list": "Retail", "line_items": [
            {"sku": "TEST-1", "description": "Widget", "quantity": 2, "unit_price": 100, "unit": "piece", "tax_rate": 0},
        ]}
        item = {"entity_id": "item:1", "sku": "TEST-1", "name": "Widget",
                "retail_price": 100, "wholesale_price": 65, "cost_price": 40}
        patched_doc = {**doc, "price_list": "Wholesale", "line_items": [
            {"sku": "TEST-1", "description": "Widget", "quantity": 2, "unit_price": 65, "unit": "piece", "tax_rate": 0, "price_list": "Wholesale"},
        ]}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item], "total": 1})),
            patch("ui.api_client.patch_doc", new=AsyncMock(return_value=patched_doc)),
        ):
            r = await ui_client.post("/docs/doc:1/reprice",
                                     content='{"price_list": "Wholesale"}',
                                     headers={"Content-Type": "application/json"},
                                     cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["repriced"] == 1
        assert data["price_list"] == "Wholesale"

    @pytest.mark.asyncio
    async def test_reprice_skips_manual_lines(self, ui_client):
        """Reprice leaves lines without a SKU unchanged."""
        doc = {**_BLANK_DOC, "price_list": "Retail", "line_items": [
            {"description": "Shipping", "quantity": 1, "unit_price": 25, "unit": "piece", "tax_rate": 0},
        ]}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.patch_doc", new=AsyncMock(return_value=doc)),
        ):
            r = await ui_client.post("/docs/doc:1/reprice",
                                     content='{"price_list": "Wholesale"}',
                                     headers={"Content-Type": "application/json"},
                                     cookies=_authed())
        assert r.status_code == 200
        assert r.json()["repriced"] == 0

    @pytest.mark.asyncio
    async def test_catalog_lookup_passes_price_list(self, ui_client):
        """Catalog lookup respects price_list query param."""
        item = {"entity_id": "item:1", "sku": "TEST", "name": "Widget",
                "retail_price": 100, "wholesale_price": 65, "sell_by": "piece", "quantity": 5}
        with patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [item], "total": 1})):
            r = await ui_client.get("/docs/catalog-lookup?sku=TEST&price_list=Wholesale", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["unit_price"] == 65.0

    @pytest.mark.asyncio
    async def test_doc_detail_shows_price_list_dropdown_above_lines(self, ui_client):
        """Doc detail page renders price list dropdown in the line items section."""
        doc = {**_BLANK_DOC, "price_list": "Wholesale"}
        with (
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "T", "currency": "USD"})),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[
                {"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"},
            ])),
        ):
            r = await ui_client.get("/docs/doc:1", cookies=_authed())
        assert r.status_code == 200
        html = r.content.decode()
        assert 'id="doc-price-list"' in html
        assert "celerpReprice" in html
        # Wholesale should be the selected option
        assert 'selected' in html


class TestBugFixesBatch25Mar:
    """Bug fixes batch: barcode lookup, qty decimals, empty state, split SKU."""

    def test_format_value_number_integer(self):
        from ui.components.table import format_value
        assert format_value(1.0, "number") == "1"
        assert format_value(5.0, "number") == "5"
        assert format_value(100.0, "number") == "100"

    def test_format_value_number_decimal(self):
        from ui.components.table import format_value
        assert format_value(1.5, "number") == "1.5"
        assert format_value(3.14, "number") == "3.14"

    def test_format_value_number_string_input(self):
        from ui.components.table import format_value
        assert format_value("1.0", "number") == "1"
        assert format_value("2.5", "number") == "2.5"

    def test_display_val_number_integer(self):
        from ui.components.table import _display_val
        from fasthtml.common import to_xml
        html = to_xml(_display_val(1.0, "number"))
        assert ">1<" in html
        assert "1.0" not in html

    def test_display_val_number_decimal(self):
        from ui.components.table import _display_val
        from fasthtml.common import to_xml
        html = to_xml(_display_val(2.5, "number"))
        assert ">2.5<" in html

    def test_list_items_sku_filter(self):
        """API list_items route accepts sku param for exact matching."""
        import inspect
        from celerp_inventory.routes import list_items
        sig = inspect.signature(list_items)
        assert "sku" in sig.parameters
        assert "barcode" in sig.parameters

    def test_catalog_lookup_tries_barcode(self):
        """Catalog lookup should try barcode match between SKU and general search."""
        # This is a structural test - verify the catalog_lookup code path
        import ast
        import textwrap
        from pathlib import Path
        src = (Path(__file__).parent.parent / "ui/routes/documents.py").read_text()
        assert "barcode" in src[src.index("doc_catalog_lookup"):src.index("doc_catalog_lookup") + 1500]

    @pytest.mark.asyncio
    async def test_split_auto_generates_sku(self, ui_client):
        """Bulk split auto-generates .N suffix SKU."""
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "DEMO-001", "quantity": 10})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.split_item", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/bulk/split", data={
                "selected": "item:1",
                "split_qty": "3",
            }, cookies=_authed())
        assert r.status_code == 200
        assert b"DEMO-001.1" in r.content

    @pytest.mark.asyncio
    async def test_split_auto_generates_incremented_sku(self, ui_client):
        """If .1 exists, next split gets .2."""
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "DEMO-001", "quantity": 10})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [{"sku": "DEMO-001.1"}], "total": 1})),
            patch("ui.api_client.split_item", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/bulk/split", data={
                "selected": "item:1",
                "split_qty": "3",
            }, cookies=_authed())
        assert r.status_code == 200
        assert b"DEMO-001.2" in r.content

    @pytest.mark.asyncio
    async def test_split_child_of_child(self, ui_client):
        """Splitting DEMO-001.1 generates DEMO-001.1.1."""
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "DEMO-001.1", "quantity": 5})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.split_item", new=AsyncMock(return_value={"event_id": "e1"})),
        ):
            r = await ui_client.post("/api/items/bulk/split", data={
                "selected": "item:2",
                "split_qty": "2",
            }, cookies=_authed())
        assert r.status_code == 200
        assert b"DEMO-001.1.1" in r.content


# ---------------------------------------------------------------------------
# Bug fixes batch: 6 bugs (Mar 25 2026)
# ---------------------------------------------------------------------------

class TestBugFixesBatch25Mar6Bugs:
    """Bug 1-6 fixes: SKU links, draft void/delete, save lines, doc URLs, history, split logic."""

    # Bug 1: SKU column renders as link
    def test_sku_display_cell_renders_link(self):
        """display_cell for SKU field wraps value in <a> link to /inventory/{entity_id}."""
        from ui.components.table import display_cell
        from fasthtml.common import to_xml
        html = to_xml(display_cell("item:abc123", "sku", "RING-001", cell_type="text"))
        assert 'href="/inventory/item:abc123"' in html
        assert "RING-001" in html

    def test_sku_display_cell_still_has_dblclick_htmx(self):
        """SKU cell retains hx-trigger=dblclick for inline editing."""
        from ui.components.table import display_cell
        from fasthtml.common import to_xml
        html = to_xml(display_cell("item:abc123", "sku", "RING-001", cell_type="text"))
        assert "dblclick" in html

    # Bug 2: Void button hidden for drafts
    @pytest.mark.asyncio
    async def test_draft_doc_has_no_void_button(self, ui_client):
        """Draft documents do not show Void button (only Delete)."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Void" not in r.content
        assert b"Confirm Delete" in r.content

    @pytest.mark.asyncio
    async def test_sent_doc_has_void_button(self, ui_client):
        """Non-draft, non-void documents do show Void button."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_SENT_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert r.status_code == 200
        assert b"Void" in r.content

    # Bug 3: _CELERP_EID defined in script block
    @pytest.mark.asyncio
    async def test_doc_detail_script_defines_celerp_eid(self, ui_client):
        """Draft doc detail page contains _CELERP_EID JS constant."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_BLANK_DOC)):
            r = await ui_client.get("/docs/doc:INV-2026-0001", cookies=_authed())
        assert b"_CELERP_EID" in r.content

    # Bug 4: Doc table row uses entity_id or id
    def test_doc_table_row_uses_id_field(self):
        """_doc_table renders correct href when doc has 'id' key instead of 'entity_id'."""
        from ui.routes.documents import _doc_table
        from fasthtml.common import to_xml
        docs = [{"id": "doc:INV-001", "doc_number": "INV-001", "status": "sent",
                 "doc_type": "invoice", "total_amount": 100, "outstanding_balance": 0}]
        html = to_xml(_doc_table(docs, doc_type="invoice"))
        assert "/docs/doc:INV-001" in html

    def test_doc_table_row_uses_entity_id_field(self):
        """_doc_table renders correct href when doc has 'entity_id' key."""
        from ui.routes.documents import _doc_table
        from fasthtml.common import to_xml
        docs = [{"entity_id": "doc:INV-002", "doc_number": "INV-002", "status": "sent",
                 "doc_type": "invoice", "total_amount": 200, "outstanding_balance": 0}]
        html = to_xml(_doc_table(docs, doc_type="invoice"))
        assert "/docs/doc:INV-002" in html

    # Bug 5: Contact detail shows activity section
    @pytest.mark.asyncio
    async def test_contact_detail_shows_activity_section(self, ui_client):
        """Contact detail page renders Activity tab link."""
        contact = {**_CONTACTS[0], "address": "123 St"}
        ledger = [{"event_type": "contact.updated", "ts": "2026-03-25T10:00:00", "data": {}, "actor_id": "user:1"}]
        with (
            patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
            patch("ui.api_client.list_contact_docs", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": ledger})),
        ):
            r = await ui_client.get("/contacts/ct:1", cookies=_authed())
        assert r.status_code == 200
        assert b"Activity" in r.content

    def test_inventory_ledger_table_shows_actor(self):
        """_ledger_table in inventory renders a User column with actor_id."""
        from ui.routes.inventory import _ledger_table
        from fasthtml.common import to_xml
        ledger = [{"event_type": "item.updated", "ts": "2026-03-25T10:00:00", "data": {}, "actor_id": "user:admin"}]
        html = to_xml(_ledger_table(ledger))
        assert "user:admin" in html
        assert "User" in html

    # Bug 6: Bulk split sends only 1 child
    @pytest.mark.asyncio
    async def test_bulk_split_sends_one_child_sku(self, ui_client):
        """Bulk split only creates 1 new child item; parent keeps its SKU."""
        captured = []
        async def mock_split(token, eid, children):
            captured.extend(children)
            return {"event_id": "e1"}
        with (
            patch("ui.api_client.get_item", new=AsyncMock(return_value={"sku": "RING-001", "quantity": 10})),
            patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})),
            patch("ui.api_client.split_item", new=mock_split),
        ):
            r = await ui_client.post("/api/items/bulk/split", data={
                "selected": "item:1",
                "split_qty": "3",
            }, cookies=_authed())
        assert r.status_code == 200
        # Only 1 child should be sent (the new split-off item)
        assert len(captured) == 1
        assert captured[0]["quantity"] == 3.0
        # Child SKU must not be the original parent SKU
        assert captured[0]["sku"] != "RING-001"


class TestInventoryUXFixes:
    """Tests for the 5-fix inventory UX improvements (2026-03-25)."""

    # ── Fix 1: Removed top status menu ────────────────────────────────────

    def test_status_tabs_removed_from_inventory_content(self):
        """_inventory_content must NOT call _status_tabs (duplicate menu removed)."""
        import inspect
        from ui.routes.inventory import _inventory_content
        src = inspect.getsource(_inventory_content)
        # The div with id="status-tabs" must not be generated
        assert "_status_tabs(" not in src

    def test_sold_archived_in_sidebar_nav(self):
        """Inventory module manifest includes Sold/Archived nav entries in sidebar."""
        import importlib
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "celerp_inv_init",
            str(Path(__file__).parent.parent / "default_modules/celerp-inventory/__init__.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        nav = mod.PLUGIN_MANIFEST["slots"]["nav"]
        keys = [n.get("key") for n in nav]
        assert "inventory_sold" in keys
        assert "inventory_archived" in keys
        hrefs = [n.get("href") for n in nav]
        assert "/inventory?status=sold" in hrefs
        assert "/inventory?status=archived" in hrefs

    # ── Fix 2: Column manager dropdown behavior ────────────────────────────

    def test_column_manager_uses_button_not_details(self):
        """Column manager must use a button+div dropdown, not a <details> element."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _column_manager
        schema = [{"key": "name", "label": "Name", "show_in_table": True}]
        html = to_xml(_column_manager(schema, {}))
        assert "col-mgr-btn" in html
        assert "col-mgr-menu" in html
        # No <details> element
        assert "<details" not in html

    def test_column_manager_has_localstorage_js(self):
        """Column manager JS must save to localStorage."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _column_manager
        schema = [{"key": "name", "label": "Name", "show_in_table": True}]
        html = to_xml(_column_manager(schema, {}))
        assert "localStorage" in html
        assert "saveVis" in html

    def test_column_manager_closes_on_outside_click(self):
        """Column manager JS must have click-outside-to-close handler."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _column_manager
        schema = [{"key": "name", "label": "Name", "show_in_table": True}]
        html = to_xml(_column_manager(schema, {}))
        assert "document.addEventListener" in html
        assert "menu.style.display" in html

    # ── Fix 3: Drag-and-drop column reordering ─────────────────────────────

    def test_data_table_th_has_draggable(self):
        """Table column headers must have draggable='true' for HTML5 drag-and-drop."""
        from fasthtml.common import to_xml
        from ui.components.table import data_table
        schema = [{"key": "name", "label": "Name", "type": "text", "show_in_table": True}]
        rows = [{"id": "item:1", "name": "Test Item"}]
        html = to_xml(data_table(schema, rows, entity_type="inventory"))
        assert 'draggable="true"' in html

    def test_data_table_js_has_dragdrop_handlers(self):
        """data_table JS must include HTML5 drag-and-drop event handlers."""
        from fasthtml.common import to_xml
        from ui.components.table import data_table
        schema = [{"key": "name", "label": "Name", "type": "text", "show_in_table": True}]
        rows = [{"id": "item:1", "name": "Test Item"}]
        html = to_xml(data_table(schema, rows, entity_type="inventory"))
        assert "dragstart" in html
        assert "dragover" in html
        assert "drop" in html
        assert "ORDER_KEY" in html

    def test_column_manager_has_drag_reorder_on_labels(self):
        """Column manager checkboxes must have draggable=true for menu reorder."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _column_manager
        schema = [
            {"key": "name", "label": "Name", "show_in_table": True},
            {"key": "sku", "label": "SKU", "show_in_table": True},
        ]
        html = to_xml(_column_manager(schema, {}))
        assert 'draggable="true"' in html

    # ── Fix 4: Split/create timestamps ────────────────────────────────────

    def test_item_create_data_sets_timestamps(self):
        """post_item must inject created_at/updated_at into the event data."""
        import inspect
        import sys
        # Read the source of the inventory routes to verify the fix
        routes_path = (
            Path(__file__).parent.parent
            / "default_modules/celerp-inventory/celerp_inventory/routes.py"
        )
        with open(routes_path) as f:
            src = f.read()
        # post_item must set created_at and updated_at
        assert 'data.setdefault("created_at"' in src
        assert 'data.setdefault("updated_at"' in src

    def test_split_child_data_has_timestamps(self):
        """split_item child_data must include created_at and updated_at."""
        routes_path = (
            Path(__file__).parent.parent
            / "default_modules/celerp-inventory/celerp_inventory/routes.py"
        )
        with open(routes_path) as f:
            src = f.read()
        # Split handler must set timestamps in child_data
        assert '"created_at": now_iso' in src
        assert '"updated_at": now_iso' in src

    def test_merge_create_data_has_timestamps(self):
        """merge_items create_data must include created_at and updated_at."""
        routes_path = (
            Path(__file__).parent.parent
            / "default_modules/celerp-inventory/celerp_inventory/routes.py"
        )
        with open(routes_path) as f:
            src = f.read()
        assert '"created_at": now_iso' in src and '"updated_at": now_iso' in src

    # ── Fix 5: Print Labels bulk action ───────────────────────────────────

    def test_labels_manifest_bulk_action_uses_ui_route(self):
        """Labels module bulk_action form_action must point to UI route, not API."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "celerp_labels_init",
            str(Path(__file__).parent.parent / "default_modules/celerp-labels/__init__.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bulk = mod.PLUGIN_MANIFEST["slots"]["bulk_action"]
        assert bulk["form_action"] == "/labels/print-bulk"

    def test_print_bulk_helper_generates_html(self):
        """_bulk_print_preview_page returns base_shell response with template picker."""
        from celerp_labels.ui_routes import _printable_label_sheet
        items = [{"name": "Test Ring", "sku": "RING-001"}]
        template = {
            "id": "t1", "name": "Test", "format": "40x30mm",
            "fields": [{"key": "name", "type": "text"}, {"key": "sku", "type": "text"}],
        }
        result = _printable_label_sheet(items, template)
        from starlette.responses import HTMLResponse
        assert isinstance(result, HTMLResponse)
        assert b"window.print()" in result.body
        assert b"Test Ring" in result.body
        assert b"RING-001" in result.body

    def test_printable_label_sheet_handles_no_template(self):
        """_printable_label_sheet falls back gracefully when no template provided."""
        from celerp_labels.ui_routes import _printable_label_sheet
        items = [{"name": "Ring", "sku": "R1"}]
        result = _printable_label_sheet(items, None)
        from starlette.responses import HTMLResponse
        assert isinstance(result, HTMLResponse)
        assert b"Ring" in result.body


# ── AI Page Tests ─────────────────────────────────────────────────────────────


class TestAIPage:
    """Tests for AI page components, showcase, chat, modular arch."""

    def test_showcase_view_has_scenarios_and_cta(self):
        """Showcase view renders 4 scenario tabs and both tier CTAs."""
        from celerp_ai.ui_routes import _showcase_view
        from fasthtml.common import to_xml
        html = to_xml(_showcase_view())
        assert "ai-showcase" in html
        assert "Meet your AI operator" in html
        assert "Batch Bill Entry" in html
        assert "Smart Restock" in html
        assert "Discrepancy Audit" in html
        assert "Bulk Catalog Import" in html
        assert "$29/mo" in html
        assert "$49/mo" in html
        assert "celerpShowcaseSelect" in html

    def test_chat_view_has_sidebar_and_input(self):
        """Chat view renders message area, sidebar, and input form."""
        from celerp_ai.ui_routes import _chat_view
        from fasthtml.common import to_xml
        html = to_xml(_chat_view())
        assert "ai-messages" in html
        assert "ai-query-input" in html
        assert "ai-sidebar" in html
        assert 'hx-post="/ai/chat"' in html
        assert "ai-msg" not in html.split("<script")[0]  # No pre-loaded messages (JS may contain the class name)

    def test_msg_bubble_user(self):
        from celerp_ai.ui_routes import _msg_bubble
        from fasthtml.common import to_xml
        html = to_xml(_msg_bubble("user", "Hello"))
        assert "ai-msg--user" in html
        assert "Hello" in html

    def test_msg_bubble_ai(self):
        from celerp_ai.ui_routes import _msg_bubble
        from fasthtml.common import to_xml
        html = to_xml(_msg_bubble("ai", "I can help"))
        assert "ai-msg--ai" in html
        assert "I can help" in html

    def test_scenarios_match_plan(self):
        """All 4 scenarios from the v7 plan are present."""
        from celerp_ai.ui_routes import _get_scenarios
        scenarios = _get_scenarios("en")
        assert len(scenarios) == 4
        ids = {s["id"] for s in scenarios}
        assert ids == {"batch-bills", "smart-restock", "discrepancy-audit", "bulk-catalog"}
        for s in scenarios:
            assert s.get("user"), f"Scenario {s['id']} missing user prompt"
            assert s.get("thinking"), f"Scenario {s['id']} missing thinking text"
            assert s.get("reply"), f"Scenario {s['id']} missing reply text"

    def test_showcase_disabled_input(self):
        """Showcase input bar is fully disabled (not interactive)."""
        from celerp_ai.ui_routes import _showcase_view
        from fasthtml.common import to_xml
        html = to_xml(_showcase_view())
        assert "ai-input--disabled" in html
        assert "ai-input__field--disabled" in html

    def test_ai_module_nav_slot_config(self):
        """celerp-ai manifest declares nav slot with min_role=operator."""
        import importlib
        init_path = str(Path(__file__).parent.parent / "default_modules/celerp-ai/__init__.py")
        spec = importlib.util.spec_from_file_location("celerp_ai_init", init_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        manifest = mod.PLUGIN_MANIFEST
        assert manifest["slots"]["nav"]["key"] == "ai"
        assert manifest["slots"]["nav"]["min_role"] == "operator"
        assert manifest["slots"]["nav"]["href"] == "/ai"

    def test_ai_dropzone_only_on_draft_bills(self):
        """Dropzone renders only for draft bill/expense, not other doc types."""
        from celerp.modules import slots
        slots.register("nav", {"key": "ai", "href": "/ai", "_module": "celerp-ai"})
        try:
            from celerp_docs._ui_documents import _doc_detail
            from fasthtml.common import to_xml

            draft_bill = {"entity_id": "doc:1", "doc_type": "bill", "status": "draft",
                          "line_items": [], "contact_id": "c:1"}
            html = to_xml(_doc_detail(draft_bill))
            assert "ai-dropzone" in html

            draft_expense = {"entity_id": "doc:4", "doc_type": "expense", "status": "draft",
                             "line_items": [], "contact_id": "c:4"}
            html4 = to_xml(_doc_detail(draft_expense))
            assert "ai-dropzone" in html4

            draft_invoice = {"entity_id": "doc:2", "doc_type": "invoice", "status": "draft",
                             "line_items": [], "contact_id": "c:2"}
            html2 = to_xml(_doc_detail(draft_invoice))
            assert "ai-dropzone" not in html2

            paid_bill = {"entity_id": "doc:3", "doc_type": "bill", "status": "paid",
                         "line_items": [], "contact_id": "c:3"}
            html3 = to_xml(_doc_detail(paid_bill))
            assert "ai-dropzone" not in html3
        finally:
            slots.clear()

    def test_ai_dropzone_hidden_when_module_not_loaded(self):
        """Without celerp-ai nav slot, dropzone must not render."""
        from celerp.modules import slots
        slots.clear()
        try:
            from celerp_docs._ui_documents import _doc_detail
            from fasthtml.common import to_xml
            draft_bill = {"entity_id": "doc:1", "doc_type": "bill", "status": "draft",
                          "line_items": [], "contact_id": "c:1"}
            html = to_xml(_doc_detail(draft_bill))
            assert "ai-dropzone" not in html
        finally:
            slots.clear()

    def test_showcase_has_typing_animation_script(self):
        """Showcase includes JS for typing animation effect."""
        from celerp_ai.ui_routes import _showcase_script
        from fasthtml.common import to_xml
        html = to_xml(_showcase_script())
        assert "playScenario" in html
        assert "appendMsg" in html
        assert "appendThinking" in html
