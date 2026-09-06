# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory read API enforces view_inventory itself, not via the UI gate.

Hiding the inventory route in the web UI is a convenience, never a control: the
`/items` list and `/items/valuation` endpoints must reject a caller whose role
has view_inventory dynamically revoked, regardless of what the browser still
shows or which access token it holds. These tests hit the API directly.
"""
from __future__ import annotations

import pytest

from test_helpers import perm_setup

_ROLE_PERM_URL = "/companies/me/role-permissions"


async def _revoke_view_inventory(client, owner_headers: dict, role: str) -> None:
    """Revoke view_inventory for exactly *role* through the owner-gated matrix."""
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "view_inventory", "role_key": role, "granted": False},
        headers=owner_headers,
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_revoked_view_inventory_blocks_list_items(client, session):
    ctx = await perm_setup(client, session)
    # The operator can list items before the revocation.
    before = await client.get("/items", headers=ctx["operator_h"])
    assert before.status_code == 200, before.text
    await _revoke_view_inventory(client, ctx["admin_h"], "operator")
    after = await client.get("/items", headers=ctx["operator_h"])
    assert after.status_code == 403, after.text


@pytest.mark.asyncio
async def test_revoked_view_inventory_blocks_valuation(client, session):
    ctx = await perm_setup(client, session)
    before = await client.get("/items/valuation", headers=ctx["operator_h"])
    assert before.status_code == 200, before.text
    await _revoke_view_inventory(client, ctx["admin_h"], "operator")
    after = await client.get("/items/valuation", headers=ctx["operator_h"])
    assert after.status_code == 403, after.text


@pytest.mark.asyncio
async def test_permitted_user_receives_items_and_valuation(client, session):
    ctx = await perm_setup(client, session)
    items = await client.get("/items", headers=ctx["admin_h"])
    assert items.status_code == 200, items.text
    assert "items" in items.json()
    valuation = await client.get("/items/valuation", headers=ctx["admin_h"])
    assert valuation.status_code == 200, valuation.text
