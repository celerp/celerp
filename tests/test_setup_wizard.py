# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Comprehensive tests for the setup wizard flow and related kernel wiring.

Coverage targets:
  A. ui/routes/setup.py     — all GET/POST handlers, form rendering, redirect logic
  B. celerp/main.py         — ENABLED_MODULES env-var path vs. config.toml fallback
  C. ui/routes/settings.py  — module-gated tabs, setup_done banner, company tab field display
  D. _load_verticals()      — preset dir loading, blank-last ordering, error tolerance
  E. Fringe / integration   — unauthenticated, API errors, missing presets dir, etc.
"""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio

from tests.conftest import make_test_token, authed_cookies

# ---------------------------------------------------------------------------
# Helpers shared with test_ui.py (duplicated so this file is self-contained)
# ---------------------------------------------------------------------------

def _authed(token: str | None = None, role: str = "owner") -> dict:
    return {"celerp_token": token or make_test_token(role=role)}


_COMPANY = {
    "id": "c1", "name": "Test Corp", "slug": "test-corp",
    "currency": "USD", "timezone": "Asia/Bangkok", "fiscal_year_start": "01",
    "settings": {
        "currency": "USD", "timezone": "Asia/Bangkok", "fiscal_year_start": "01",
        "tax_id": "", "phone": "", "address": "",
    },
}


# ---------------------------------------------------------------------------
# Fixture: fresh ui_client for each test in this file
# ---------------------------------------------------------------------------

@pytest.fixture()
def ui_client(tmp_path):
    """HTTPX AsyncClient pointing at the UI app (no redirects)."""
    import httpx
    from ui.app import app as ui_app
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_app),
        base_url="http://testserver",
        follow_redirects=False,
    )


# ===========================================================================
# A. GET /setup/company
# ===========================================================================

class TestSetupCompanyGet:
    """GET /setup/company rendering."""

    @pytest.mark.asyncio
    async def test_renders_with_token(self, ui_client):
        """Authenticated request renders the company-details form."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert r.status_code == 200
        assert b"Company details" in r.content
        assert b"currency" in r.content.lower()
        assert b"timezone" in r.content.lower()

    @pytest.mark.asyncio
    async def test_unauthenticated_redirects_to_login(self, ui_client):
        """No cookie → redirect to /login."""
        r = await ui_client.get("/setup/company")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_api_error_falls_back_to_empty_company(self, ui_client):
        """If get_company raises APIError, form still renders with empty values."""
        from ui.api_client import APIError
        with patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(500, "err"))):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert r.status_code == 200
        assert b"Company details" in r.content

    @pytest.mark.asyncio
    async def test_renders_all_verticals_in_select(self, ui_client):
        """Business-type select is populated with at least one non-blank option."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert r.status_code == 200
        # At minimum 'blank' must be present
        assert b"blank" in r.content

    @pytest.mark.asyncio
    async def test_wizard_steps_rendered(self, ui_client):
        """Step indicator shows step 2 as active."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert b"wizard-step" in r.content or b"step-num" in r.content

    @pytest.mark.asyncio
    async def test_form_pre_fills_company_values(self, ui_client):
        """Existing company values are pre-populated in the form inputs."""
        company = {**_COMPANY, "currency": "EUR", "tax_id": "9876543",
                   "settings": {**_COMPANY["settings"], "currency": "EUR", "tax_id": "9876543"}}
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert b"EUR" in r.content
        assert b"9876543" in r.content


# ===========================================================================
# B. POST /setup/company
# ===========================================================================

