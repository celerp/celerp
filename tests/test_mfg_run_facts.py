# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What a run says about itself comes from what was done with it.

An imported or created run cannot claim progress, a status, or a value: those come only from
the run's own movements. A component listed twice is one requirement for the total.
"""
from __future__ import annotations

import uuid

import pytest

from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)


@pytest.mark.asyncio
async def test_an_import_cannot_forge_a_runs_progress_status_or_value(client, auth):
    headers = auth["headers"]
    entity_id = "mfg:" + uuid.uuid4().hex
    r = await client.post("/manufacturing/import/batch", headers=headers, json={"records": [{
        "entity_id": entity_id, "event_type": "mfg.order.created", "source": "import",
        "idempotency_key": "forged-" + entity_id,
        "data": {"description": "Imported", "status": "completed", "received_qty": 9, "received_lots": ["item:x"],
                 "inputs": [{"item_id": "item:a", "quantity": 2, "issued_qty": 2},
                            {"item_id": "item:a", "quantity": 1}],
                 "expected_outputs": [], "actual_outputs": [{"sku": "X", "name": "X", "quantity": 1}],
                 "wip_issued": "500", "wip_account_code": "1130-WIP", "priority": "high"}}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text

    run = (await client.get(f"/manufacturing/{entity_id}", headers=headers)).json()
    assert run["status"] == "planned" and run["received_qty"] == 0.0 and run["received_lots"] == []
    assert run["actual_outputs"] == [] and run["priority"] == "high"
    assert run["inputs"] == [{"item_id": "item:a", "quantity": 3.0, "issued_qty": 0.0}]
    assert "wip_issued" not in run and "wip_account_code" not in run


@pytest.mark.asyncio
async def test_a_component_listed_twice_becomes_one_requirement(client, auth):
    headers = auth["headers"]
    r = await client.post("/items", headers=headers, json={
        "sku": "DUP-RAW", "name": "Raw", "quantity": 5, "sell_by": "piece", "cost_price": 2.0})
    item_id = r.json()["id"]
    r = await client.post("/manufacturing", headers=headers, json={
        "description": "Dup", "inputs": [{"item_id": item_id, "quantity": 2}, {"item_id": item_id, "quantity": 1.5}],
        "expected_outputs": [{"sku": "DUP-FG", "name": "Out", "quantity": 1}]})
    assert r.status_code == 200, r.text
    run = (await client.get(f"/manufacturing/{r.json()['id']}", headers=headers)).json()
    assert run["inputs"] == [{"item_id": item_id, "quantity": 3.5, "issued_qty": 0.0}]
