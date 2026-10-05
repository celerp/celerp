# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A page the caller's role may not open sends them to the dashboard, which says
why; a silent bounce reads as a broken link."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

_NOTICE = "You do not have access to the page you opened. Ask an administrator if you need it."


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/modules", "/settings/payments"])
async def test_a_page_the_role_may_not_open_sends_it_to_the_dashboard_with_a_reason(ui_client, path):
    company = {"id": "c1", "name": "B", "current_role": "manager", "settings": {}}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
        r = await ui_client.get(path, cookies={"celerp_token": make_test_token(role="manager")})
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard?notice=not_permitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("query,shown", [("?notice=not_permitted", True), ("", False), ("?notice=other", False)])
async def test_the_dashboard_says_why_the_caller_landed_there(ui_client, query, shown):
    company = {"id": "c1", "name": "B", "current_role": "manager", "settings": {}}
    load = AsyncMock(return_value=(company, {}, {}, {}, []))
    with patch("ui.routes.dashboard._load_dashboard", new=load), \
            patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
            patch("ui.api_client.get_ar_aging", new=AsyncMock(return_value={"buckets": {}})), \
            patch("ui.api_client.get_activity", new=AsyncMock(return_value=[])):
        r = await ui_client.get(f"/dashboard{query}", cookies={"celerp_token": make_test_token(role="manager")})
    assert r.status_code == 200, r.text[:300]
    assert (_NOTICE in r.text) is shown


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/modules", "/settings/payments"])
async def test_a_company_with_the_dashboard_turned_off_is_still_told_why(ui_client, path):
    """The redirect lands on the dashboard; where the company has turned it off,
    the page that answers instead still says why the caller was sent away."""
    company = {"id": "c1", "name": "B", "current_role": "manager",
               "settings": {"enabled_modules": ["celerp-contacts"]}}
    cookies = {"celerp_token": make_test_token(role="manager")}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
            patch("celerp.modules.loader.route_module",
                  lambda scope: "celerp-dashboard" if scope.get("path") == "/dashboard" else None):
        denied = await ui_client.get(path, cookies=cookies)
        assert denied.status_code == 302
        landed = await ui_client.get(denied.headers["location"], cookies=cookies)
    assert "This module is turned off for your company." in landed.text
    assert landed.text.count(_NOTICE) == 1
