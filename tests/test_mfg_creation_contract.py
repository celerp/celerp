# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What a production run is made of, and what it makes.

A run turns stock into stock: its product and every component it issues are stocked items
or components, never a service, a non-stocked item or a freight charge (those belong in a
recipe's labor and overhead). A run makes exactly one product, named by the item itself;
what it expects to receive is that product. Every door that creates a run (a run created
directly, a build, Demand Planning, an import) is held to the same contract, and a run an
older release left that breaks it is refused when it moves.

A run's request key identifies one request: sending it again returns the run it made, and
sending it with anything different is refused.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from mfg_runs import complete, issue, product, receive, refusal
from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_helpers import company_auth

pytestmark = pytest.mark.asyncio

NOT_STOCK = ("service", "non_stocked", "freight")


async def _typed(client, auth, inventory_type: str, cost: float = 0.0, qty: float = 0) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"T-{uuid.uuid4().hex[:6]}", "name": "Part", "quantity": qty, "sell_by": "piece",
        "status": "available", "inventory_type": inventory_type, "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _recipe(client, auth, item: str, components: list[tuple[str, float]]):
    return await client.put(f"/manufacturing/items/{item}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": c, "quantity": q} for c, q in components],
        "labor": [], "overhead": []})


async def _create(client, auth, inputs: list[tuple[str, float]], output: str | None, qty: float = 1,
                  key: str | None = None, **extra):
    body = {"description": "Run", "inputs": [{"item_id": i, "quantity": q} for i, q in inputs], **extra}
    if output is not None:
        body.update(output_item_id=output, quantity=qty)
    if key:
        body["idempotency_key"] = key
    return await client.post("/manufacturing", headers=auth["headers"], json=body)


async def _build(client, auth, item: str, qty: float, key: str, complete_it: bool = False):
    return await client.post(f"/manufacturing/items/{item}/build", headers=auth["headers"],
                             json={"quantity": qty, "idempotency_key": key, "complete": complete_it})


async def _count(session, auth, *, entity_type: str | None = None, event_type: str | None = None) -> int:
    session.expire_all()
    if entity_type:
        return await session.scalar(select(func.count()).select_from(Projection).where(
            Projection.company_id == auth["company_id"], Projection.entity_type == entity_type))
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.event_type == event_type))


async def _historical_run(session, auth, inputs: list[tuple[str, float]], output: str) -> str:
    """A run as an older release wrote it, outside today's creation contract."""
    order = f"mfg:{uuid.uuid4()}"
    await emit_event(session, company_id=auth["company_id"], entity_id=order, entity_type="mfg_order",
                     event_type="mfg.order.created", data={
                         "description": "Older run", "order_type": "assembly", "output_item_id": output,
                         "inputs": [{"item_id": i, "quantity": q} for i, q in inputs],
                         "expected_outputs": [{"sku": "OLD", "name": "Old", "quantity": 1}]},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    return order


# ---------------------------------------------------------------------------
# B1: a run turns stock into stock
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", NOT_STOCK)
async def test_a_recipe_refuses_a_product_that_is_not_stock(client, session, auth, kind):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _typed(client, auth, kind)
    refusal(await _recipe(client, auth, made, [(raw, 1)]), 422, "not_stock")
    assert not (await _state(session, auth, made)).get("recipe")


@pytest.mark.parametrize("kind", NOT_STOCK)
async def test_a_recipe_refuses_a_component_that_is_not_stock(client, session, auth, kind):
    part = await _typed(client, auth, kind, cost=50.0, qty=5)
    made = await _item(client, auth, 0.0, qty=0)
    refusal(await _recipe(client, auth, made, [(part, 1)]), 422, "not_stock")
    assert not (await _state(session, auth, made)).get("recipe")


@pytest.mark.parametrize("kind", NOT_STOCK)
@pytest.mark.parametrize("side", ["product", "component"])
async def test_a_run_refuses_a_product_or_component_that_is_not_stock(client, session, auth, kind, side):
    odd = await _typed(client, auth, kind, cost=50.0, qty=5)
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0)
    r = await (_create(client, auth, [(raw, 1)], odd) if side == "product" else
               _create(client, auth, [(raw, 1), (odd, 1)], made))
    refusal(r, 422, "not_stock")
    assert await _count(session, auth, entity_type="mfg_order") == 0


