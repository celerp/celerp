# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Global Config > Company, "Your companies": a deactivated company the user belongs to
is listed under "Deactivated", only when there is one. Its owner can reactivate it there
without leaving the company they are working in; any other member is told to ask the
company owner to reactivate it there."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app
from test_helpers import invite_user, register_admin
from ui.app import app as ui_app

pytestmark = pytest.mark.asyncio


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _deactivated_first_company(client, session):
    """(owner token in Second Co, member token in Member Co, Perm Co id): the owner and a
    manager both belong to Perm Co, which the owner then deactivates; each keeps working
    in another, active company."""
    admin_token = await register_admin(client)
    member_token = await invite_user(client, session, _h(admin_token), "member@example.test", "manager")
    perm_id = (await client.get("/auth/my-companies", headers=_h(admin_token))).json()["items"][0]["company_id"]
    r = await client.post("/companies", json={"name": "Second Co"}, headers=_h(admin_token))
    assert r.status_code == 200, r.text
    owner_token = r.json()["access_token"]
    r = await client.post("/companies", json={"name": "Member Co"}, headers=_h(member_token))
    assert r.status_code == 200, r.text
    member_token = r.json()["access_token"]
    r = await client.delete("/companies/me", headers=_h(admin_token))
    assert r.status_code == 200, r.text
    return owner_token, member_token, perm_id


@asynccontextmanager
async def _ui_as(token: str):
    def _bridged(tok, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {tok}"}, follow_redirects=True)

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               follow_redirects=False, cookies={"celerp_token": token}) as ui:
            yield ui


async def test_my_companies_lists_deactivated_companies_separately(client, session):
    owner_token, member_token, perm_id = await _deactivated_first_company(client, session)
    owner = (await client.get("/auth/my-companies", headers=_h(owner_token))).json()
    assert perm_id not in [c["company_id"] for c in owner["items"]]
    assert [(c["company_id"], c["company_name"], c["role"]) for c in owner["deactivated"]] == [
        (perm_id, "Perm Co", "owner")]
    member = (await client.get("/auth/my-companies", headers=_h(member_token))).json()
    assert [(c["company_id"], c["role"]) for c in member["deactivated"]] == [(perm_id, "manager")]


async def test_no_deactivated_section_without_a_deactivated_company(client, session):
    token = await register_admin(client)
    await client.post("/companies", json={"name": "Second Co"}, headers=_h(token))
    async with _ui_as(token) as ui:
        r = await ui.get("/settings/company/companies-list")
    assert r.status_code == 200
    assert "Second Co" in r.text
    assert "companies-deactivated" not in r.text


async def test_owner_reactivates_from_your_companies_and_stays_put(client, session):
    owner_token, _, perm_id = await _deactivated_first_company(client, session)
    async with _ui_as(owner_token) as ui:
        page = await ui.get("/settings/company/companies-list")
        assert "companies-deactivated" in page.text and "Perm Co" in page.text
        assert f'hx-post="/settings/company/{perm_id}/reactivate"' in page.text
        r = await ui.post(f"/settings/company/{perm_id}/reactivate")
    assert r.status_code == 200, r.text
    assert "Perm Co is active again" in r.text
    assert "celerp_token" not in r.headers.get("set-cookie", "")
    owner = (await client.get("/auth/my-companies", headers=_h(owner_token))).json()
    assert perm_id in [c["company_id"] for c in owner["items"]]
    assert owner["deactivated"] == []
    current = next(c for c in owner["items"] if c["is_current"])
    assert current["company_name"] == "Second Co"


async def test_member_is_told_to_ask_the_owner_and_cannot_reactivate(client, session):
    owner_token, member_token, perm_id = await _deactivated_first_company(client, session)
    async with _ui_as(member_token) as ui:
        page = await ui.get("/settings/company/companies-list")
        assert "Perm Co" in page.text
        assert "Ask the company owner to reactivate it" in page.text
        assert "/reactivate" not in page.text
        r = await ui.post(f"/settings/company/{perm_id}/reactivate")
    assert r.status_code == 200
    assert "cell-error" in r.text
    owner = (await client.get("/auth/my-companies", headers=_h(owner_token))).json()
    assert [c["company_id"] for c in owner["deactivated"]] == [perm_id]


async def test_switch_into_a_deactivated_company_says_to_ask_the_owner(client, session):
    _, member_token, perm_id = await _deactivated_first_company(client, session)
    r = await client.post(f"/auth/switch-company/{perm_id}", headers=_h(member_token))
    assert r.status_code == 403
    assert "Ask the company owner" in r.json()["detail"]
