# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every role sees the names of the company's members; only a role that can manage
users sees their email addresses.

Checked through the member list route and through the assistant's tool for the same
list, for each role, and again after the owner grants Manage users to a role that
does not hold it by default.
"""
from __future__ import annotations

import os

import pytest

from celerp.ai import tools as ai_tools

pytestmark = pytest.mark.asyncio

_ROLES = ("viewer", "operator", "manager", "admin")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, session, owner_h: dict, role: str) -> dict:
    addr = f"{role}-{os.urandom(4).hex()}@example.com"
    r = await client.post("/companies/me/users", headers=owner_h, json={
        "name": role.title(), "email": addr, "password": "testpass123", "role": role})
    assert r.status_code == 200, r.text
    from test_helpers import clear_sessions
    await clear_sessions(session)
    r = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r.status_code == 200, r.text
    return _h(r.json()["access_token"])


async def _via_tool(h: dict, role: str) -> list[dict]:
    from celerp.main import app
    caps = ai_tools.compile_agent_capabilities(app, {}, role, caller_gates=frozenset())
    cap = next(c for c in caps.values() if c["method"] == "GET" and c["path"] == "/companies/me/users")
    got = await ai_tools.execute_agent_capability(app, h["Authorization"], cap, {}, "call-members")
    assert got["ok"] is True, got
    return got["data"]["items"]


def _check(items: list[dict], emails: bool, who: str) -> None:
    assert items and all(m["name"] for m in items), (who, items)
    shown = [m["email"] for m in items]
    if emails:
        assert all(shown), (who, shown)
    else:
        assert not any(shown), (who, shown)


async def test_member_emails_follow_manage_users_on_the_route_and_the_tool(client, session):
    r = await client.post("/auth/register", json={
        "company_name": "Members Co", "email": f"own-{os.urandom(4).hex()}@example.com",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    owner_h = _h(r.json()["access_token"])
    logins = {"owner": owner_h}
    for role in _ROLES:
        logins[role] = await _login(client, session, owner_h, role)

    async def both(role: str, emails: bool) -> None:
        h = logins[role]
        r = await client.get("/companies/me/users", headers=h)
        assert r.status_code == 200, r.text
        _check(r.json()["items"], emails, f"route as {role}")
        _check(await _via_tool(h, role), emails, f"tool as {role}")

    defaults = {"viewer": False, "operator": False, "manager": False, "admin": True, "owner": True}
    for role, emails in defaults.items():
        await both(role, emails)

    r = await client.patch("/companies/me/role-permissions", headers=owner_h,
                           json={"perm_key": "manage_users", "role_key": "manager", "granted": True})
    assert r.status_code == 200, r.text
    await both("manager", True)
    await both("operator", False)
