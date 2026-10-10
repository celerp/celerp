# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A bulk run action handles each run whole or not at all.

Completing a run issues its components, receives its output and closes it. A run
refused part way leaves none of that behind and is listed as skipped with its
reason; the other selected runs are still completed.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.services import auto_je


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@bulkrun.test"
    r = await client.post("/auth/register", json={
        "company_name": "Bulk Run Co", "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku, **kw) -> str:
    body = {"status": "available", "sku": sku, "name": sku, "sell_by": "piece", **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _run_of(client, token, product: str, component: str) -> str:
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=_h(token),
                         json={"output_qty": 1, "components": [{"item_id": component, "quantity": 1}],
                               "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    r = await client.post(f"/manufacturing/items/{product}/build", headers=_h(token), json={"quantity": 1})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _status(client, token, run_id: str) -> str:
    return (await client.get(f"/manufacturing/{run_id}", headers=_h(token))).json()["status"]


async def _qty(client, token, item_id: str) -> float:
    return float((await client.get(f"/items/{item_id}", headers=_h(token))).json()["quantity"])


@pytest.mark.asyncio
async def test_a_run_refused_after_its_components_were_issued_is_skipped_whole(client):
    """The second run's output is a draft item, so it is refused at receipt, after its
    components were issued. It is skipped with nothing issued; the first run completes."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=10, cost_total=800)
    ring = await _item(client, token, "RING", quantity=0)
    pendant = await _item(client, token, "PEND", quantity=0)
    good = await _run_of(client, token, ring, gold)
    refused = await _run_of(client, token, pendant, gold)
    r = await client.post("/items/bulk/revert-to-draft", headers=_h(token), json={"entity_ids": [pendant]})
    assert r.status_code == 200, r.text

    r = await client.post("/manufacturing/bulk-action", headers=_h(token),
                          json={"run_ids": [refused, good], "action": "complete"})

    assert r.status_code == 200, r.text
    assert r.json()["done"] == [good]
    assert [s["id"] for s in r.json()["skipped"]] == [refused]
    assert "PEND is a draft, not stock yet" in r.json()["skipped"][0]["reason"]
    assert await _qty(client, token, gold) == 9.0
    assert await _status(client, token, good) == "completed"
    assert await _status(client, token, refused) != "completed"


@pytest.mark.asyncio
async def test_a_run_whose_completion_entry_fails_keeps_nothing(client, monkeypatch):
    """The completion journal entry of the first run is refused after the run was issued,
    received and marked completed. None of that may be kept for a run reported skipped."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=10, cost_total=800)
    ring = await _item(client, token, "RING", quantity=0)
    chain = await _item(client, token, "CHAIN", quantity=0)
    refused = await _run_of(client, token, ring, gold)
    good = await _run_of(client, token, chain, gold)

    real = auto_je.create_for_mfg_movement

    async def refuse_first(session, *, order_id, movement, **kw):
        if order_id == refused and movement.startswith("complete:"):
            raise auto_je.UnbalancedJournalEntry(f"Auto JE for {order_id} completion: does not balance")
        return await real(session, order_id=order_id, movement=movement, **kw)

    monkeypatch.setattr(auto_je, "create_for_mfg_movement", refuse_first)

    r = await client.post("/manufacturing/bulk-action", headers=_h(token),
                          json={"run_ids": [refused, good], "action": "complete"})

    assert r.status_code == 200, r.text
    assert [s["id"] for s in r.json()["skipped"]] == [refused]
    assert await _status(client, token, refused) != "completed"
    assert await _qty(client, token, gold) == 9.0
    assert await _qty(client, token, ring) == 0.0
    assert r.json()["done"] == [good]
    assert await _status(client, token, good) == "completed"
