# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Business type in the UI: the explicit choice on the setup company step, and the
later change from Company Details. Both go through api.set_business_type only."""
from __future__ import annotations

import json
import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from test_helpers import make_test_token
from ui.api_client import APIError


def _authed(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(role=role)}


@pytest.fixture()
def ui_client():
    from ui.app import app as ui_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                             base_url="http://testserver", follow_redirects=False)


def _vertical_select(html: str) -> str:
    """The searchable business-type control: its visible input, value input and options."""
    m = re.search(r'<div class="combobox-wrap">\s*<input[^>]*>\s*<input type="hidden" name="vertical"'
                  r'.*?combobox-option--empty.*?</div>', html, re.S)
    assert m, "no business-type control rendered"
    return m.group(0)


def _selected_values(select_html: str) -> list[str]:
    """What the control submits: the value of its single hidden input."""
    return re.findall(r'<input type="hidden" name="vertical"[^>]*value="([^"]*)"', select_html)


def _shows_only_placeholder(select_html: str) -> bool:
    """Nothing is chosen: the visible input is empty and shows the prompt."""
    from ui.i18n import t
    visible = re.search(r'<input[^>]*class="combobox-input[^>]*>', select_html).group(0)
    return 'value=""' in visible and f'placeholder="{t("setup.choose_business_type")}"' in visible


_FULL_FORM = {"vertical": "gemstones", "currency": "EUR", "timezone": "Europe/Paris"}


def _assert_form_kept(html: str, vertical: str = "gemstones", currency: str = "EUR") -> None:
    assert _selected_values(_vertical_select(html)) == [vertical]
    currency_input = re.search(r'<input[^>]*name="currency"[^>]*>', html).group(0)
    assert f'value="{currency}"' in currency_input


# -- setup: rendering -------------------------------------------------------------

class TestSetupRender:
    @pytest.mark.asyncio
    async def test_fresh_setup_selects_only_placeholder(self, ui_client):
        company = {"name": "Co", "settings": {}}
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
            r = await ui_client.get("/setup/company", cookies=_authed())
        select = _vertical_select(r.text)
        assert _selected_values(select) == [""]
        assert _shows_only_placeholder(select)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["general", "gemstones"])
    async def test_company_with_a_type_goes_to_the_dashboard(self, ui_client, stored):
        company = {"name": "Co", "vertical": stored, "settings": {"vertical": stored}}
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
            r = await ui_client.get("/setup/company", cookies=_authed())
        assert r.status_code == 302 and r.headers["location"] == "/dashboard"

    def test_hidden_presets_not_offered(self):
        from ui.routes.setup import business_type_options
        values = [v for v, _ in business_type_options()]
        assert "saas" not in values and "property_rental" not in values
        assert values[-1] == "blank"


# -- setup: submit ----------------------------------------------------------------

class TestSetupSubmit:
    def _mocks(self, stack: ExitStack, set_type=None, patch_company=None):
        mocks = {
            "patch_company": patch_company or AsyncMock(return_value={}),
            "set_business_type": set_type or AsyncMock(return_value={"restart_required": False}),
            "restart_system": AsyncMock(return_value={"ok": True}),
        }
        for name, mock in mocks.items():
            stack.enter_context(patch(f"ui.api_client.{name}", new=mock))
        return mocks

    @pytest.mark.asyncio
    @pytest.mark.parametrize("vertical, message", [
        ("", "Choose a business type to continue."),
        ("no_such_type", "Unknown business type"),
        ("saas", "Unknown business type"),
    ])
    async def test_missing_unknown_or_hidden_rejected_before_mutation(self, ui_client, vertical, message):
        with ExitStack() as stack:
            m = self._mocks(stack)
            r = await ui_client.post("/setup/company", data={**_FULL_FORM, "vertical": vertical},
                                     cookies=_authed())
        assert r.status_code == 200
        assert message in r.text
        m["patch_company"].assert_not_awaited()
        m["set_business_type"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_currency_rerender_keeps_business_type_and_clears_currency(self, ui_client):
        with ExitStack() as stack:
            m = self._mocks(stack)
            r = await ui_client.post("/setup/company", data={**_FULL_FORM, "currency": "XXX"},
                                     cookies=_authed())
        assert r.status_code == 200
        _assert_form_kept(r.text, currency="")  # an unknown code is never shown as chosen
        m["patch_company"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_failure_rerender_keeps_submitted_values(self, ui_client):
        stored = {"name": "Co", "currency": "USD", "settings": {"currency": "USD"}}
        with ExitStack() as stack:
            m = self._mocks(stack, patch_company=AsyncMock(side_effect=APIError(500, "save failed")))
            stack.enter_context(patch("ui.api_client.get_company", new=AsyncMock(return_value=stored)))
            r = await ui_client.post("/setup/company", data=_FULL_FORM, cookies=_authed())
        assert r.status_code == 200
        assert "save failed" in r.text
        _assert_form_kept(r.text)
        m["set_business_type"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_business_type_failure_does_not_advance(self, ui_client):
        with ExitStack() as stack:
            m = self._mocks(stack, set_type=AsyncMock(side_effect=APIError(500, "type failed")))
            r = await ui_client.post("/setup/company", data=_FULL_FORM, cookies=_authed())
        assert r.status_code == 200
        assert "type failed" in r.text
        _assert_form_kept(r.text)
        m["restart_system"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_business_type_operation_not_category_or_module_primitives(self, ui_client):
        import ui.routes.setup as setup_mod
        for gone in ("_set_enabled_modules", "_seed_vertical_categories", "_PRESETS_DIR", "_CATEGORIES_DIR"):
            assert not hasattr(setup_mod, gone), gone
        with ExitStack() as stack:
            m = self._mocks(stack, set_type=AsyncMock(return_value={"restart_required": True}))
            schema = stack.enter_context(patch("ui.api_client.patch_category_schema", new=AsyncMock()))
            r = await ui_client.post("/setup/company", data=_FULL_FORM, cookies=_authed())
        assert r.headers["location"].endswith("/setup/activating")
        assert m["set_business_type"].await_args.args[1] == "gemstones"
        assert "vertical" not in m["patch_company"].await_args.args[1]
        m["restart_system"].assert_awaited_once()
        schema.assert_not_awaited()


# -- Company Details --------------------------------------------------------------

_SELF = {"id": "contact:self", "entity_id": "contact:self", "name": "My Co",
         "contact_type": "both", "is_self": True, "addresses": []}


def _details_patches(stack: ExitStack, role: str, vertical: str | None = "gemstones") -> None:
    company = {"name": "My Co", "currency": "USD", "current_role": role, "vertical": vertical,
               "settings": {"self_contact_id": "contact:self", "vertical": vertical}}
    stack.enter_context(patch("ui.api_client.get_company", new=AsyncMock(return_value=company)))
    for name, val in (("get_contact", _SELF), ("list_contacts", {"items": [_SELF]}),
                      ("list_contact_docs", {"items": []}), ("list_items", {"items": []}),
                      ("list_docs", {"items": []})):
        stack.enter_context(patch(f"ui.api_client.{name}", new=AsyncMock(return_value=val)))


class TestCompanyDetails:
    @pytest.mark.asyncio
    async def test_owner_sees_business_type(self, ui_client):
        with ExitStack() as stack:
            _details_patches(stack, "owner")
            r = await ui_client.get("/finance/company-details", cookies=_authed("owner"))
        assert r.status_code == 200, r.text
        assert "/settings/company/vertical/edit" in r.text
        assert "Gems &amp; Jewelry" in r.text or "Gems & Jewelry" in r.text

    @pytest.mark.asyncio
    async def test_admin_without_lifecycle_permission_does_not_see_it(self, ui_client):
        with ExitStack() as stack:
            _details_patches(stack, "admin")
            r = await ui_client.get("/finance/company-details", cookies=_authed("admin"))
        assert r.status_code == 200, r.text
        assert "/settings/company/vertical/edit" not in r.text

    @pytest.mark.asyncio
    async def test_edit_is_searchable_with_esc_cancel(self, ui_client):
        with ExitStack() as stack:
            _details_patches(stack, "owner")
            r = await ui_client.get("/settings/company/vertical/edit", cookies=_authed())
        assert "combobox-input" in r.text
        assert 'data-value="agricultural"' in r.text and 'data-value="saas"' not in r.text
        assert "/settings/company/vertical/display" in r.text

    @pytest.mark.asyncio
    async def test_save_calls_business_type_and_confirms(self, ui_client):
        set_type = AsyncMock(return_value={"vertical": "fashion", "restart_required": False})
        patch_company = AsyncMock()
        with patch("ui.api_client.set_business_type", new=set_type), \
             patch("ui.api_client.patch_company", new=patch_company):
            r = await ui_client.patch("/settings/company/vertical", data={"value": "fashion"}, cookies=_authed())
        assert r.status_code == 200
        assert set_type.await_args.args[1] == "fashion"
        patch_company.assert_not_awaited()
        assert "Fashion" in r.text
        assert "Business type saved." in r.headers["HX-Trigger"]
        assert 'href="/modules"' not in r.text

    @pytest.mark.asyncio
    async def test_restart_needed_points_to_modules(self, ui_client):
        set_type = AsyncMock(return_value={"vertical": "fashion", "restart_required": True})
        with patch("ui.api_client.set_business_type", new=set_type):
            r = await ui_client.patch("/settings/company/vertical", data={"value": "fashion"}, cookies=_authed())
        assert 'href="/modules"' in r.text
        assert '"persist": true' in r.headers["HX-Trigger"]

    @pytest.mark.asyncio
    async def test_unknown_value_rejected_without_call(self, ui_client):
        set_type = AsyncMock()
        with patch("ui.api_client.set_business_type", new=set_type):
            r = await ui_client.patch("/settings/company/vertical", data={"value": "saas"}, cookies=_authed())
        assert "Unknown business type" in r.text
        set_type.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_refusal_shown(self, ui_client):
        with patch("ui.api_client.set_business_type", new=AsyncMock(side_effect=APIError(403, "Forbidden"))):
            r = await ui_client.patch("/settings/company/vertical", data={"value": "fashion"}, cookies=_authed())
        assert "Forbidden" in r.text and "cell-error" in r.text


class TestSetupRestart:
    @pytest.mark.asyncio
    async def test_restart_request_dropping_still_goes_to_activating(self, ui_client):
        """The restart request can fail as the server goes down; setup still moves on
        to the page that waits for the modules."""
        with ExitStack() as stack:
            stack.enter_context(patch("ui.api_client.patch_company", new=AsyncMock(return_value={})))
            stack.enter_context(patch("ui.api_client.set_business_type",
                                      new=AsyncMock(return_value={"restart_required": True})))
            stack.enter_context(patch("ui.api_client.restart_system",
                                      new=AsyncMock(side_effect=httpx.RemoteProtocolError("server closed"))))
            r = await ui_client.post("/setup/company", data=_FULL_FORM, cookies=_authed())
        assert r.status_code == 302
        assert r.headers["location"].endswith("/setup/activating")


_CHANGES = {"categories_added": {"diamond": "Diamond", "ruby": "Ruby"}, "modules_enabled": ["Manufacturing"],
            "settings_updated": ["inventory_method"], "defaults_updated": ["payment_terms", "terms_conditions"],
            "demo_items_replaced": 9, "demo_items_kept": 1}


def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


class TestCompanyDetailsSummary:
    @pytest.mark.asyncio
    async def test_change_summary_is_shown_until_dismissed(self, ui_client):
        set_type = AsyncMock(return_value={"vertical": "fashion", "restart_required": False, "changes": _CHANGES})
        with patch("ui.api_client.set_business_type", new=set_type):
            r = await ui_client.patch("/settings/company/vertical", data={"value": "fashion"}, cookies=_authed())
        toast = _toast(r)
        assert toast["persist"] is True
        lines = toast["message"].split("\n")
        assert lines[0] == "Business type saved."
        assert "Categories added: Diamond, Ruby" in lines
        assert "Modules enabled: Manufacturing" in lines
        assert "Settings updated: Stock cutting method, Payment Terms, Terms & Conditions" in lines
        assert "Demo items replaced: 9" in lines
        assert "Demo items kept because they were edited or used: 1" in lines

    @pytest.mark.asyncio
    async def test_nothing_changed_is_a_plain_confirmation(self, ui_client):
        empty = {k: (type(v)() if isinstance(v, (list, dict)) else 0) for k, v in _CHANGES.items()}
        set_type = AsyncMock(return_value={"vertical": "fashion", "restart_required": False, "changes": empty})
        with patch("ui.api_client.set_business_type", new=set_type):
            r = await ui_client.patch("/settings/company/vertical", data={"value": "fashion"}, cookies=_authed())
        toast = _toast(r)
        assert toast["message"] == "Business type saved."
        assert "persist" not in toast

    def test_toast_keeps_line_breaks(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.css").read_text()
        assert re.search(r"\.toast__msg\s*\{[^}]*white-space:\s*pre-line", css)

    def test_summary_copy_in_every_locale(self):
        from pathlib import Path
        keys = ("settings.business_type_changes.categories", "settings.business_type_changes.modules",
                "settings.business_type_changes.settings", "settings.business_type_changes.demo_replaced",
                "settings.business_type_changes.demo_kept", "settings.purchasing_payment_terms")
        for path in sorted((Path(__file__).resolve().parents[1] / "ui" / "locales").glob("*.json")):
            data = json.loads(path.read_text())
            for key in keys:
                assert data.get(key), f"{path.name}: {key}"


class TestStoredTypeDisplay:
    @pytest.mark.asyncio
    async def test_hidden_stored_type_shows_its_label(self, ui_client):
        with ExitStack() as stack:
            _details_patches(stack, "owner", vertical="saas")
            r = await ui_client.get("/finance/company-details", cookies=_authed("owner"))
        assert "SaaS / Software" in r.text

    @pytest.mark.asyncio
    async def test_unknown_stored_type_shows_empty_marker(self, ui_client):
        from ui.routes.settings import _company_display_cell
        from fasthtml.common import to_xml
        html = to_xml(_company_display_cell("vertical", "general"))
        assert "general" not in html
        assert ">--<" in html

    @pytest.mark.asyncio
    async def test_editor_opens_with_the_hidden_type_visible(self, ui_client):
        with ExitStack() as stack:
            _details_patches(stack, "owner", vertical="saas")
            r = await ui_client.get("/settings/company/vertical/edit", cookies=_authed())
        text_input = re.search(r'<input[^>]*combobox-input[^>]*>', r.text).group(0)
        assert 'value="SaaS / Software"' in text_input
