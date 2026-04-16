# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    email = f"admin-{uuid.uuid4().hex[:8]}@scan.test"
    r = await client.post("/auth/register", json={"company_name": "Scan Co", "email": email, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_scan_record_and_resolve_by_entity_id(client):
    token = await _register(client)
    item = await client.post("/items", headers=_h(token), json={"sku": "SCAN-1", "name": "Scannable", "quantity": 1, "sell_by": "piece"})
    item_id = item.json()["id"]

    scan = await client.post("/scanning/scan", headers=_h(token), json={"code": "SCAN-CODE-1", "location_id": "loc:1", "raw": {"source": "camera"}})
    assert scan.status_code == 200

    ledger = (await client.get("/ledger?entity_type=scan", headers=_h(token))).json()["items"]
    assert any(e["event_type"] == "scan.barcode" and e["data"]["code"] == "SCAN-CODE-1" for e in ledger)

    resolved = await client.get(f"/scanning/resolve/{item_id}", headers=_h(token))
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["id"] == item_id
    assert body["entity_type"] == "item"
    assert "mfg_consume" in body["available_actions"]


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_batch_scanning_start_and_complete(client):
    token = await _register(client)

    start = await client.post("/scanning/batch", headers=_h(token), json={"location_id": "loc:batch"})
    assert start.status_code == 200
    batch_id = start.json()["batch_id"]

    done = await client.post(f"/scanning/batch/{batch_id}/complete", headers=_h(token))
    assert done.status_code == 200
    assert done.json()["ok"] is True

    ledger = (await client.get("/ledger?entity_type=scan", headers=_h(token))).json()["items"]
    start_ev = next(e for e in ledger if e["entity_id"] == batch_id and e["data"].get("raw", {}).get("action") == "start")
    end_ev = next(e for e in ledger if e["entity_id"] == batch_id and e["data"].get("raw", {}).get("action") == "complete")
    assert start_ev["event_type"] == "scan.nfc"
    assert end_ev["event_type"] == "scan.nfc"

