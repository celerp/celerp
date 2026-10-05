# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A page the caller's role may not open sends them to the dashboard, which says
why once; a silent bounce reads as a broken link, and a notice that repeats on a
reload or follows a crafted link reads as a refusal that never happened."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import REPO_ROOT, assert_not_permitted_redirect, make_test_token


def _text(key: str, lang: str) -> str:
    return json.loads((REPO_ROOT / "ui" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))[key]


def _error_boxes(html: str) -> list[str]:
    """The text of each red box in the page's main content."""
    main = html.split('id="main-content"', 1)[1]
    return re.findall(r'class="flash flash--error"[^>]*>([^<]*)<', main)


_MANAGER = {"id": "c1", "name": "B", "current_role": "manager", "settings": {}}
_DASHBOARD_OFF = {**_MANAGER, "settings": {"enabled_modules": ["celerp-contacts"]}}


async def _client(lang: str = "en") -> AsyncClient:
    from ui.app import app as ui_app
    c = AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False)
    c.cookies.set("celerp_token", make_test_token(role="manager"))
    c.cookies.set("celerp_lang", lang)
    return c


@pytest_asyncio.fixture
async def ui_client():
    async with await _client() as c:
        yield c


@contextmanager
def _dashboard(company=_MANAGER):
    load = AsyncMock(return_value=(company, {}, {}, {}, []))
    with patch("ui.routes.dashboard._load_dashboard", new=load), \
            patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}})), \
            patch("ui.api_client.get_activity", new=AsyncMock(return_value=[])):
        yield


@contextmanager
def _dashboard_turned_off():
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_DASHBOARD_OFF)), \
            patch("celerp.modules.loader.route_module",
                  lambda scope: "celerp-dashboard" if scope.get("path") == "/dashboard" else None):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/modules", "/settings/payments"])
async def test_a_page_the_role_may_not_open_sends_it_to_the_dashboard_with_a_reason(ui_client, path):
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_MANAGER)):
        r = await ui_client.get(path)
    assert_not_permitted_redirect(r)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/modules", "/settings/payments"])
async def test_an_htmx_request_the_role_may_not_make_navigates_to_the_dashboard(ui_client, path):
    """A refusal reached from inside a page (a tab, an edit, a toggle) navigates the
    whole page to the dashboard and its reason, rather than swapping the dashboard
    into the fragment that fired the request."""
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_MANAGER)):
        r = await ui_client.get(path, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/dashboard"
    assert "celerp_notice=not_permitted" in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "de"])
async def test_the_notice_shows_once_after_a_refusal_and_not_on_reload(lang):
    notice = _text("perm.redirected_no_access", lang)
    async with await _client(lang) as c:
        with _dashboard():
            refused = await c.get("/modules")
            landed = await c.get(refused.headers["location"])
            reloaded = await c.get(refused.headers["location"])
    assert landed.status_code == 200, landed.text[:300]
    assert landed.text.count(notice) == 1
    assert "celerp_notice" not in c.cookies
    assert notice not in reloaded.text


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "de"])
async def test_a_crafted_notice_link_shows_nothing(lang, monkeypatch):
    from test_inventory_content_degradation import _install_inventory_getters
    notice = _text("perm.redirected_no_access", lang)
    async with await _client(lang) as c:
        with _dashboard():
            dashboard = await c.get("/dashboard?notice=not_permitted")
        _install_inventory_getters(monkeypatch)
        inventory = await c.get("/inventory?notice=not_permitted")
    assert dashboard.status_code == 200 and inventory.status_code == 200
    assert notice not in dashboard.text
    assert notice not in inventory.text


@pytest.mark.asyncio
async def test_another_session_never_sees_the_notice():
    notice = _text("perm.redirected_no_access", "en")
    async with await _client() as refused_session, await _client() as other_session:
        with _dashboard():
            await refused_session.get("/modules")
            other = await other_session.get("/dashboard")
    assert notice not in other.text


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "de"])
async def test_with_the_dashboard_turned_off_the_refusal_shows_one_notice(lang):
    """The redirect lands on the dashboard; where the company has turned it off,
    the page answering instead says only why the caller was sent away. The caller
    never asked for the dashboard, so its being off is not a second refusal."""
    notice = _text("perm.redirected_no_access", lang)
    async with await _client(lang) as c:
        with _dashboard_turned_off():
            refused = await c.get("/modules")
            landed = await c.get(refused.headers["location"])
    assert _error_boxes(landed.text) == [notice]


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "de"])
async def test_a_module_turned_off_is_named_when_opened_directly(lang):
    async with await _client(lang) as c:
        with _dashboard_turned_off():
            r = await c.get("/dashboard")
    assert r.status_code == 403
    [box] = _error_boxes(r.text)
    assert "Dashboard" in box


@pytest.mark.asyncio
@pytest.mark.parametrize("role, shown", [("manager", False), ("owner", True)])
async def test_subscriptions_sidebar_offers_only_links_the_role_can_open(role, shown):
    """The /subscriptions shell is built for the caller's role, so a manager is not
    offered Modules or Company Details, which would only refuse them."""
    company = {**_MANAGER, "current_role": role}
    async with await _client() as c:
        c.cookies.set("celerp_token", make_test_token(role=role))
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
                patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": []})):
            r = await c.get("/subscriptions")
    assert r.status_code == 200, r.text
    assert ('href="/modules"' in r.text) is shown
    assert ('href="/finance/company-details"' in r.text) is shown