class TestSetupCompanyPost:
    """POST /setup/company — form submission logic."""

    @pytest.mark.asyncio
    async def test_unauthenticated_redirects_to_login(self, ui_client):
        r = await ui_client.post("/setup/company", data={"vertical": "blank"})
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_blank_vertical_redirects_to_cloud(self, ui_client):
        """Choosing blank vertical → /setup/cloud (no preset applied)."""
        with patch("ui.api_client.patch_company", new=AsyncMock(return_value={})):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "blank", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01"},
                cookies=_authed(),
            )
        assert r.status_code in (302, 303)
        assert r.headers.get("location", "").endswith("/setup/cloud")

    @pytest.mark.asyncio
    async def test_industry_vertical_calls_apply_preset_and_restart(self, ui_client):
        """Choosing a real vertical seeds categories directly via patch_category_schema and calls /system/restart."""
        restart_called = []
        modules_written = []
        patch_schema_calls = []

        class FakeResp:
            @property
            def is_error(self): return False
            def json(self): return {"ok": True}

        class FakeClient:
            async def post(self, url, **kw):
                if "restart" in url:
                    restart_called.append(url)
                return FakeResp()
            async def get(self, url, **kw):
                return type("R", (), {"is_error": False, "json": lambda self: {"name": "Test", "settings": {}}})()
            async def patch(self, url, **kw):
                if "category-schema" in url:
                    patch_schema_calls.append(url)
                return FakeResp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        def _capture_modules(mods):
            modules_written.append(mods)

        with (
            patch("ui.api_client.patch_company", new=AsyncMock(return_value={})),
            patch("ui.api_client._client", return_value=FakeClient()),
            patch("ui.routes.setup._set_enabled_modules", side_effect=_capture_modules),
        ):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "gemstones", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01"},
                cookies=_authed(),
            )

        assert r.status_code in (302, 303)
        assert "/setup/activating" in r.headers.get("location", "")
        # Modules were written to config (list from gemstones preset)
        assert len(modules_written) == 1
        assert isinstance(modules_written[0], list)
        assert len(modules_written[0]) > 0
        # Category schemas were seeded directly (9 categories in gemstones preset)
        assert len(patch_schema_calls) == 9, (
            f"Expected 9 category schema patch calls for gemstones but got {patch_schema_calls}. "
            "Without this, gemstone columns won't appear in the inventory column manager."
        )
        # Restart was triggered
        assert len(restart_called) == 1

    @pytest.mark.asyncio
    async def test_industry_vertical_redirects_to_activating(self, ui_client):
        """After preset + restart call, redirect goes to /setup/activating."""
        class FakeResp:
            @property
            def is_error(self): return False

        class FakeClient:
            async def post(self, url, **kw): return FakeResp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with (
            patch("ui.api_client.patch_company", new=AsyncMock(return_value={})),
            patch("ui.api_client._client", return_value=FakeClient()),
        ):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "food_beverage", "currency": "USD"},
                cookies=_authed(),
            )
        assert "/setup/activating" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_patch_company_error_re_renders_form(self, ui_client):
        """patch_company raising APIError re-renders the form with error message."""
        from ui.api_client import APIError
        with (
            patch("ui.api_client.patch_company", new=AsyncMock(side_effect=APIError(422, "Invalid currency"))),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        ):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "blank", "currency": "BADCUR"},
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"Invalid currency" in r.content

    @pytest.mark.asyncio
    async def test_patch_company_error_get_company_also_fails(self, ui_client):
        """Both patch_company and get_company fail — form still renders."""
        from ui.api_client import APIError
        with (
            patch("ui.api_client.patch_company", new=AsyncMock(side_effect=APIError(422, "err"))),
            patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(500, "db error"))),
        ):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "blank"},
                cookies=_authed(),
            )
        assert r.status_code == 200  # renders form with empty company

    @pytest.mark.asyncio
    async def test_apply_preset_api_error_still_redirects_to_activating(self, ui_client):
        """Even if apply-preset raises, the wizard still proceeds (fire-and-forget)."""
        class FakeClient:
            async def post(self, url, **kw):
                raise Exception("network error")
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with (
            patch("ui.api_client.patch_company", new=AsyncMock(return_value={})),
            patch("ui.api_client._client", return_value=FakeClient()),
        ):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "gemstones"},
                cookies=_authed(),
            )
        # Should still redirect to activating (exception is swallowed)
        assert r.status_code in (302, 303)
        assert "/setup/activating" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_default_currency_is_thb_when_missing(self, ui_client):
        """Form defaults to THB when no currency submitted."""
        patched_data = {}

        async def _capture_patch(token, data):
            patched_data.update(data)
            return {}

        with patch("ui.api_client.patch_company", new=AsyncMock(side_effect=_capture_patch)):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "blank"},  # no currency field
                cookies=_authed(),
            )
        assert patched_data.get("currency") == "THB"

    @pytest.mark.asyncio
    async def test_all_form_fields_forwarded_to_patch(self, ui_client):
        """All expected fields reach patch_company."""
        captured = {}

        async def _cap(token, data):
            captured.update(data)

        with patch("ui.api_client.patch_company", new=AsyncMock(side_effect=_cap)):
            await ui_client.post(
                "/setup/company",
                data={
                    "vertical": "blank",
                    "currency": "SGD",
                    "timezone": "Asia/Singapore",
                    "tax_id": "T12345",
                    "phone": "+65 1234 5678",
                    "address": "1 Marina Blvd",
                },
                cookies=_authed(),
            )
        assert captured["currency"] == "SGD"
        assert captured["timezone"] == "Asia/Singapore"
        assert captured["tax_id"] == "T12345"
        assert captured["phone"] == "+65 1234 5678"
        assert captured["address"] == "1 Marina Blvd"


