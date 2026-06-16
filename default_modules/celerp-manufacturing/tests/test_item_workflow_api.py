# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""API tests for PUT /manufacturing/items/{id}/workflow."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@workflow.test"
    r = await client.post("/auth/register", json={"company_name": "Workflow Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku="FG") -> str:
    r = await client.post("/items", headers=_h(token), json={"sku": sku, "name": sku, "quantity": 0, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _step(**kw):
    base = {"name": "", "instructions": "", "station": "", "time_value": 0, "time_unit": "min", "wait": False}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_put_workflow_round_trip_assigns_id_and_normalizes_minutes(client) -> None:
    token = await _register(client)
    fg = await _item(client, token)

    body = {"steps": [
        _step(name="Cut", instructions="Cut & anneal", station="Bench 1", time_value=15, time_unit="min"),
        _step(name="Cast", instructions="Cool overnight", station="Casting", time_value=12, time_unit="hr", wait=True),
        _step(name="Cure", time_value=2, time_unit="day"),
    ]}
    r = await client.put(f"/manufacturing/items/{fg}/workflow", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    steps = r.json()["workflow"]["steps"]

    # Canonical minutes derived server-side from (time_value, time_unit).
    assert [s["time_minutes"] for s in steps] == [15.0, 720.0, 2880.0]
    # A stable id is assigned to every step.
    assert all(s["id"] for s in steps)
    assert len({s["id"] for s in steps}) == 3
    # The wait flag round-trips.
    assert steps[1]["wait"] is True

    got = (await client.get(f"/items/{fg}", headers=_h(token))).json()
    assert got["workflow"]["steps"][2]["time_minutes"] == 2880.0
    assert got["workflow"]["steps"][2]["time_value"] == 2.0
    assert got["workflow"]["steps"][2]["time_unit"] == "day"


@pytest.mark.asyncio
async def test_put_workflow_preserves_supplied_id_and_full_replaces(client) -> None:
    token = await _register(client)
    fg = await _item(client, token)

    first = {"steps": [_step(id="keep-me", name="One"), _step(name="Two")]}
    r = await client.put(f"/manufacturing/items/{fg}/workflow", headers=_h(token), json=first)
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["steps"][0]["id"] == "keep-me"

    second = {"steps": [_step(name="Only")]}
    r = await client.put(f"/manufacturing/items/{fg}/workflow", headers=_h(token), json=second)
    got = (await client.get(f"/items/{fg}", headers=_h(token))).json()
    # Full-replace, not merge.
    assert [s["name"] for s in got["workflow"]["steps"]] == ["Only"]


@pytest.mark.asyncio
async def test_put_workflow_rejects_bad_time_unit(client) -> None:
    token = await _register(client)
    fg = await _item(client, token)
    r = await client.put(f"/manufacturing/items/{fg}/workflow", headers=_h(token),
                         json={"steps": [_step(name="X", time_value=1, time_unit="weeks")]})
    assert r.status_code == 422
    assert "time unit" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_put_workflow_rejects_unknown_ref_file(client) -> None:
    token = await _register(client)
    fg = await _item(client, token)
    r = await client.put(f"/manufacturing/items/{fg}/workflow", headers=_h(token),
                         json={"steps": [_step(name="X", ref_file_id="nope")]})
    assert r.status_code == 422
    assert "reference file" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_put_workflow_unknown_item_404(client) -> None:
    token = await _register(client)
    r = await client.put("/manufacturing/items/item:does-not-exist/workflow", headers=_h(token),
                         json={"steps": []})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_workflow_emits_event(client, session) -> None:
    token = await _register(client)
    fg = await _item(client, token)
    await client.put(f"/manufacturing/items/{fg}/workflow", headers=_h(token),
                     json={"steps": [_step(name="Step")]})
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id == fg,
                                  LedgerEntry.event_type == "item.workflow.set")
    )).scalars().all()
    assert len(rows) == 1
