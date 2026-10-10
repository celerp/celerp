# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every manufacturing endpoint's required permission, declared once and asserted.

The whole module answers to manage_manufacturing, reads included: work orders carry
costs, so a role without the grant sees none of it. Two endpoints need a second grant
on top. A new endpoint with no entry below must still require manage_manufacturing,
so the default is a decision rather than an oversight.
"""

from __future__ import annotations

import uuid

import pytest

from celerp.services.permissions import missing_permission_text
from celerp_manufacturing.routes import router

MFG = "manage_manufacturing"

EXPECTED: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/manufacturing/import/batch"): frozenset({MFG, "import_export_data"}),
    ("POST", "/manufacturing/{order_id}/reconcile"): frozenset({MFG, "manage_accounting"}),
}


def _declared_permissions(route) -> frozenset[str]:
    """The permissions a route requires, read off its dependency callables."""
    seen = set()
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        dep = stack.pop()
        val = getattr(getattr(dep, "call", None), "required_permission", None)
        if isinstance(val, str):
            seen.add(val)
        stack.extend(getattr(dep, "dependencies", []) or [])
    return frozenset(seen)


def test_every_endpoint_requires_exactly_the_permission_declared_for_it():
    found = {}
    for route in router.routes:
        path = getattr(route, "path", "")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            found[(method, path)] = _declared_permissions(route)

    assert found, "the router has no routes"
    missing = sorted(k for k in EXPECTED if k not in found)
    assert not missing, f"declared endpoints that no longer exist: {missing}"

    wrong = {key: (EXPECTED.get(key, frozenset({MFG})), got) for key, got in sorted(found.items())
             if got != EXPECTED.get(key, frozenset({MFG}))}
    assert not wrong, f"expected vs found, per endpoint: {wrong}"


async def _owner(client) -> dict:
    addr = f"owner-{uuid.uuid4().hex[:8]}@perm.test"
    r = await client.post("/auth/register", json={"company_name": "Perm Co", "email": addr,
                                                  "name": "Owner", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _member(client, session, owner: dict, role: str) -> dict:
    from test_helpers import invite_user

    token = await invite_user(client, session, owner, f"{role}-{uuid.uuid4().hex[:8]}@perm.test", role)
    return {"Authorization": f"Bearer {token}"}


async def _grant(client, owner: dict, perm: str, role: str) -> None:
    r = await client.patch("/companies/me/role-permissions", headers=owner,
                           json={"perm_key": perm, "role_key": role, "granted": True})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/manufacturing/to-make", "/manufacturing", "/manufacturing/work-centers",
                                  "/manufacturing/import/template"])
async def test_a_viewer_without_the_grant_is_refused(client, session, path):
    owner = await _owner(client)
    viewer = await _member(client, session, owner, "viewer")
    r = await client.get(path, headers=viewer)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == missing_permission_text(MFG)


@pytest.mark.asyncio
async def test_a_viewer_granted_manufacturing_can_read_it(client, session):
    owner = await _owner(client)
    viewer = await _member(client, session, owner, "viewer")
    await _grant(client, owner, MFG, "viewer")
    r = await client.get("/manufacturing/to-make", headers=viewer)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_manufacturing_without_accounting_cannot_reconcile(client, session):
    """An operator holds manage_manufacturing by default but not manage_accounting."""
    owner = await _owner(client)
    operator = await _member(client, session, owner, "operator")
    assert (await client.get("/manufacturing/to-make", headers=operator)).status_code == 200
    r = await client.post("/manufacturing/wo:none/reconcile", headers=operator, json={})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == missing_permission_text("manage_accounting")
