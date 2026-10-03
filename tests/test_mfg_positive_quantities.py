# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production material quantities are positive wherever new data is written.

A run or recipe that uses nothing (a component at zero) or gives material back (a negative
component) would make finished stock out of nothing, and an output of zero per batch has no
meaning; each is refused with a message saying why. History written before this rule still
rebuilds, and an older recipe that breaks it never stops a component's price from being edited.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.projections.engine import ProjectionEngine
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (fixtures)
from mfg_runs import product

pytestmark = pytest.mark.asyncio


async def _runs(session, auth) -> int:
    session.expire_all()
    return len((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "mfg_order"))).all())


def _says_positive(r) -> None:
    assert r.status_code == 422, r.text
    assert "greater than zero" in r.text or "greater than 0" in r.text, r.text


@pytest.mark.parametrize("qty", [0, -1])
async def test_direct_run_component_must_be_positive(client, session, auth, qty):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")

    r = await client.post("/manufacturing", headers=auth["headers"], json={
        "description": "Direct", "output_item_id": made, "quantity": 1,
        "inputs": [{"item_id": raw, "quantity": qty}]})

    _says_positive(r)
    assert await _runs(session, auth) == 0


async def test_issue_and_return_quantities_must_be_positive(client, session, auth):
    """Issuing or returning nothing, or a negative amount, of a component is refused."""
    raw = await _item(client, auth, 100.0, qty=10)
    other = await _item(client, auth, 10.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")

    r = await client.post("/manufacturing", headers=auth["headers"], json={
        "description": "Direct", "output_item_id": made, "quantity": 1,
        "inputs": [{"item_id": raw, "quantity": 2}, {"item_id": other, "quantity": 1}]})
    assert r.status_code == 200, r.text
    run = r.json()["id"]

    for body in ({"items": [{"item_id": raw, "quantity": 0}]}, {"items": [{"item_id": raw, "quantity": -1}]}):
        _says_positive(await client.post(f"/manufacturing/{run}/issue", headers=auth["headers"], json=body))
        _says_positive(await client.post(f"/manufacturing/{run}/return", headers=auth["headers"], json=body))


@pytest.mark.parametrize("qty", [0, -1])
async def test_recipe_component_must_be_positive(client, session, auth, qty):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")

    r = await client.put(f"/manufacturing/items/{made}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": raw, "quantity": qty}], "labor": [], "overhead": []})

    _says_positive(r)
    assert not (await _state(session, auth, made)).get("recipe")


@pytest.mark.parametrize("qty", [0, -1])
async def test_recipe_output_qty_must_be_positive(client, session, auth, qty):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")

    r = await client.put(f"/manufacturing/items/{made}/recipe", headers=auth["headers"], json={
        "output_qty": qty, "components": [{"item_id": raw, "quantity": 1}], "labor": [], "overhead": []})

    _says_positive(r)
    assert not (await _state(session, auth, made)).get("recipe")


async def test_an_older_invalid_recipe_still_rebuilds_and_never_blocks_a_price_edit(client, session, auth):
    """A recipe an older release stored with a zero output and a zero component still rebuilds
    as it was written, makes no run, and a later price edit of its component goes through."""
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 1)])
    legacy = {"output_qty": 0, "components": [{"item_id": raw, "quantity": 0}], "labor": [], "overhead": []}
    # Recorded as the older release wrote it, without today's check on new recipes.
    entry = LedgerEntry(company_id=auth["company_id"], entity_id=made, entity_type="item",
                        event_type="item.recipe.set", data={"recipe": legacy}, actor_id=auth["user_id"],
                        location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
    session.add(entry)
    await session.flush()
    await ProjectionEngine.apply_event(session, entry)
    await session.commit()

    r = await client.post(f"/manufacturing/items/{made}/build", headers=auth["headers"], json={"quantity": 1})
    assert r.status_code == 422, r.text
    r = await client.patch(f"/items/{raw}", headers=auth["headers"], json={"cost_price": 12})
    assert r.status_code == 200, r.text
    # The pricing page re-costs every product using the component next; the older recipe is
    # left as it was rather than failing the re-cost of the others.
    r = await client.post(f"/manufacturing/items/{raw}/recost-dependents", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert made not in r.json()["recosted"]
    assert (await client.get("/manufacturing/to-make", headers=auth["headers"])).status_code == 200
    rows = (await session.execute(select(LedgerEntry.event_type).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == made))).scalars().all()
    assert "item.recipe.set" in rows
    assert (await _state(session, auth, made))["recipe"]["output_qty"] == 0
