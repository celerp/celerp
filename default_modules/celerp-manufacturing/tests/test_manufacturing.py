# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


async def _register(client, email: str | None = None) -> str:
    addr = email or f"admin-{uuid.uuid4().hex[:8]}@mfg.test"
    r = await client.post("/auth/register", json={"company_name": "Mfg Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _balanced(entries: list[dict]) -> None:
    d = sum(float(x.get("debit", 0) or 0) for x in entries)
    c = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(d - c) < 1e-6


@pytest.mark.asyncio
async def test_full_assembly_flow_with_lineage_and_je(client, session):
    token = await _register(client)
    raw = await client.post("/items", headers=_h(token), json={"sku": "RAW-A", "name": "Raw A", "quantity": 10, "sell_by": "piece"})
    raw_id = raw.json()["id"]

    order = await client.post(
        "/manufacturing",
        headers=_h(token),
        json={
            "description": "Assembly",
            "estimated_cost": 120,
            "inputs": [{"item_id": raw_id, "quantity": 4}],
            "expected_outputs": [{"sku": "FG-A", "name": "Finished A", "quantity": 2}],
        },
    )
    assert order.status_code == 200
    order_id = order.json()["id"]

    assert (await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": raw_id, "quantity": 4})).status_code == 200
    assert (await client.post(f"/manufacturing/{order_id}/start", headers=_h(token))).status_code == 200
    done = await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={"waste_quantity": 1})
    assert done.status_code == 200

    state = (await client.get(f"/manufacturing/{order_id}", headers=_h(token))).json()
    assert state["status"] == "completed"
    assert state["is_in_production"] is False

    raw_state = (await client.get(f"/items/{raw_id}", headers=_h(token))).json()
    assert raw_state["quantity"] == 6

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    fg = next(i for i in items if i.get("sku") == "FG-A")
    assert fg["quantity"] == 2
    assert fg["manufacturing_order_id"] == order_id

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if order_id in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    assert [x["account"] for x in entries].count("1130") == 2
    assert any(x["account"] == "5100" for x in entries)
    _balanced(entries)

    je_row = (await session.execute(select(LedgerEntry).where(LedgerEntry.id == je["id"]))).scalar_one()
    assert je_row.metadata_["trigger"] == "mfg.order.completed"
    assert je_row.metadata_["order_id"] == order_id


@pytest.mark.asyncio
async def test_merge_flow_and_mfg_guards(client):
    token = await _register(client)
    i1 = (await client.post("/items", headers=_h(token), json={"sku": "RAW-1", "name": "Raw 1", "quantity": 5, "sell_by": "piece"})).json()["id"]
    i2 = (await client.post("/items", headers=_h(token), json={"sku": "RAW-2", "name": "Raw 2", "quantity": 5, "sell_by": "piece"})).json()["id"]

    order = await client.post(
        "/manufacturing",
        headers=_h(token),
        json={
            "description": "Merge",
            "inputs": [{"item_id": i1, "quantity": 2}, {"item_id": i2, "quantity": 3}],
            "expected_outputs": [{"sku": "FG-M", "name": "Merged", "quantity": 1}],
        },
    )
    order_id = order.json()["id"]

    # complete without consuming all inputs
    assert (await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": i1, "quantity": 2})).status_code == 200
    assert (await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={})).status_code == 409

    # consume missing item
    assert (await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": "item:missing", "quantity": 1})).status_code == 404

    # finish happy path
    assert (await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": i2, "quantity": 3})).status_code == 200
    assert (await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={})).status_code == 200

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    assert any(i.get("sku") == "FG-M" and i.get("quantity") == 1 for i in items)

    # already completed guards
    assert (await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={})).status_code == 409
    assert (await client.post(f"/manufacturing/{order_id}/cancel", headers=_h(token), json={"reason": "x"})).status_code == 409


@pytest.mark.asyncio
async def test_cancel_after_start_sets_cancelled_not_in_production(client):
    token = await _register(client)
    item_id = (await client.post("/items", headers=_h(token), json={"sku": "RAW-C", "name": "Raw C", "quantity": 4, "sell_by": "piece"})).json()["id"]
    order_id = (
        await client.post(
            "/manufacturing",
            headers=_h(token),
            json={"description": "Cancelable", "inputs": [{"item_id": item_id, "quantity": 1}], "expected_outputs": [{"sku": "FG-C", "name": "FG C", "quantity": 1}]},
        )
    ).json()["id"]

    await client.post(f"/manufacturing/{order_id}/start", headers=_h(token))
    r = await client.post(f"/manufacturing/{order_id}/cancel", headers=_h(token), json={"reason": "operator stop"})
    assert r.status_code == 200

    state = (await client.get(f"/manufacturing/{order_id}", headers=_h(token))).json()
    assert state["status"] == "cancelled"
    assert state["is_in_production"] is False


@pytest.mark.asyncio
async def test_bom_crud(client):
    token = await _register(client)

    create = await client.post(
        "/companies/me/boms",
        headers=_h(token),
        json={
            "bom_id": "bom:FG-A",
            "name": "FG A BOM",
            "version": 1,
            "inputs": [{"item_id": "item:raw", "quantity": 2}],
            "outputs": [{"sku": "FG-A", "name": "Finished A", "quantity": 1}],
            "is_active": True,
        },
    )
    assert create.status_code == 200

    listed = (await client.get("/companies/me/boms", headers=_h(token))).json()["items"]
    assert any(b["bom_id"] == "bom:FG-A" for b in listed)

    got = (await client.get("/companies/me/boms/bom:FG-A", headers=_h(token))).json()
    assert got["name"] == "FG A BOM"

    assert (
        await client.patch(
            "/companies/me/boms/bom:FG-A",
            headers=_h(token),
            json={"name": "FG A BOM Updated", "version": 2},
        )
    ).status_code == 200
    patched = (await client.get("/companies/me/boms/bom:FG-A", headers=_h(token))).json()
    assert patched["name"] == "FG A BOM Updated"
    assert patched["version"] == 2

    assert (await client.delete("/companies/me/boms/bom:FG-A", headers=_h(token))).status_code == 200
    deactivated = (await client.get("/companies/me/boms/bom:FG-A", headers=_h(token))).json()
    assert deactivated["is_active"] is False
