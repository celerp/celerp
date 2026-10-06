# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The pages the UI shows instead of a module page or a missing page.

- A module turned off for the company: refused with a sidebar built for this
  user and company (the module that is off is not listed, nor links the role
  may not use), and a link to the Modules page for a role that may open it;
  any other role is told to ask an administrator.
- The API cannot be reached: the page says so with the real status, never that
  the module is turned off.
- The 404 and 500 pages build their sidebar for the user who asked, not for an
  owner with empty settings."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from test_helpers import make_test_token
from ui.api_client import APIError

_NAV = [
    {"key": "fake", "label": "FakeModuleNav", "href": "/fake-page", "_module": "fake-mod", "order": 1},
    {"key": "fake-admin", "label": "FakeAdminNav", "href": "/fake-admin",
     "permission": "manage_company_settings", "order": 2},
]
_OFF = {"id": "c1", "name": "B", "settings": {"enabled_modules": ["celerp-contacts"]}}


def _off(role: str) -> dict:
    return {**_OFF, "current_role": role}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _gate(get_company):
    return (patch("celerp.modules.loader.route_module",
                  lambda scope: "fake-mod" if scope.get("path") == "/fake-page" else None),
            patch("celerp.modules.slots.get", lambda slot: list(_NAV) if slot == "nav" else []),
            patch("ui.api_client.get_company", new=get_company))


async def _get(client, role: str, get_company):
    a, b, c = _gate(get_company)
    with a, b, c:
        return await client.get("/fake-page", cookies={"celerp_token": make_test_token(role=role)})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,detail", [(503, "Celerp is not running."), (504, "Celerp took too long.")])
async def test_api_unreachable_is_not_reported_as_module_off(ui_client, status, detail):
    r = await _get(ui_client, "admin", AsyncMock(side_effect=APIError(status, detail)))
    assert r.status_code == status
    assert "turned off" not in r.text
    assert detail in r.text
    # No company was read, so no menu built from guessed settings.
    assert "FakeModuleNav" not in r.text
    assert 'href="/fake-page"' in r.text  # retry this page


@pytest.mark.asyncio
async def test_refusal_page_sidebar_hides_the_module_that_is_off(ui_client):
    r = await _get(ui_client, "admin", AsyncMock(return_value=_OFF))
    assert r.status_code == 403
    assert "turned off" in r.text
    assert "FakeModuleNav" not in r.text
    assert "FakeAdminNav" in r.text


@pytest.mark.asyncio
async def test_refusal_page_sidebar_follows_the_role(ui_client):
    r = await _get(ui_client, "viewer", AsyncMock(return_value=_OFF))
    assert r.status_code == 403
    assert "FakeAdminNav" not in r.text


@pytest.mark.asyncio
async def test_refusal_page_links_to_the_modules_page(ui_client):
    """The way on is the Modules page, where the module is turned back on. It is
    no module's page, so it answers even when the dashboard is the module that is
    off (/ lands on /dashboard, which would refuse again)."""
    from celerp.modules.loader import route_module
    r = await _get(ui_client, "admin", AsyncMock(return_value=_off("admin")))
    content = r.text.split('class="content-area"', 1)[1]
    assert 'href="/modules" class="btn btn--primary"' in content
    assert 'href="/"' not in content and 'href="/dashboard"' not in content
    assert route_module({"type": "http", "method": "GET", "path": "/modules", "root_path": "",
                         "query_string": b"", "headers": []}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator", "manager"])
async def test_refusal_page_asks_a_role_that_cannot_open_modules_to_ask_an_administrator(ui_client, role):
    """The Modules page would send this role away, so the refusal offers no link to it.
    The role on the page is the one Celerp holds, not the one in the cookie."""
    r = await _get(ui_client, "admin", AsyncMock(return_value=_off(role)))
    assert r.status_code == 403
    content = r.text.split('class="content-area"', 1)[1]
    assert 'href="/modules"' not in content
    assert "Ask an administrator to turn it on in Modules." in content


@pytest.mark.asyncio
async def test_refusal_message_sits_in_the_shell_content_area_once(ui_client):
    r = await _get(ui_client, "admin", AsyncMock(return_value=_off("admin")))
    assert r.text.count('class="content-area"') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_htmx_request_to_a_module_that_is_off_navigates_to_its_refusal_page(ui_client, method):
    """An HTMX request would swap the refusal page into a fragment, or show only a
    generic error toast. It is sent to the page a full request to the same address
    lands on: the page saying the module is turned off."""
    a, b, c = _gate(AsyncMock(return_value=_off("admin")))
    cookies = {"celerp_token": make_test_token(role="admin")}
    with a, b, c:
        r = await ui_client.request(method, "/fake-page?tab=2", cookies=cookies, headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert r.headers["HX-Redirect"] == "/fake-page?tab=2"
        full = await ui_client.get(r.headers["HX-Redirect"], cookies=cookies)
    assert full.status_code == 403
    assert "turned off" in full.text


def _request(role: str) -> Request:
    token = make_test_token(role=role)
    return Request({"type": "http", "method": "GET", "path": "/nowhere", "root_path": "",
                    "query_string": b"", "headers": [(b"cookie", f"celerp_token={token}".encode())]})


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["ui_404_handler", "ui_500_handler"])
async def test_error_page_sidebar_is_built_for_the_user(handler):
    import ui.app as app_mod
    with patch("celerp.modules.slots.get", lambda slot: list(_NAV) if slot == "nav" else []), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value=_OFF)):
        r = await getattr(app_mod, handler)(_request("viewer"), Exception("x"))
    body = r.body.decode()
    assert "FakeModuleNav" not in body
    assert "FakeAdminNav" not in body
    assert 'href="/dashboard"' not in body.split('class="content-area"', 1)[1]
