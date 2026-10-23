# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A core page whose work is done by a module answers, while the company cannot use
that module, with the shared module-off page: turned off, or starting after Celerp
restarts, each in its own words, with the way forward."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token, mark_running

_DOCS_OFF = {"enabled_modules": ["celerp-inventory"]}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token()})
        yield c


async def _payments(ui_client, settings: dict, role: str = "owner"):
    company = {"settings": settings, "current_role": role}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_relay_status", new=AsyncMock(return_value={"connected": True})), \
         patch("ui.api_client.get_payments_status", new=AsyncMock(return_value={"enabled": False})), \
         patch("ui.routes.settings_payments._check_permission", new=AsyncMock(return_value=None)):
        return await ui_client.get("/settings/payments")


async def test_payments_page_opens_while_documents_runs_for_the_company(ui_client, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    r = await _payments(ui_client, {})
    assert r.status_code == 200, r.text
    assert "Module turned off" not in r.text and "Module not started yet" not in r.text


async def test_payments_page_with_documents_off_shows_the_module_off_page(ui_client, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    r = await _payments(ui_client, _DOCS_OFF)
    assert r.status_code == 403
    assert "Module turned off" in r.text
    assert "The Documents module is turned off for your company." in r.text
    assert 'href="/modules"' in r.text


async def test_payments_page_before_documents_starts_says_it_starts_after_a_restart(ui_client):
    r = await _payments(ui_client, {})
    assert r.status_code == 403
    assert "Module not started yet" in r.text
    assert "The Documents module starts after Celerp restarts." in r.text
    assert "find this page" not in r.text
    assert 'href="/modules"' in r.text


async def test_someone_who_cannot_manage_modules_is_told_who_can_act(ui_client):
    r = await _payments(ui_client, {}, role="viewer")
    assert r.status_code == 403
    assert "Ask an administrator to restart Celerp." in r.text


async def test_payments_page_with_documents_off_for_a_viewer_asks_to_turn_it_on(ui_client, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    r = await _payments(ui_client, _DOCS_OFF, role="viewer")
    assert r.status_code == 403
    assert "Ask an administrator to turn it on in Modules." in r.text
