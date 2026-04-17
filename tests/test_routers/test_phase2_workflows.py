# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import pytest


async def _register(client):
    r = await client.post("/auth/register", json={"company_name": "P2 Co", "email": "admin@p2.test", "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_invoice_lifecycle_with_payment_and_je(client):
    token = await _register(client)
    create = await client.post(
        "/docs",
        headers=_auth(token),
        json={"doc_type": "invoice", "contact_id": "contact:1", "line_items": [{"name": "A", "quantity": 1, "unit_price": 100, "line_total": 100}], "subtotal": 100, "tax": 7, "total": 107},
    )
    assert create.status_code == 200
    doc_id = create.json()["id"]

    assert (await client.post(f"/docs/{doc_id}/send", headers=_auth(token), json={})).status_code == 200
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=_auth(token))).status_code == 200
    assert (await client.post(f"/docs/{doc_id}/payment", headers=_auth(token), json={"amount": 107})).status_code == 200

    doc = (await client.get(f"/docs/{doc_id}", headers=_auth(token))).json()
    assert doc["status"] == "paid"
    assert doc["amount_outstanding"] == 0


@pytest.mark.asyncio
async def test_manufacturing_flow_and_dashboard(client):
    token = await _register(client)
    item = await client.post("/items", headers=_auth(token), json={"sku": "RAW-1", "name": "Raw", "quantity": 5, "sell_by": "piece"})
    item_id = item.json()["id"]

    order = await client.post(
        "/manufacturing",
        headers=_auth(token),
        json={"description": "Build", "inputs": [{"item_id": item_id, "quantity": 2}], "expected_outputs": [{"sku": "FG-1", "name": "FG", "quantity": 1}]},
    )
    assert order.status_code == 200
    order_id = order.json()["id"]

    assert (await client.post(f"/manufacturing/{order_id}/start", headers=_auth(token))).status_code == 200
    assert (await client.post(f"/manufacturing/{order_id}/consume", headers=_auth(token), json={"item_id": item_id, "quantity": 2})).status_code == 200
    assert (await client.post(f"/manufacturing/{order_id}/complete", headers=_auth(token), json={})).status_code == 200

    kpis = (await client.get("/dashboard/kpis", headers=_auth(token))).json()
    assert "manufacturing" in kpis


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_scanning_and_bom_endpoints(client):
    token = await _register(client)
    assert (await client.post("/companies/me/boms", headers=_auth(token), json={"bom_id": "bom:1", "name": "B", "inputs": [], "outputs": []})).status_code == 200
    boms = (await client.get("/companies/me/boms", headers=_auth(token))).json()["items"]
    assert len(boms) == 1

    scan = await client.post("/scanning/scan", headers=_auth(token), json={"code": "X-1"})
    assert scan.status_code == 200
