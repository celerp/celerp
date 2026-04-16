# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _u() -> str:
    return str(uuid.uuid4())


async def test_bom_create_then_delete(journey_api):
    bom = await journey_api.post(
        "/manufacturing/boms",
        json={
            "name": f"ITest BOM {_u()[:8]}",
            "output_qty": 1,
            "components": [],
        },
    )
    assert bom.status_code == 200, bom.text
    bom_id = bom.json()["bom_id"]

    deleted = await journey_api.delete(f"/manufacturing/boms/{bom_id}")
    assert deleted.status_code == 200, deleted.text


async def test_manufacturing_order_start_consume_step_complete(journey_api):
    order = await journey_api.post(
        "/manufacturing",
        json={
            "description": f"ITest Order {_u()[:8]}",
            "inputs": [{"item_id": "item:gc:472043", "quantity": 1}],
            "expected_outputs": [{"sku": f"OUT-{_u()[:6]}", "name": "Output", "quantity": 1}],
            "idempotency_key": _u(),
        },
    )
    assert order.status_code == 200, order.text
    order_id = order.json()["id"]

    started = await journey_api.post(f"/manufacturing/{order_id}/start")
    assert started.status_code == 200, started.text

    consumed = await journey_api.post(
        f"/manufacturing/{order_id}/consume",
        json={"item_id": "item:gc:472043", "quantity": 0, "idempotency_key": _u()},
    )
    assert consumed.status_code == 200, consumed.text

    step = await journey_api.post(
        f"/manufacturing/{order_id}/step",
        json={"step_id": "assembly", "notes": "done", "idempotency_key": _u()},
    )
    assert step.status_code == 200, step.text

    completed = await journey_api.post(
        f"/manufacturing/{order_id}/complete",
        json={"idempotency_key": _u()},
    )
    assert completed.status_code == 200, completed.text

    got = await journey_api.get(f"/manufacturing/{order_id}")
    assert got.status_code == 200, got.text
    assert got.json()["status"] == "completed"


async def test_manufacturing_order_cancel(journey_api):
    order = await journey_api.post(
        "/manufacturing",
        json={
            "description": f"ITest Cancel Order {_u()[:8]}",
            "inputs": [{"item_id": "item:gc:472043", "quantity": 1}],
            "expected_outputs": [{"sku": f"OUT-{_u()[:6]}", "name": "Output", "quantity": 1}],
            "idempotency_key": _u(),
        },
    )
    assert order.status_code == 200, order.text
    order_id = order.json()["id"]

    cancel = await journey_api.post(
        f"/manufacturing/{order_id}/cancel",
        json={"reason": "No longer needed", "idempotency_key": _u()},
    )
    assert cancel.status_code == 200, cancel.text

    got = await journey_api.get(f"/manufacturing/{order_id}")
    assert got.status_code == 200, got.text
    assert got.json()["status"] == "cancelled"
