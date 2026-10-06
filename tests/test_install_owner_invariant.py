# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The installation owner keeps access to every company they belong to: no member can
deactivate them in any company, however many companies they belong to. The refusal
says how to do it: make someone else the installation owner first, in Global Config,
Users, which the installation owner can do there."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app
from test_helpers import invite_user, register_admin
from ui.app import app as ui_app

pytestmark = pytest.mark.asyncio


async def _two_owners(client, session):
    """(install owner headers, install owner id, second owner token, second owner id):
    the installation owner also owns a second company, so this is not their last
    active membership."""
    admin_token = await register_admin(client)
    admin_h = {"Authorization": f"Bearer {admin_token}"}
    second_token = await invite_user(client, session, admin_h, "second-owner@example.test", "owner")
    users = (await client.get("/companies/me/users", headers=admin_h)).json()["items"]
    admin_id = next(u["id"] for u in users if u["email"] == "admin@perm.example")
    second_id = next(u["id"] for u in users if u["email"] == "second-owner@example.test")
    r = await client.post("/companies", json={"name": "Second Co"}, headers=admin_h)
    assert r.status_code == 200, r.text
    return admin_h, admin_id, second_token, second_id


@asynccontextmanager
async def _ui_as(token: str):
    def _bridged(tok, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {tok}"}, follow_redirects=True)

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               follow_redirects=False, cookies={"celerp_token": token}) as ui:
            yield ui


async def test_api_refuses_to_deactivate_the_installation_owner_in_any_company(client, session):
    admin_h, admin_id, second_token, _ = await _two_owners(client, session)
    r = await client.patch(f"/companies/me/users/{admin_id}", json={"is_active": False},
                           headers={"Authorization": f"Bearer {second_token}"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "installation owner" in detail
    assert "Global Config" in detail and "Users" in detail
    users = (await client.get("/companies/me/users", headers=admin_h)).json()["items"]
    assert next(u for u in users if u["id"] == admin_id)["is_active"] is True


async def test_users_screen_shows_the_refusal_and_where_to_transfer(client, session):
    _, admin_id, second_token, _ = await _two_owners(client, session)
    async with _ui_as(second_token) as ui:
        r = await ui.patch(f"/settings/users/{admin_id}/is_active", data={"value": "false"})
    assert r.status_code == 200
    assert "cell-error" in r.text
    assert "installation owner" in r.text and "Global Config" in r.text


async def test_users_list_marks_the_installation_owner(client, session):
    admin_h, admin_id, _, second_id = await _two_owners(client, session)
    users = (await client.get("/companies/me/users", headers=admin_h)).json()["items"]
    flags = {u["id"]: u["is_install_owner"] for u in users}
    assert flags == {admin_id: True, second_id: False}


async def test_installation_owner_transfers_ownership_from_the_users_screen(client, session):
    admin_h, admin_id, second_token, second_id = await _two_owners(client, session)
    admin_token = admin_h["Authorization"].split()[1]
    async with _ui_as(second_token) as ui:
        page = await ui.get("/settings/general?tab=users")
        assert f"/settings/users/{admin_id}/installation-owner" not in page.text
    async with _ui_as(admin_token) as ui:
        page = await ui.get("/settings/general?tab=users")
        assert f"/settings/users/{second_id}/installation-owner" in page.text
        r = await ui.post(f"/settings/users/{second_id}/installation-owner")
    assert r.status_code == 200, r.text
    users = (await client.get("/companies/me/users", headers=admin_h)).json()["items"]
    assert {u["id"]: u["is_install_owner"] for u in users} == {admin_id: False, second_id: True}
    # The new installation owner may now deactivate the old one, who is a plain member.
    r = await client.patch(f"/companies/me/users/{admin_id}", json={"is_active": False},
                           headers={"Authorization": f"Bearer {second_token}"})
    assert r.status_code == 200, r.text
