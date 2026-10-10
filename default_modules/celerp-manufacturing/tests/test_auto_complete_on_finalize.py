# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Auto-complete work orders on invoice posting (setting ``auto_complete_work_orders``).

When both ``auto_create_work_orders`` and ``auto_complete_work_orders`` are on, finalizing
a sales invoice creates a work order per manufacturable line AND completes it on the spot:
components consumed, finished goods produced, completion JE posted, with an in-app
notification. A per-line savepoint isolates a completion failure so the invoice still
finalizes and the run is left ``planned`` for manual completion.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp_manufacturing import movements
from stock_books import assert_settled


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@mfgac.test"
    r = await client.post("/auth/register", json={"company_name": "MfgAC Co", "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku, **kw) -> str:
    body = {"status": "available", "sku": sku, "name": sku, "quantity": kw.pop("quantity", 0), "sell_by": "piece", **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _recipe(client, token, item_id, components) -> None:
    r = await client.put(f"/manufacturing/items/{item_id}/recipe", headers=_h(token),
                         json={"output_qty": 1, "components": components, "labor": [], "overhead": []})
    assert r.status_code == 200, r.text


async def _enable(client, token, *, auto_create=True, auto_complete=True) -> None:
    r = await client.patch("/companies/me", headers=_h(token), json={"settings": {"manufacturing": {
        "auto_create_work_orders": auto_create,
        "auto_complete_work_orders": auto_complete,
    }}})
    assert r.status_code == 200, r.text


async def _finalize_invoice(client, token, item_id, sku, qty) -> str:
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "line_items": [{"item_id": item_id, "sku": sku, "name": sku, "quantity": qty, "unit_price": 100}],
        "total": 0,
    })
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))).status_code == 200
    return doc_id


async def _runs_for(client, token, item_id) -> list[dict]:
    items = (await client.get("/manufacturing", headers=_h(token))).json()["items"]
    return [o for o in items if o.get("output_item_id") == item_id]


async def _qty(client, token, item_id) -> float:
    return (await client.get(f"/items/{item_id}", headers=_h(token))).json()["quantity"]


async def _notifs(client, token) -> list[dict]:
    return (await client.get("/notifications", headers=_h(token))).json()["items"]


@pytest.mark.asyncio
async def test_auto_complete_on_finalize_completes_run(client):
    """Both settings on: finalize creates a run and completes it - raw stock drops, the run
    is ``completed``, and a manufacturing notification discloses it."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDAC", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGAC", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _enable(client, token)

    await _finalize_invoice(client, token, ring, "RINGAC", 2)

    runs = await _runs_for(client, token, ring)
    assert len(runs) == 1 and runs[0]["status"] == "completed"
    assert await _qty(client, token, gold) == 90  # 100 - 5*2 consumed by the run
    notes = await _notifs(client, token)
    assert any(n["category"] == "manufacturing" for n in notes)


async def _entries(session, company_id, pattern: str) -> list[tuple]:
    """The posted lines of the one journal entry whose id matches ``pattern``, sorted."""
    row = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.like(pattern)))).scalar_one()
    assert row.state["status"] == "posted", row.state
    return sorted((e["account"], float(e.get("debit") or 0), float(e.get("credit") or 0))
                  for e in row.state["entries"])


@pytest.mark.asyncio
async def test_auto_complete_posts_completion_je(client, session):
    """The run moves the components through work in progress onto the made goods account,
    the invoice's cost of sale comes off that account, and shipping the line ships the made
    lot: every inventory account then carries exactly the stock it holds."""
    token = await _register(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(token))).json()["id"])
    gold = await _item(client, token, "GOLDJE", quantity=100, cost_total=8000)  # 80 each
    ring = await _item(client, token, "RINGJE", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _enable(client, token)

    doc = await _finalize_invoice(client, token, ring, "RINGJE", 2)

    run = (await _runs_for(client, token, ring))[0]["id"]
    assert await _entries(session, company_id, f"je:auto:{run}:issue:%") == [
        ("1130-OB", 0.0, 800.0), ("1130-WIP", 800.0, 0.0)]
    assert await _entries(session, company_id, f"je:auto:{run}:receive:%") == [
        ("1130-P", 800.0, 0.0), ("1130-WIP", 0.0, 800.0)]
    assert await _entries(session, company_id, "je:auto:doc:%:fin") == [
        ("1120", 200.0, 0.0), ("1130-P", 0.0, 800.0), ("4100", 0.0, 200.0), ("5100", 800.0, 0.0)]

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [ring]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "fulfilled"
    assert (await client.get(f"/docs/{doc}", headers=_h(token))).json()["fulfillment_status"] == "fulfilled"
    session.expire_all()
    await assert_settled(client, session, {"company_id": str(company_id)})


@pytest.mark.asyncio
async def test_auto_complete_idempotent_on_refinalize(client):
    """Revert to draft then finalize again: still exactly one run, still completed - a
    completed run counts as linked, so no second run is created."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDID", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGID", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _enable(client, token)

    doc = await _finalize_invoice(client, token, ring, "RINGID", 2)
    assert (await client.post(f"/docs/{doc}/revert-to-draft", headers=_h(token), json={"reason": "correction"})).status_code == 200
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(token))).status_code == 200

    runs = await _runs_for(client, token, ring)
    assert len(runs) == 1 and runs[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_partial_completion_rolls_back(client):
    """A mid-completion failure rolls the line back to its savepoint: the run stays ``planned``,
    the component consumption and its entry are undone, the invoice still finalizes, and a
    high-priority notification names the run left open. Receiving the output is made to fail,
    so the rollback has an issue already written to undo.
    """
    token = await _register(client)
    gold = await _item(client, token, "GOLDRB", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGRB", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _enable(client, token)

    with patch.object(movements, "_receive", new=AsyncMock(side_effect=RuntimeError("boom"))):
        await _finalize_invoice(client, token, ring, "RINGRB", 2)

    runs = await _runs_for(client, token, ring)
    assert len(runs) == 1 and runs[0]["status"] == "planned"  # savepoint rolled back the issue
    assert await _qty(client, token, gold) == 100  # consumption undone
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    assert not any(runs[0]["id"] in (e["data"].get("memo") or "") for e in ledger)  # and its entry
    notes = await _notifs(client, token)
    assert any(n["category"] == "manufacturing" and n["priority"] == "high" for n in notes)


@pytest.mark.asyncio
async def test_auto_complete_off_leaves_run_planned(client):
    """Regression guard: with auto-complete off the run stays ``planned`` and nothing is
    consumed - the new setting is inert when off."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDOFF", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGOFF", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _enable(client, token, auto_create=True, auto_complete=False)

    await _finalize_invoice(client, token, ring, "RINGOFF", 2)

    runs = await _runs_for(client, token, ring)
    assert len(runs) == 1 and runs[0]["status"] == "planned"
    assert await _qty(client, token, gold) == 100
