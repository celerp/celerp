# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""First run is one form: /setup asks for the workspace, business type and currency
in one screen, one POST applies all of it, and the user lands on the dashboard.

The API is stubbed at ui.api_client (and at the names ui.routes.auth imports
directly), the same way tests/test_setup_wizard.py does.
"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from test_helpers import make_test_token

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"

_VALID = {
    "company_name": "Acme Trading",
    "name": "Pat Owner",
    "email": "owner@example.com",
    "password": "correct-horse-1",
    "confirm_password": "correct-horse-1",
    "vertical": "blank",
    "currency": "EUR",
    "timezone": "America/Argentina/Buenos_Aires",
}

_SOURCES = [{"key": "manager_io", "display_name": "Manager.io", "artifacts": []}]


@pytest.fixture()
def ui_client():
    import httpx
    from ui.app import app as ui_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                             base_url="http://testserver", follow_redirects=False)


def _authed(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(role=role)}


class _Stubs:
    """The API seen by a first-run setup: not bootstrapped, no setup code."""

    def __init__(self, *, bootstrapped=False, code_required=False, sources=None,
                 business_type_result=None, business_type_error=None, company=None):
        self.register = AsyncMock(return_value=("access-tok", "refresh-tok"))
        self.patch_company = AsyncMock(return_value={})
        self.set_business_type = AsyncMock(
            side_effect=business_type_error,
            return_value=business_type_result or {"restart_required": False})
        self.restart_system = AsyncMock(return_value={})
        # The first person to set up Celerp is its installation owner.
        self.installation_owner = AsyncMock(return_value=True)
        self.get_company = AsyncMock(return_value=company or {"id": "c1", "settings": {}})
        self.migration_sources = AsyncMock(return_value=_SOURCES if sources is None else sources)
        if isinstance(sources, Exception):
            self.migration_sources = AsyncMock(side_effect=sources)
        self._bootstrapped = AsyncMock(return_value=bootstrapped)
        self._code = AsyncMock(return_value=code_required)
        self._stack = ExitStack()

    def __enter__(self):
        for target, mock in (
            ("ui.routes.auth.bootstrap_status", self._bootstrapped),
            ("ui.api_client.bootstrap_status", self._bootstrapped),
            ("ui.api_client.setup_code_required", self._code),
            ("ui.routes.auth.api_register", self.register),
            ("ui.api_client.patch_company", self.patch_company),
            ("ui.api_client.set_business_type", self.set_business_type),
            ("ui.api_client.restart_system", self.restart_system),
            ("ui.api_client.installation_owner", self.installation_owner),
            ("ui.api_client.get_company", self.get_company),
            ("ui.api_client.migration_sources", self.migration_sources),
        ):
            self._stack.enter_context(patch(target, new=mock))
        return self

    def __exit__(self, *exc):
        self._stack.close()


def _visible_input_names(html: str) -> set[str]:
    """Names of the inputs a user fills in: every named input, select or textarea
    that is not hidden. A searchable dropdown submits through a hidden input next
    to its visible text box, so its hidden name counts as visible."""
    names: set[str] = set()
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        m = re.search(r'\bname="([^"]+)"', tag)
        if not m:
            continue
        hidden = re.search(r'\btype="hidden"', tag)
        if hidden and "data-name=" not in tag:
            continue
        names.add(m.group(1))
    return names


def _form_html(html: str) -> str:
    m = re.search(r'<form[^>]*action="/setup"[^>]*>.*?</form>', html, re.S)
    assert m, "the workspace form is on the page"
    return m.group(0)


# ── A1: /setup is the workspace form ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_setup_get_renders_workspace_form_directly(ui_client):
    with _Stubs():
        r = await ui_client.get("/setup")
    assert r.status_code == 200
    html = r.text
    form = _form_html(html)
    assert 'name="company_name"' in form
    assert 'name="vertical"' in form
    assert 'name="currency"' in form
    assert "/setup/fresh" not in html
    assert "quick-link-card" not in html  # no chooser cards


@pytest.mark.asyncio
async def test_setup_form_inputs_are_exactly_the_budget(ui_client):
    budget = {"company_name", "name", "email", "password", "confirm_password", "vertical", "currency"}
    with _Stubs():
        r = await ui_client.get("/setup")
    assert _visible_input_names(_form_html(r.text)) == budget
    with _Stubs(code_required=True):
        r = await ui_client.get("/setup")
    assert _visible_input_names(_form_html(r.text)) == budget | {"setup_code"}


@pytest.mark.asyncio
async def test_setup_fresh_route_is_gone(ui_client):
    with _Stubs():
        r = await ui_client.get("/setup/fresh")
    assert r.status_code == 404


