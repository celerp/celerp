# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest


async def _register(client, email: str = "admin@scan.test") -> str:
    r = await client.post("/auth/register", json={"company_name": "Scan Co", "email": email, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_scanning_resolve_not_found_and_found(client):
    token = await _register(client)
    r = await client.get("/scanning/resolve/NOPE", headers=_h(token))
    assert r.status_code == 404

    item = await client.post("/items", headers=_h(token), json={"sku": "SCAN-1", "name": "Scannable", "quantity": 1, "sell_by": "piece"})
    assert item.status_code == 200
    resolved = await client.get("/scanning/resolve/SCAN-1", headers=_h(token))
    assert resolved.status_code == 200
    assert resolved.json()["entity_type"] == "item"


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_dashboard_activity_contains_recent_events(client):
    token = await _register(client, email="admin2@scan.test")
    await client.post("/items", headers=_h(token), json={"sku": "A1", "name": "A", "quantity": 1, "sell_by": "piece"})
    await client.post("/scanning/scan", headers=_h(token), json={"code": "A1"})

    kpis = await client.get("/dashboard/kpis", headers=_h(token))
    assert kpis.status_code == 200
    data = kpis.json()
    assert "inventory" in data and "sales" in data

    activity = await client.get("/dashboard/activity?limit=10", headers=_h(token))
    assert activity.status_code == 200
    acts = activity.json()["activities"]
    assert len(acts) >= 1
    assert all("event_type" in a and "entity_id" in a for a in acts)