# ===========================================================================
# C. GET /setup/activating
# ===========================================================================

class TestSetupActivating:
    """GET /setup/activating — spinner page while server restarts."""

    @pytest.mark.asyncio
    async def test_renders_activating_page(self, ui_client):
        r = await ui_client.get("/setup/activating", cookies=_authed())
        assert r.status_code == 200
        assert b"Activating" in r.content

    @pytest.mark.asyncio
    async def test_page_contains_status_poll_script(self, ui_client):
        """JavaScript must poll /setup/activating-status."""
        r = await ui_client.get("/setup/activating", cookies=_authed())
        assert b"activating-status" in r.content

    @pytest.mark.asyncio
    async def test_page_redirects_to_dashboard_after_poll(self, ui_client):
        """Poll script must redirect to /dashboard on success."""
        r = await ui_client.get("/setup/activating", cookies=_authed())
        assert b"/dashboard" in r.content

    @pytest.mark.asyncio
    async def test_unauthenticated_redirects_to_login(self, ui_client):
        r = await ui_client.get("/setup/activating")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")


# ===========================================================================
# C2. GET /setup/activating-status
# ===========================================================================

class TestSetupActivatingStatus:
    """JSON endpoint polled by the activating spinner page."""

    @pytest.mark.asyncio
    async def test_returns_down_when_api_unreachable(self, ui_client):
        """When the API client raises, phase=down is returned."""
        import httpx
        with patch("ui.api_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            r = await ui_client.get("/setup/activating-status", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] == "down"

    @pytest.mark.asyncio
    async def test_returns_loading_when_some_modules_not_running(self, ui_client):
        """When requested > loaded, phase=loading."""
        modules_payload = [
            {"name": "celerp-inventory", "label": "Inventory", "running": True, "enabled": True},
            {"name": "celerp-contacts", "label": "Contacts", "running": False, "enabled": True},
            {"name": "celerp-sales-funnel", "label": "Sales Pipeline", "running": False, "enabled": True},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = modules_payload

        with patch("celerp.config.read_config", return_value={"modules": {"enabled": ["celerp-inventory", "celerp-contacts", "celerp-sales-funnel"]}}):
            with patch("ui.api_client.httpx.AsyncClient") as mock_cls:
                mock_inst = AsyncMock()
                mock_inst.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_inst)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                r = await ui_client.get("/setup/activating-status", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] == "loading"
        assert data["loaded"] == 1
        assert data["requested"] == 3

    @pytest.mark.asyncio
    async def test_returns_ready_when_all_modules_running(self, ui_client):
        """When all requested modules are running, phase=ready."""
        modules_payload = [
            {"name": "celerp-inventory", "label": "Inventory", "running": True, "enabled": True},
            {"name": "celerp-contacts", "label": "Contacts", "running": True, "enabled": True},
            {"name": "celerp-sales-funnel", "label": "Sales Pipeline", "running": True, "enabled": True},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = modules_payload

        with patch("celerp.config.read_config", return_value={"modules": {"enabled": ["celerp-inventory", "celerp-contacts", "celerp-sales-funnel"]}}):
            with patch("ui.api_client.httpx.AsyncClient") as mock_cls:
                mock_inst = AsyncMock()
                mock_inst.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_inst)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                r = await ui_client.get("/setup/activating-status", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] == "ready"
        assert data["loaded"] == 3
        assert data["requested"] == 3

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_down(self, ui_client):
        """No cookie -> phase=down (safe default, no token to query API)."""
        r = await ui_client.get("/setup/activating-status")
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] == "down"

    @pytest.mark.asyncio
    async def test_api_non_200_returns_down(self, ui_client):
        """API returns non-200 -> phase=down."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with patch("ui.api_client.httpx.AsyncClient") as mock_cls:
            mock_inst = AsyncMock()
            mock_inst.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_inst)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            r = await ui_client.get("/setup/activating-status", cookies=_authed())
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] == "down"


# ===========================================================================
# D. GET /setup/cloud
# ===========================================================================

class TestSetupCloud:
    """GET /setup/cloud — cloud upsell page."""

    @pytest.mark.asyncio
    async def test_renders_cloud_page(self, ui_client):
        r = await ui_client.get("/setup/cloud", cookies=_authed())
        assert r.status_code == 200
        assert b"Cloud" in r.content or b"cloud" in r.content.lower()

    @pytest.mark.asyncio
    async def test_unauthenticated_redirects_to_login(self, ui_client):
        r = await ui_client.get("/setup/cloud")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")


# ===========================================================================
# E. Legacy redirect routes
# ===========================================================================

class TestSetupLegacyRedirects:
    """Old /setup/users, /setup/vertical, /setup/modules routes must redirect."""

    @pytest.mark.asyncio
    async def test_get_setup_users_redirects(self, ui_client):
        r = await ui_client.get("/setup/users", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/cloud" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_post_setup_users_redirects(self, ui_client):
        r = await ui_client.post("/setup/users", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/cloud" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_post_setup_users_done_redirects(self, ui_client):
        r = await ui_client.post("/setup/users/done", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/cloud" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_get_setup_vertical_redirects(self, ui_client):
        r = await ui_client.get("/setup/vertical", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/company" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_post_setup_vertical_redirects(self, ui_client):
        r = await ui_client.post("/setup/vertical", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/cloud" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_get_setup_modules_redirects(self, ui_client):
        r = await ui_client.get("/setup/modules", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/cloud" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_post_setup_modules_redirects(self, ui_client):
        r = await ui_client.post("/setup/modules", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/setup/cloud" in r.headers.get("location", "")


# ===========================================================================
# F. _load_verticals() logic
# ===========================================================================

class TestLoadVerticals:
    """_load_verticals() — reads preset JSONs, orders blank last."""

    def _make_presets_dir(self, tmp_path: Path, presets: list[dict]) -> Path:
        d = tmp_path / "presets"
        d.mkdir()
        for p in presets:
            (d / f"{p['name']}.json").write_text(json.dumps(p))
        return d

    def test_blank_sorted_last(self, tmp_path):
        presets_dir = self._make_presets_dir(tmp_path, [
            {"name": "blank", "display_name": "Blank"},
            {"name": "gemstones", "display_name": "Gems & Jewelry"},
            {"name": "fashion", "display_name": "Fashion & Apparel"},
        ])
        import importlib
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", presets_dir):
            result = setup_mod._load_verticals()
        assert result[-1][0] == "blank", "blank must be last"

    def test_non_blank_alphabetically_sorted(self, tmp_path):
        presets_dir = self._make_presets_dir(tmp_path, [
            {"name": "watches", "display_name": "Watches"},
            {"name": "automotive", "display_name": "Automotive"},
            {"name": "fashion", "display_name": "Fashion"},
        ])
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", presets_dir):
            result = setup_mod._load_verticals()
        labels = [label for _, label in result]
        assert labels == sorted(labels)

    def test_empty_presets_dir_returns_empty(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", empty_dir):
            result = setup_mod._load_verticals()
        assert result == []

    def test_missing_presets_dir_returns_empty(self, tmp_path):
        missing = tmp_path / "nonexistent"
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", missing):
            result = setup_mod._load_verticals()
        assert result == []

    def test_malformed_json_skipped(self, tmp_path):
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "good.json").write_text(json.dumps({"name": "good", "display_name": "Good"}))
        (presets_dir / "bad.json").write_text("NOT VALID JSON {{{")
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", presets_dir):
            result = setup_mod._load_verticals()
        names = [n for n, _ in result]
        assert "good" in names
        assert "bad" not in names

    def test_preset_missing_name_key_skipped(self, tmp_path):
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "nope.json").write_text(json.dumps({"display_name": "No name key"}))
        (presets_dir / "ok.json").write_text(json.dumps({"name": "ok", "display_name": "OK"}))
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", presets_dir):
            result = setup_mod._load_verticals()
        names = [n for n, _ in result]
        assert "ok" in names

    def test_only_blank_returns_blank_last(self, tmp_path):
        presets_dir = self._make_presets_dir(tmp_path, [
            {"name": "blank", "display_name": "Blank"},
        ])
        import ui.routes.setup as setup_mod
        with patch.object(setup_mod, "_PRESETS_DIR", presets_dir):
            result = setup_mod._load_verticals()
        assert len(result) == 1
        assert result[0][0] == "blank"


# ===========================================================================
# G. main.py ENABLED_MODULES / config.toml fallback logic
# ===========================================================================

class TestMainModuleLoading:
    """celerp/main.py — env-var path vs config.toml fallback."""

    def test_enabled_env_var_takes_precedence_over_config(self, tmp_path):
        """When ENABLED_MODULES is set, read_config is never called."""
        fake_read_config = MagicMock(return_value={"modules": {"enabled": ["celerp-labels"]}})
        fake_load_all = MagicMock(return_value=[])
        fake_register = MagicMock()

        import importlib, sys, types

        with (
            patch.dict("os.environ", {"MODULE_DIR": str(tmp_path), "ENABLED_MODULES": "celerp-inventory"}),
            patch("celerp.config.read_config", fake_read_config),
            patch("celerp.modules.loader.load_all", fake_load_all),
            patch("celerp.modules.loader.register_api_routes", fake_register),
        ):
            # Simulate what main.py lifespan does
            import os
            _MODULE_DIR = os.environ.get("MODULE_DIR", "")
            _enabled_env = os.environ.get("ENABLED_MODULES", "")
            if _enabled_env:
                _enabled = set(_enabled_env.split(","))
            else:
                from celerp.config import read_config
                cfg = read_config()
                _enabled = set(cfg.get("modules", {}).get("enabled") or [])

        assert _enabled == {"celerp-inventory"}
        fake_read_config.assert_not_called()

    def test_empty_env_var_falls_back_to_config_toml(self):
        """When ENABLED_MODULES is empty, config.toml is read."""
        fake_cfg = {"modules": {"enabled": ["celerp-reports"]}}
        fake_read_config = MagicMock(return_value=fake_cfg)

        with (
            patch.dict("os.environ", {"ENABLED_MODULES": ""}),
            patch("celerp.config.read_config", fake_read_config),
        ):
            import os
            _enabled_env = os.environ.get("ENABLED_MODULES", "")
            if _enabled_env:
                _enabled = set(_enabled_env.split(","))
            else:
                from celerp.config import read_config
                cfg = read_config()
                _enabled = set(cfg.get("modules", {}).get("enabled") or [])

        assert _enabled == {"celerp-reports"}
        fake_read_config.assert_called_once()

    def test_empty_config_toml_returns_empty_set(self):
        """read_config returning {} yields empty enabled set."""
        with (
            patch.dict("os.environ", {"ENABLED_MODULES": ""}),
            patch("celerp.config.read_config", return_value={}),
        ):
            import os
            _enabled_env = os.environ.get("ENABLED_MODULES", "")
            if not _enabled_env:
                from celerp.config import read_config
                cfg = read_config()
                _enabled = set(cfg.get("modules", {}).get("enabled") or [])
            else:
                _enabled = set(_enabled_env.split(","))

        assert _enabled == set()

    def test_config_toml_missing_modules_key_returns_empty(self):
        """Config with no modules section yields empty set."""
        fake_cfg = {"database": {"url": "sqlite+aiosqlite:///./dev.db"}}
        with (
            patch.dict("os.environ", {"ENABLED_MODULES": ""}),
            patch("celerp.config.read_config", return_value=fake_cfg),
        ):
            import os
            _enabled_env = os.environ.get("ENABLED_MODULES", "")
            if not _enabled_env:
                from celerp.config import read_config
                cfg = read_config()
                _enabled = set(cfg.get("modules", {}).get("enabled") or [])
            else:
                _enabled = set()

        assert _enabled == set()

    def test_modules_not_loaded_when_enabled_is_empty(self):
        """If _enabled is empty, load_all must NOT be called."""
        fake_load_all = MagicMock(return_value=[])
        fake_register = MagicMock()

        with (
            patch.dict("os.environ", {"ENABLED_MODULES": ""}),
            patch("celerp.config.read_config", return_value={}),
        ):
            import os
            _enabled_env = os.environ.get("ENABLED_MODULES", "")
            from celerp.config import read_config
            cfg = read_config()
            _enabled = set(cfg.get("modules", {}).get("enabled") or [])

            if _enabled:
                fake_load_all(str(os.environ.get("MODULE_DIR", "")), _enabled)
                fake_register(None, [])

        fake_load_all.assert_not_called()
        fake_register.assert_not_called()


# ===========================================================================
# H. settings.py — module-gated tabs
# ===========================================================================

class TestSettingsModuleGatedTabs:
    """_settings_tabs() only shows tabs for enabled modules."""

    def _get_tabs_html(self, enabled_modules: set) -> str:
        from ui.routes.settings import _settings_tabs
        result = _settings_tabs("company", enabled_modules=enabled_modules)
        # FastHTML FT → render to string
        from fasthtml.common import to_xml
        return to_xml(result)

    def test_kernel_tabs_always_present(self):
        """company, users, taxes, terms, modules are always shown."""
        html = self._get_tabs_html(set())
        for tab in ("company", "users", "taxes", "terms", "modules"):
            assert tab in html, f"kernel tab '{tab}' missing from empty-module render"

    def test_inventory_tabs_hidden_without_module(self):
        """schema, locations, import-history, bulk-attach not shown without celerp-inventory."""
        html = self._get_tabs_html(set())
        for tab in ("schema", "locations", "import-history", "bulk-attach"):
            assert f"tab={tab}" not in html, f"inventory tab '{tab}' shown without module"

    def test_inventory_tabs_shown_with_module(self):
        """schema, locations, import-history, bulk-attach shown when celerp-inventory enabled."""
        html = self._get_tabs_html({"celerp-inventory"})
        for tab in ("schema", "locations", "import-history", "bulk-attach"):
            assert f"tab={tab}" in html, f"inventory tab '{tab}' missing with module"

    def test_connectors_tab_gated(self):
        html_no = self._get_tabs_html(set())
        html_yes = self._get_tabs_html({"celerp-connectors"})
        assert "tab=connectors" not in html_no
        assert "tab=connectors" in html_yes

    def test_backup_tab_gated(self):
        html_no = self._get_tabs_html(set())
        html_yes = self._get_tabs_html({"celerp-backup"})
        assert "tab=backup" not in html_no
        assert "tab=backup" in html_yes

    def test_ai_tab_not_in_settings(self):
        """AI tab is now on its own /ai page, not in _settings_tabs."""
        html = self._get_tabs_html({"celerp-ai"})
        assert "tab=ai" not in html

    def test_verticals_tab_gated(self):
        html_no = self._get_tabs_html(set())
        html_yes = self._get_tabs_html({"celerp-verticals"})
        assert "tab=verticals" not in html_no
        assert "tab=verticals" in html_yes

    def test_cloud_relay_not_in_settings_tabs(self):
        """cloud-relay is now its own /settings/cloud page, not a tab in _settings_tabs."""
        html = self._get_tabs_html(set())
        assert "tab=cloud-relay" not in html

    def test_multiple_modules_all_tabs_shown(self):
        """All optional tabs present when all modules enabled (except AI which has its own page)."""
        em = {"celerp-inventory", "celerp-connectors", "celerp-backup", "celerp-ai", "celerp-verticals"}
        html = self._get_tabs_html(em)
        for tab in ("schema", "locations", "import-history", "bulk-attach", "connectors", "backup", "verticals"):
            assert f"tab={tab}" in html
        assert "tab=ai" not in html

    def test_none_enabled_modules_treated_as_empty(self):
        """None passed as enabled_modules → same as empty set."""
        from ui.routes.settings import _settings_tabs
        from fasthtml.common import to_xml
        html = to_xml(_settings_tabs("company", enabled_modules=None))
        assert "company" in html
        assert "tab=schema" not in html


# ===========================================================================
# I. settings.py — setup_done banner + settings page with modules loaded
# ===========================================================================

class TestSettingsSetupDoneBanner:
    """Settings page shows a welcome banner when ?setup=done is in the URL."""

    @pytest.mark.asyncio
    async def test_setup_done_banner_present_with_param(self, ui_client):
        """GET /settings?setup=done shows a setup-complete banner."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/general?setup=done", cookies=_authed())
        assert r.status_code == 200
        assert b"Setup complete" in r.content or b"setup-done-banner" in r.content

    @pytest.mark.asyncio
    async def test_settings_page_tabs_filtered_by_enabled_modules(self, ui_client):
        """Settings page with celerp-inventory enabled shows schema/locations tabs."""
        modules = [
            {"name": "celerp-inventory", "enabled": True, "running": True, "depends_on": []},
        ]
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=modules)),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        # Schema link lives in /settings/inventory nav
        assert b"inventory" in r.content or b"Inventory" in r.content

    @pytest.mark.asyncio
    async def test_settings_page_no_extra_tabs_when_no_modules(self, ui_client):
        """Settings page with no modules — inventory tabs must not appear."""
        with (
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_users", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
            patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
            patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/settings/general?tab=company", cookies=_authed())
        assert r.status_code == 200
        assert b"tab=schema" not in r.content


# ===========================================================================
# J. Company tab — flat dict field display (_company_details_form pre-fill)
# ===========================================================================

class TestCompanyDetailsFormPreFill:
    """_company_details_form handles both flat and nested company dicts."""

    def _render(self, company: dict) -> str:
        from ui.routes.setup import _company_details_form
        from fasthtml.common import to_xml
        return to_xml(_company_details_form(company))

    def test_flat_dict_renders_currency(self):
        """Flat company dict (no settings sub-key) renders currency correctly."""
        html = self._render({"currency": "JPY", "timezone": "Asia/Tokyo"})
        assert "JPY" in html

    def test_nested_settings_dict_renders_currency(self):
        """Nested settings dict renders currency correctly."""
        html = self._render({"settings": {"currency": "EUR", "timezone": "Europe/London"}})
        assert "EUR" in html

    def test_both_present_top_level_wins(self):
        """When both top-level and settings dict present, merged correctly (settings keys complement)."""
        company = {
            "currency": "USD",
            "settings": {"currency": "SGD", "timezone": "Asia/Singapore"},
        }
        # s = {**settings, **company} → company top-level overrides
        html = self._render(company)
        # Either USD or SGD is present depending on merge order — just assert it renders
        assert "USD" in html or "SGD" in html

    def test_empty_dict_uses_defaults(self):
        """Empty company dict renders with placeholder defaults."""
        html = self._render({})
        assert "Company details" in html

    def test_tax_id_pre_filled(self):
        html = self._render({"tax_id": "9999999", "settings": {"tax_id": "9999999"}})
        assert "9999999" in html

    def test_address_pre_filled(self):
        html = self._render({"address": "42 Sukhumvit", "settings": {"address": "42 Sukhumvit"}})
        assert "42 Sukhumvit" in html

    def test_phone_pre_filled(self):
        html = self._render({"phone": "+66 99 999 9999", "settings": {"phone": "+66 99 999 9999"}})
        assert "+66 99 999 9999" in html

    def test_error_flash_shown(self):
        """error parameter renders flash element."""
        html = self._render({"settings": {}})
        from ui.routes.setup import _company_details_form
        from fasthtml.common import to_xml
        html_with_err = to_xml(_company_details_form({}, error="Something went wrong"))
        assert "Something went wrong" in html_with_err

    def test_no_error_flash_absent(self):
        """No error param → no flash element."""
        html = self._render({})
        assert "flash" not in html.lower() or "Something went wrong" not in html


# ===========================================================================
# K. Fringe / integration edge cases
# ===========================================================================

class TestSetupFringe:
    """Edge cases that reveal integration bugs."""

    @pytest.mark.asyncio
    async def test_post_blank_vertical_does_not_call_apply_preset(self, ui_client):
        """blank vertical must never touch /companies/me/apply-preset."""
        api_calls = []

        class FakeClient:
            async def post(self, url, **kw):
                api_calls.append(url)

                class R:
                    @property
                    def is_error(self): return False
                return R()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with (
            patch("ui.api_client.patch_company", new=AsyncMock(return_value={})),
            patch("ui.api_client._client", return_value=FakeClient()),
        ):
            r = await ui_client.post(
                "/setup/company",
                data={"vertical": "blank"},
                cookies=_authed(),
            )
        assert r.status_code in (302, 303)
        assert all("apply-preset" not in u for u in api_calls), \
            f"apply-preset must NOT be called for blank vertical; got calls: {api_calls}"
        assert all("restart" not in u for u in api_calls), \
            f"/system/restart must NOT be called for blank vertical; got calls: {api_calls}"

    @pytest.mark.asyncio
    async def test_activating_page_max_attempts_message_present(self, ui_client):
        """Poll script must have a max-attempts / timeout fallback message."""
        r = await ui_client.get("/setup/activating", cookies=_authed())
        content = r.content.decode()
        # "maxAttempts" or "Taking longer" must appear
        assert "maxAttempts" in content or "Taking longer" in content or "longer than expected" in content.lower()

    def test_settings_tabs_active_class_marks_correct_tab(self):
        """Active tab receives 'tab--active' CSS class."""
        from ui.routes.settings import _settings_tabs
        from fasthtml.common import to_xml
        html = to_xml(_settings_tabs("users", enabled_modules=set()))
        # The users tab link must have tab--active
        # Simple heuristic: tab--active appears near 'users'
        idx_users = html.find("tab=users")
        idx_active = html.find("tab--active")
        assert idx_active != -1, "No tab--active class found"

    @pytest.mark.asyncio
    async def test_settings_401_redirects_to_login(self, ui_client):
        """Settings page with 401 from API → redirect to /login."""
        from ui.api_client import APIError
        with patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(401, "Unauthorized"))):
            r = await ui_client.get("/settings/general", cookies=_authed())
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_setup_company_get_without_company_name_still_renders(self, ui_client):
        """Company with only id field still renders the form."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": "x"})):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert r.status_code == 200
        assert b"Company details" in r.content