# ── A3: one submit does the whole setup ──────────────────────────────────────

@pytest.mark.asyncio
async def test_setup_post_applies_type_currency_timezone_and_lands_on_dashboard(ui_client):
    with _Stubs() as s:
        r = await ui_client.post("/setup", data=_VALID)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"
    s.register.assert_awaited_once()
    patched = {k: v for call in s.patch_company.await_args_list for k, v in call.args[1].items()}
    assert patched["currency"] == "EUR"
    assert patched["timezone"] == "America/Argentina/Buenos_Aires"
    s.set_business_type.assert_awaited_once_with("access-tok", "blank")
    assert "celerp_token=access-tok" in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_setup_post_needing_restart_goes_to_activating_then_dashboard(ui_client):
    with _Stubs(business_type_result={"restart_required": True}) as s:
        r = await ui_client.post("/setup", data=_VALID)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup/activating"
    s.restart_system.assert_awaited_once()
    page = await ui_client.get("/setup/activating", cookies=_authed())
    assert "window.location.href = '/dashboard'" in page.text
    assert "/onboarding" not in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", ["Mars/Olympus", ""])
async def test_setup_post_invalid_timezone_falls_back_to_default(ui_client, tz):
    with _Stubs() as s:
        r = await ui_client.post("/setup", data={**_VALID, "timezone": tz})
    assert r.headers["location"] == "/dashboard"
    patched = {k: v for call in s.patch_company.await_args_list for k, v in call.args[1].items()}
    assert patched["timezone"] == "UTC"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,needle", [
    ("vertical", "no-such-type", "Unknown business type"),
    ("currency", "XXX", "Invalid currency"),
])
async def test_setup_post_rejects_unknown_business_type_and_currency(ui_client, field, value, needle):
    with _Stubs() as s:
        r = await ui_client.post("/setup", data={**_VALID, field: value})
    assert r.status_code == 200
    assert needle in r.text
    s.register.assert_not_awaited()
    s.set_business_type.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_post_missing_business_type_keeps_typed_values(ui_client):
    with _Stubs() as s:
        r = await ui_client.post("/setup", data={**_VALID, "vertical": ""})
    assert r.status_code == 200
    html = r.text
    assert "Choose a business type to continue." in html
    assert 'value="Acme Trading"' in html
    assert 'value="Pat Owner"' in html
    assert 'value="owner@example.com"' in html
    assert 'value="EUR"' in html
    assert "correct-horse-1" not in html
    s.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_post_twice_is_harmless(ui_client):
    with _Stubs(bootstrapped=True) as s:
        signed_out = await ui_client.post("/setup", data=_VALID)
        signed_in = await ui_client.post("/setup", data=_VALID, cookies=_authed())
    assert signed_out.status_code == 302 and signed_out.headers["location"] == "/login"
    assert signed_in.status_code == 302 and signed_in.headers["location"] == "/dashboard"
    s.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_apply_failure_lands_on_retry_page(ui_client):
    from ui.api_client import APIError
    with _Stubs(business_type_error=APIError(500, "modules unavailable")) as s:
        r = await ui_client.post("/setup", data=_VALID)
        assert r.status_code == 302
        assert r.headers["location"].startswith("/setup/company")
        assert "celerp_token=access-tok" in r.headers.get("set-cookie", "")
        page = await ui_client.get(r.headers["location"], cookies={"celerp_token": "access-tok"})
    assert page.status_code == 200
    html = page.text
    assert "could not be finished" in html
    form = re.search(r'<form[^>]*action="/setup/company"[^>]*>.*?</form>', html, re.S).group(0)
    assert _visible_input_names(form) == {"vertical", "currency"}


@pytest.mark.asyncio
async def test_retry_page_resubmit_finishes_on_dashboard(ui_client):
    with _Stubs() as s:
        r = await ui_client.post("/setup/company", data={"vertical": "blank", "currency": "EUR",
                                                         "timezone": "Asia/Tokyo"},
                                 cookies=_authed())
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"
    s.set_business_type.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_page_with_type_already_set_redirects_and_applies_nothing(ui_client):
    company = {"id": "c1", "vertical": "blank", "settings": {"vertical": "blank"}}
    with _Stubs(company=company) as s:
        got = await ui_client.get("/setup/company", cookies=_authed())
        posted = await ui_client.post("/setup/company", data={"vertical": "blank", "currency": "EUR"},
                                      cookies=_authed())
    assert got.status_code == 302 and got.headers["location"] == "/dashboard"
    assert posted.status_code == 302 and posted.headers["location"] == "/dashboard"
    s.set_business_type.assert_not_awaited()
    s.patch_company.assert_not_awaited()