async def test_a_historical_recipe_on_a_service_cannot_be_built(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _typed(client, auth, "service")
    await emit_event(session, company_id=auth["company_id"], entity_id=made, entity_type="item",
                     event_type="item.recipe.set", data={"recipe": {
                         "output_qty": 1, "components": [{"item_id": raw, "quantity": 1}], "labor": [],
                         "overhead": []}},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()

    refusal(await _build(client, auth, made, 1, "b", complete_it=True), 422, "not_stock")

    assert await _count(session, auth, entity_type="mfg_order") == 0
    assert await _count(session, auth, event_type="item.consumed") == 0


async def test_a_historical_run_cannot_receive_into_or_issue_what_is_not_stock(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    service = await _typed(client, auth, "service", cost=50.0, qty=5)
    order = await _historical_run(session, auth, [(raw, 2)], service)
    refusal(await issue(client, auth, order, key="i"), 409, "not_stock")
    assert (await _state(session, auth, raw))["quantity"] == 10
    refusal(await receive(client, auth, order, 1, key="r"), 409, "not_stock")
    refusal(await complete(client, auth, order, key="c"), 409, "not_stock")
    await assert_settled(client, session, auth)

    made = await _item(client, auth, 0.0, qty=0)
    other = await _historical_run(session, auth, [(service, 1)], made)
    refusal(await issue(client, auth, other, key="i"), 409, "not_stock")
    assert (await _state(session, auth, service))["quantity"] == 5


async def test_stocked_and_component_items_build_and_the_books_carry_the_stock(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    part = await _typed(client, auth, "component", cost=60.0, qty=6)
    made = await _typed(client, auth, "component")
    assert (await _recipe(client, auth, made, [(raw, 2), (part, 1)])).status_code == 200

    r = await _build(client, auth, made, 2, "b", complete_it=True)

    assert r.status_code == 200, r.text
    run = await _state(session, auth, r.json()["id"])
    assert run["status"] == "completed"
    lot = run["receipts"][0]["lot_item_id"]
    assert (await _state(session, auth, lot))["cost_total"] == 60.0
    await assert_settled(client, session, auth)


# ---------------------------------------------------------------------------
# B2: a run makes one product, named by the item
# ---------------------------------------------------------------------------

async def _direct_run_completes(client, session, auth, cost: float) -> None:
    raw = await _item(client, auth, cost, qty=10)
    made = await _item(client, auth, 0.0, qty=0)
    r = await _create(client, auth, [(raw, 4)], made, qty=2)
    assert r.status_code == 200, r.text
    order = r.json()["id"]
    assert (await issue(client, auth, order, key="i")).status_code == 200
    await assert_settled(client, session, auth)
    r = await receive(client, auth, order, 1, key="r1")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["cost_total"] == cost * 0.2
    r = await complete(client, auth, order, key="c")
    assert r.status_code == 200, r.text
    run = await _state(session, auth, order)
    assert run["status"] == "completed" and run["received_qty"] == 2
    assert run["actual_outputs"] == [{**run["expected_outputs"][0], "quantity": 2.0}]
    await assert_settled(client, session, auth)


async def test_a_valued_run_created_directly_receives_its_product_and_completes(client, session, auth):
    await _direct_run_completes(client, session, auth, 100.0)


async def test_a_zero_valued_run_created_directly_receives_its_product_and_completes(client, session, auth):
    await _direct_run_completes(client, session, auth, 0.0)


async def test_a_run_must_name_its_product(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    r = await _create(client, auth, [(raw, 1)], None)
    assert r.status_code == 422, r.text
    assert await _count(session, auth, entity_type="mfg_order") == 0


async def test_a_run_refuses_a_product_from_another_company_or_not_an_item(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    other = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    foreign = await _item(client, other, 0.0, qty=0)
    made = await _item(client, auth, 0.0, qty=0)
    first = (await _create(client, auth, [(raw, 1)], made)).json()["id"]
    for wrong in (foreign, first, f"item:{uuid.uuid4()}"):
        r = await _create(client, auth, [(raw, 1)], wrong)
        assert r.status_code == 422, r.text
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_run_refuses_listed_outputs(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0)
    two = [{"sku": "A", "name": "A", "quantity": 1}, {"sku": "B", "name": "B", "quantity": 1}]
    assert (await _create(client, auth, [(raw, 1)], None, expected_outputs=two)).status_code == 422
    assert (await _create(client, auth, [(raw, 1)], made, expected_outputs=two)).status_code == 422
    assert (await _create(client, auth, [(raw, 1)], made, expected_outputs=two[:1])).status_code == 422
    assert await _count(session, auth, entity_type="mfg_order") == 0


async def test_a_run_takes_its_output_from_its_product(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0)
    r = await _create(client, auth, [(raw, 1)], made, qty=3)
    assert r.status_code == 200, r.text
    product_state = await _state(session, auth, made)
    run = await _state(session, auth, r.json()["id"])
    assert run["output_item_id"] == made
    assert run["expected_outputs"] == [{"sku": product_state["sku"], "name": product_state["name"],
                                        "quantity": 3.0, "category": product_state.get("category")}]


async def test_an_import_names_one_product_like_every_other_door(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0)
    base = {"description": "Imported", "inputs": [{"item_id": raw, "quantity": 1}]}
    records = [
        {"entity_id": "mfg:imp-ok", "data": {**base, "output_item_id": made, "quantity": 2}},
        {"entity_id": "mfg:imp-none", "data": base},
        {"entity_id": "mfg:imp-list", "data": {**base, "output_item_id": made, "quantity": 2, "expected_outputs": [
            {"sku": "A", "name": "A", "quantity": 1}, {"sku": "B", "name": "B", "quantity": 1}]}},
    ]
    r = await client.post("/manufacturing/import/batch", headers=auth["headers"], json={"records": [
        {**rec, "event_type": "mfg.order.created", "source": "import", "idempotency_key": rec["entity_id"]}
        for rec in records]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and len(r.json()["errors"]) == 2, r.json()
    run = await _state(session, auth, "mfg:imp-ok")
    assert run["expected_outputs"][0]["quantity"] == 2.0 and run["output_item_id"] == made
    assert await _count(session, auth, entity_type="mfg_order") == 1


# ---------------------------------------------------------------------------
# B5: a request key identifies one request
# ---------------------------------------------------------------------------

async def test_retrying_a_run_returns_the_run_first_created(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await _item(client, auth, 0.0, qty=0)
    first = await _create(client, auth, [(raw, 2)], made, qty=1, key="k")
    again = await _create(client, auth, [(raw, 2)], made, qty=1, key="k")
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert again.json()["id"] == first.json()["id"]
    assert await _state(session, auth, first.json()["id"])
    assert await _count(session, auth, entity_type="mfg_order") == 1


@pytest.mark.parametrize("change", ["component", "quantity", "output", "output_quantity"])
async def test_a_run_key_reused_for_a_different_run_is_refused(client, session, auth, change):
    raw, other = await _item(client, auth, 100.0, qty=10), await _item(client, auth, 10.0, qty=10)
    made, made2 = await _item(client, auth, 0.0, qty=0), await _item(client, auth, 0.0, qty=0)
    assert (await _create(client, auth, [(raw, 2)], made, qty=1, key="k")).status_code == 200
    inputs, output, qty = {"component": ([(other, 2)], made, 1), "quantity": ([(raw, 3)], made, 1),
                           "output": ([(raw, 2)], made2, 1), "output_quantity": ([(raw, 2)], made, 5)}[change]
    refusal(await _create(client, auth, inputs, output, qty=qty, key="k"), 409, "key_reused")
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_retrying_a_build_returns_the_same_run(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 1)])
    first, again = await _build(client, auth, made, 2, "b"), await _build(client, auth, made, 2, "b")
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert again.json()["id"] == first.json()["id"]
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_build_key_reused_for_another_product_is_refused(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made, made2 = await product(client, auth, [(raw, 1)]), await product(client, auth, [(raw, 2)])
    assert (await _build(client, auth, made, 1, "b")).status_code == 200
    refusal(await _build(client, auth, made2, 1, "b"), 409, "key_reused")
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_build_key_reused_for_another_quantity_is_refused(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 1)])
    assert (await _build(client, auth, made, 1, "b")).status_code == 200
    refusal(await _build(client, auth, made, 3, "b"), 409, "key_reused")
    refusal(await _build(client, auth, made, 1, "b", complete_it=True), 409, "key_reused")
    assert await _count(session, auth, entity_type="mfg_order") == 1
    assert await _count(session, auth, event_type="item.consumed") == 0


async def test_a_build_retried_after_a_lost_answer_makes_one_run_and_moves_stock_once(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 1)])
    first = await _build(client, auth, made, 2, "b", complete_it=True)
    assert first.status_code == 200, first.text
    counts = {t: await _count(session, auth, event_type=t)
              for t in ("item.consumed", "item.created", "mfg.order.completed")}
    entries = await _count(session, auth, entity_type="journal_entry")

    again = await _build(client, auth, made, 2, "b", complete_it=True)

    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert await _count(session, auth, entity_type="mfg_order") == 1
    assert {t: await _count(session, auth, event_type=t) for t in counts} == counts
    assert await _count(session, auth, entity_type="journal_entry") == entries
    assert (await _state(session, auth, raw))["quantity"] == 8
    await assert_settled(client, session, auth)
