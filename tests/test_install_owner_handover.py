# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Users tab offers the installation owner, and nobody else, a control that
hands installation ownership to another active user, confirmed first and
redrawn in place."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.api_client import APIError

_ME = "11111111-1111-1111-1111-111111111111"
_HEIR = "22222222-2222-2222-2222-222222222222"
_GONE = "33333333-3333-3333-3333-333333333333"
_USERS = {"items": [
    {"id": _ME, "email": "me@example.com", "name": "Me", "role": "owner", "is_active": True},
    {"id": _HEIR, "email": "heir@example.com", "name": "Heir", "role": "admin", "is_active": True},
    {"id": _GONE, "email": "gone@example.com", "name": "Gone", "role": "viewer", "is_active": False},
]}
_COMPANY = {"id": "c1", "name": "Acme", "settings": {}}
_HX = {"HX-Request": "true"}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _cookies(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(user_id=_ME, role=role)}


def _api(install_owner: bool, transfer=None):
    return (patch("ui.api_client.get_users", new=AsyncMock(return_value=_USERS)),
            patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
            patch("ui.api_client.installation_owner", new=AsyncMock(return_value=install_owner)),
            patch("ui.api_client.transfer_install_owner", new=transfer or AsyncMock(return_value={"ok": True}),
                  create=True))


async def _users_tab(client, install_owner: bool, role: str = "owner") -> str:
    a, b, c, d = _api(install_owner)
    with a, b, c, d:
        r = await client.get("/settings/general?tab=users", cookies=_cookies(role))
    assert r.status_code == 200
    return r.text


@pytest.mark.asyncio
async def test_installation_owner_sees_handover_for_other_active_users(ui_client):
    html = await _users_tab(ui_client, install_owner=True)
    assert f'hx-post="/settings/users/{_HEIR}/installation-owner"' in html
    assert f"/settings/users/{_ME}/installation-owner" not in html
    assert f"/settings/users/{_GONE}/installation-owner" not in html
    assert 'hx-confirm="Make Heir the installation owner?' in html
    assert 'hx-target="#users-card"' in html and 'id="users-card"' in html


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_handover_is_hidden_from_anyone_but_the_installation_owner(ui_client, role):
    html = await _users_tab(ui_client, install_owner=False, role=role)
    assert "installation-owner" not in html


@pytest.mark.asyncio
async def test_handover_redraws_the_card_in_place_and_drops_the_control(ui_client):
    transfer = AsyncMock(return_value={"ok": True})
    a, b, c, d = _api(install_owner=False, transfer=transfer)
    with a, b, c, d:
        r = await ui_client.post(f"/settings/users/{_HEIR}/installation-owner", cookies=_cookies(), headers=_HX)
    transfer.assert_awaited_once()
    assert transfer.await_args.args[1] == _HEIR
    assert r.status_code == 200
    assert 'id="users-card"' in r.text
    assert "Heir is now the installation owner." in r.text
    assert "flash--success" in r.text
    assert "installation-owner" not in r.text  # the viewer no longer owns it
    assert "<html" not in r.text  # a fragment, not a page reload


@pytest.mark.asyncio
async def test_refused_handover_says_why_and_changes_nothing(ui_client):
    transfer = AsyncMock(side_effect=APIError(403, "Installation owner access required"))
    a, b, c, d = _api(install_owner=False, transfer=transfer)
    with a, b, c, d:
        r = await ui_client.post(f"/settings/users/{_HEIR}/installation-owner", cookies=_cookies("admin"), headers=_HX)
    assert "Installation owner access required" in r.text
    assert "flash--error" in r.text
    assert "is now the installation owner" not in r.text