# ── A2: additional options ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setup_options_show_sources_from_api(ui_client):
    with _Stubs():
        r = await ui_client.get("/setup")
    assert "Currently supported: Manager.io" in r.text
    assert "More coming soon." in r.text
    with _Stubs(sources=_SOURCES + [{"key": "ledgerly", "display_name": "Ledgerly", "artifacts": []}]):
        r = await ui_client.get("/setup")
    assert "Currently supported: Manager.io, Ledgerly" in r.text


@pytest.mark.asyncio
async def test_setup_options_hide_supported_line_when_sources_fail(ui_client):
    from ui.api_client import APIError
    with _Stubs(sources=APIError(503, "down")):
        r = await ui_client.get("/setup")
    assert r.status_code == 200
    assert "Move your books from another system" in r.text
    assert "Currently supported" not in r.text


@pytest.mark.asyncio
async def test_setup_options_and_later_note_present(ui_client):
    with _Stubs():
        r = await ui_client.get("/setup")
    html = r.text
    assert "Additional options" in html
    assert 'href="/setup/restore-backup"' in html
    assert 'href="/setup/migrate"' in html
    assert "Restore a Celerp backup" in html
    assert "Not sure yet? Create your workspace now." in html
    assert "You can restore a backup or move your books in later from the dashboard." in html
    assert len(re.findall(r'class="[^"]*\bbtn--primary\b', html)) == 1
    assert "Try sample company" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/setup/restore-backup", "/setup/migrate"])
async def test_setup_option_pages_link_back_to_form(ui_client, path):
    with _Stubs():
        r = await ui_client.get(path)
    assert r.status_code == 200
    assert 'href="/setup"' in r.text


@pytest.mark.asyncio
async def test_setup_restore_page_offers_whole_installation_recovery(ui_client):
    """Restore has one entry point on the setup screen, so recovering a whole
    installation is offered from inside the restore page."""
    with _Stubs():
        r = await ui_client.get("/setup/restore-backup")
    assert 'href="/setup/import-backup"' in r.text


@pytest.mark.asyncio
async def test_setup_password_rule_visible_before_submit(ui_client):
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    with _Stubs():
        r = await ui_client.get("/setup")
    assert f"At least {MIN_PASSWORD_LENGTH} characters." in r.text


# ── INV-20: translated first screen ──────────────────────────────────────────

_NEW_SETUP_KEYS = (
    "setup.options_heading", "setup.option_restore_title", "setup.option_restore_desc",
    "setup.option_move_title", "setup.option_move_desc", "setup.sources_supported",
    "setup.more_coming_soon", "setup.not_sure_yet", "setup.later_from_dashboard",
    "setup.currency_hint", "setup.business_type_hint", "setup.password_hint",
)


@pytest.mark.asyncio
async def test_setup_page_in_german(ui_client):
    en = json.loads((_LOCALES / "en.json").read_text())
    de = json.loads((_LOCALES / "de.json").read_text())
    with _Stubs():
        r = await ui_client.get("/setup", headers={"Accept-Language": "de-DE,de;q=0.9"})
    html = r.text
    import html as _html
    text = _html.unescape(html)
    for key in _NEW_SETUP_KEYS + ("page.set_up_your_workspace", "btn.create_workspace",
                                  "setup.choose_business_type"):
        assert key in de, key
        en_value = en[key].split("{")[0].strip()
        de_value = de[key].split("{")[0].strip()
        assert de_value and de_value in text, key
        if en_value != de_value:
            assert en_value not in text, f"{key}: English {en_value!r} left on the German page"


# ── A4: the getting-started hub is gone ──────────────────────────────────────

@pytest.mark.asyncio
async def test_onboarding_routes_gone_and_login_never_redirects_there(ui_client):
    r = await ui_client.get("/onboarding", cookies=_authed())
    assert r.status_code == 404
    r = await ui_client.post("/onboarding/complete", cookies=_authed())
    assert r.status_code in (404, 405)
    for settings in ({}, {"onboarding_pending": True}, {"vertical": "blank"}):
        company = {"id": "c1", "settings": settings}
        with _Stubs(bootstrapped=True, company=company), \
                patch("ui.routes.auth.api_get_company", new=AsyncMock(return_value=company)):
            r = await ui_client.get("/", cookies=_authed())
        assert r.headers["location"] == "/dashboard", settings


def test_business_type_catch_all_label():
    from ui.routes.setup import business_type_options
    options = business_type_options()
    assert options[-1][0] == "blank"
    assert options[-1][1] == "Other / general"
