# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Production-run execution: build -> issue components -> receive finished goods.

Covers the headline P3 correctness rule — completion restocks the *real* product (keyed on
``allow_splitting``: increment the SKU, or create a discrete lot under it) and never spawns a
nameless throwaway item — plus the completion journal entry and status transitions.
"""
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


async def _item(client, token, sku, **kw) -> str:
    body = {"sku": sku, "name": sku, "quantity": kw.pop("quantity", 0), "sell_by": "piece",
            "status": kw.pop("status", "available"), **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _recipe(client, token, item_id, components) -> None:
    r = await client.put(f"/manufacturing/items/{item_id}/recipe", headers=_h(token),
                         json={"output_qty": 1, "components": components, "labor": [], "overhead": []})
    assert r.status_code == 200, r.text


def _balanced(entries: list[dict]) -> None:
    d = sum(float(x.get("debit", 0) or 0) for x in entries)
    c = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(d - c) < 1e-6


@pytest.mark.asyncio
async def test_build_issue_receive_restocks_product_and_posts_je(client, session):
    """The full two-step run: issue decrements components and starts the run; receive restocks the
    product (same SKU, incremented) and completes it. No nameless item is ever created."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=100, cost_total=8000)  # unit cost 80
    ring = await _item(client, token, "RING", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 2})).json()["id"]

    # Issue all components -> components decrement, run auto-advances to In Progress.
    assert (await client.post(f"/manufacturing/{run}/issue", headers=_h(token))).status_code == 200
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert state["status"] == "in_progress" and state["is_in_production"] is True
    assert (await client.get(f"/items/{gold}", headers=_h(token))).json()["quantity"] == 90  # 100 - 10

    # Receive all -> product restocked (same item), run auto-completes.
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token))).status_code == 200
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert state["status"] == "completed" and state["is_in_production"] is False

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    rings = [i for i in items if i.get("sku") == "RING"]
    assert len(rings) == 1 and rings[0]["id"] == ring and rings[0]["quantity"] == 2  # incremented, not a clone
    assert not any(i.get("name") in (None, "") for i in items)  # no nameless throwaway item

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if run in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    assert [x["account"] for x in entries].count("1130-P") == 2
    _balanced(entries)
    je_row = (await session.execute(select(LedgerEntry).where(LedgerEntry.id == je["id"]))).scalar_one()
    assert je_row.metadata_["trigger"] == "mfg.order.completed" and je_row.metadata_["order_id"] == run


@pytest.mark.asyncio
async def test_one_tap_build_completes_in_one_call(client):
    """complete=true issues + receives + completes in a single action (restaurant / make-to-stock)."""
    token = await _register(client)
    gold = await _item(client, token, "G", quantity=100, cost_total=8000)
    ring = await _item(client, token, "R", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token),
                             json={"quantity": 3, "complete": True})).json()["id"]
    assert (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()["status"] == "completed"
    assert (await client.get(f"/items/{gold}", headers=_h(token))).json()["quantity"] == 85  # 100 - 15
    assert (await client.get(f"/items/{ring}", headers=_h(token))).json()["quantity"] == 3


@pytest.mark.asyncio
async def test_non_splittable_output_creates_lot_under_product(client):
    """allow_splitting=false: each run yields a discrete lot linked to the product (not the SKU pile)."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDX", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGX", quantity=0, allow_splitting=False)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token),
                             json={"quantity": 2, "complete": True})).json()["id"]
    rcv = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    rings = [i for i in items if i.get("sku") == "RINGX"]
    assert len(rings) == 2  # the product + one new lot
    product = next(i for i in rings if i["id"] == ring)
    lot = next(i for i in rings if i["id"] != ring)
    assert product["quantity"] == 0  # the catalog product is not the pile for discrete items
    assert lot["quantity"] == 2 and lot["parent_item_id"] == ring and lot.get("lot") is True
    assert lot["manufacturing_order_id"] == run


@pytest.mark.asyncio
async def test_cancel_after_issue_sets_cancelled_not_in_production(client):
    token = await _register(client)
    gold = await _item(client, token, "GC", quantity=100, cost_total=100)
    ring = await _item(client, token, "RC", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 1})).json()["id"]

    await client.post(f"/manufacturing/{run}/issue", headers=_h(token))
    r = await client.post(f"/manufacturing/{run}/cancel", headers=_h(token), json={"reason": "operator stop"})
    assert r.status_code == 200
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert state["status"] == "cancelled" and state["is_in_production"] is False

    # A closed run rejects further issue/receive/complete.
    assert (await client.post(f"/manufacturing/{run}/issue", headers=_h(token))).status_code == 409
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token))).status_code == 409
    assert (await client.post(f"/manufacturing/{run}/cancel", headers=_h(token), json={"reason": "x"})).status_code == 409


@pytest.mark.asyncio
async def test_schedule_run_sets_due_priority_and_guards(client):
    """Phase-A scheduling: due date / priority persist; only provided keys are written; a closed
    run cannot be rescheduled; an empty payload is rejected."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDS", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGS", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 1})).json()["id"]

    # Set due date + priority.
    assert (await client.post(f"/manufacturing/{run}/schedule", headers=_h(token),
                              json={"due_date": "2026-07-01", "priority": "high"})).status_code == 200
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert state["due_date"] == "2026-07-01" and state["priority"] == "high"

    # Partial update leaves the untouched field intact.
    assert (await client.post(f"/manufacturing/{run}/schedule", headers=_h(token),
                              json={"priority": "urgent"})).status_code == 200
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert state["due_date"] == "2026-07-01" and state["priority"] == "urgent"

    # Empty payload is rejected.
    assert (await client.post(f"/manufacturing/{run}/schedule", headers=_h(token), json={})).status_code == 422

    # A closed run cannot be rescheduled.
    await client.post(f"/manufacturing/{run}/cancel", headers=_h(token), json={"reason": "x"})
    assert (await client.post(f"/manufacturing/{run}/schedule", headers=_h(token),
                              json={"due_date": "2026-08-01"})).status_code == 409


@pytest.mark.asyncio
async def test_company_boms_endpoint_removed(client):
    # The core /companies/me/boms surface was purged — recipes live on the item now.
    token = await _register(client)
    assert (await client.get("/companies/me/boms", headers=_h(token))).status_code in (404, 405)
    assert (await client.post("/companies/me/boms", headers=_h(token), json={"bom_id": "x", "name": "x"})).status_code in (404, 405)
