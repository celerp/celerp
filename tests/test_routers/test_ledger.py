# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import pytest


async def _headers(client) -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_ledger_list_get_and_rebuild(client):
    headers = await _headers(client)

    r = await client.post("/items", json={"sku": "SKU1", "name": "Thing", "quantity": 2, "sell_by": "piece"}, headers=headers)
    assert r.status_code == 200
    entity_id = r.json()["id"]

    r = await client.get("/ledger", headers=headers)
    assert r.status_code == 200
    entries = r.json()["items"]
    assert entries

    entry_id = entries[0]["id"]
    r = await client.get(f"/ledger/{entry_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["id"]

    r = await client.get(f"/ledger?entity_id={entity_id}", headers=headers)
    assert r.status_code == 200

    r = await client.post("/ledger/rebuild", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_ledger_get_missing_404(client):
    headers = await _headers(client)
    r = await client.get("/ledger/9999", headers=headers)
    assert r.status_code == 404
