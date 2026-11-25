# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A production run that makes more than one thing: a main product plus extras, such as oil
and the press cake left over from pressing seed.

A recipe lists its extras under ``also_produces``, each with a quantity per batch and a cost
share: the percentage of the run's value it carries. The main product carries the rest. A run
snapshots them when it is created, scaled to its size; completing it receives each extra into
its own lot (the shown quantity, or what is entered), and closing splits the run's value over
the outputs by share, then within each output by quantity.
"""
from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from mfg_runs import cancel, complete, give_back, issue, receive, refusal, reopen, undo_receipt
from stock_books import assert_settled
from test_cost_restatement import _item, _merge, _sell, _state

pytestmark = pytest.mark.asyncio

MFG = "/manufacturing"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

async def put_recipe(client, auth, product: str, components: list[tuple[str, float]], extras: list[dict] | None,
                     output_qty: float = 1):
    body = {"output_qty": output_qty, "components": [{"item_id": c, "quantity": q} for c, q in components],
            "labor": [], "overhead": []}
    if extras is not None:
        body["also_produces"] = extras
    return await client.put(f"{MFG}/items/{product}/recipe", headers=auth["headers"], json=body)


async def press(client, auth, share: float = 20.0, cake_qty: float = 2.0) -> tuple[str, str, str]:
    """Seed (1000 at 2.00 each), the oil pressed from it (3 seed a litre) and the cake left
    over (``cake_qty`` a litre, carrying ``share`` percent of the value)."""
    tag = uuid.uuid4().hex[:6]
    seed = await _item(client, auth, 2000.0, qty=1000, sku=f"SEED-{tag}")
    cake = await _item(client, auth, 0.0, qty=0, sku=f"CAKE-{tag}")
    oil = await _item(client, auth, 0.0, qty=0, sku=f"OIL-{tag}")
    r = await put_recipe(client, auth, oil, [(seed, 3)], [{"item_id": cake, "quantity": cake_qty, "cost_share": share}])
    assert r.status_code == 200, r.text
    return seed, oil, cake


async def start(client, auth, product: str, qty: float, **body) -> str:
    r = await client.post(f"{MFG}/items/{product}/build", headers=auth["headers"], json={"quantity": qty, **body})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def create(client, auth, product: str, qty: float, **extra):
    return await client.post(MFG, headers=auth["headers"], json={
        "description": "press", "output_item_id": product, "quantity": qty, **extra})


async def lots_of(session, auth, order: str) -> dict[str, list[dict]]:
    """The run's live lots by the item they are a lot of: [{lot, quantity, cost}]."""
    run = await _state(session, auth, order)
    out: dict[str, list[dict]] = {}
    for r in run.get("receipts") or []:
        lot = await _state(session, auth, r["lot_item_id"])
        out.setdefault(lot["parent_item_id"], []).append(
            {"lot": r["lot_item_id"], "quantity": float(lot.get("quantity") or 0),
             "cost": float(lot.get("cost_total") or 0)})
    return out


async def qty_of(session, auth, item: str) -> float:
    return float((await _state(session, auth, item)).get("quantity") or 0)


def cents(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def largest_remainder(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Oracle: ``amount`` in cents over ``weights``, floors first, then a cent each to the
    largest remainders, the earlier part first on a tie."""
    units = int(cents(amount) * 100)
    total = sum(weights)
    exact = [Decimal(units) * w / total for w in weights]
    whole = [int(x) for x in exact]
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - whole[i]), i))
    for i in order[:units - sum(whole)]:
        whole[i] += 1
    return [Decimal(n) / 100 for n in whole]


def _key(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("message_key") if isinstance(detail, dict) else None


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------

async def test_recipe_extras_costed(client, session, auth):
    """The recipe stores its extras with the cost split: the cake's unit cost on its own line,
    the oil's standard cost the rest. A recipe with none stores no ``also_produces``."""
    seed, oil, cake = await press(client, auth)
    recipe = (await _state(session, auth, oil))["recipe"]
    [line] = recipe["also_produces"]
    assert line["item_id"] == cake and line["quantity"] == 2 and line["cost_share"] == 20
    assert line["sku"] == (await _state(session, auth, cake))["sku"]
    # 3 seed at 2.00 = 6.00 a litre: 80% to the litre of oil, 20% to its 2 of cake.
    assert recipe["unit_cost"] == pytest.approx(4.8)
    assert line["unit_cost"] == pytest.approx(0.6)

    # A client-sent unit cost is never trusted.
    r = await put_recipe(client, auth, oil, [(seed, 3)],
                         [{"item_id": cake, "quantity": 2, "cost_share": 20, "unit_cost": 99}])
    assert r.status_code == 200, r.text
    assert r.json()["recipe"]["also_produces"][0]["unit_cost"] == pytest.approx(0.6)

    # [] means none: the recipe event carries no also_produces, as before the feature.
    r = await put_recipe(client, auth, oil, [(seed, 3)], [])
    assert r.status_code == 200, r.text
    session.expire_all()
    last = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == oil,
        LedgerEntry.event_type == "item.recipe.set").order_by(LedgerEntry.id.desc()).limit(1))).scalar_one()
    assert "also_produces" not in last.data["recipe"]
    assert (await _state(session, auth, oil))["recipe"]["unit_cost"] == pytest.approx(6.0)


SHARE_CASES = [
    # (lines as (which, share), accepted, refusal key)
    ([("a", 0)], True, None),
    ([("a", 10), ("b", 20)], True, None),
    ([("a", 99.99)], True, None),
    ([("a", 100)], False, "manufacturing.err_cost_shares"),
    ([("a", 60), ("b", 40)], False, "manufacturing.err_cost_shares"),
    ([("a", -1)], False, "manufacturing.err_cost_shares"),
    ([("a", "nan")], False, None),
    ([("a", "inf")], False, None),
    ([("a", "abc")], False, None),
    ([("product", 10)], False, "manufacturing.err_coproduct_in_recipe"),
    ([("seed", 10)], False, "manufacturing.err_coproduct_in_recipe"),
    ([("a", 10), ("a", 5)], False, "manufacturing.err_coproduct_duplicate"),
    ([("deleted", 10)], False, "mfg.component_deleted"),
    ([("merged", 10)], False, "mfg.component_merged"),
    ([("service", 10)], False, "mfg.not_stock"),
    ([("missing", 10)], False, None),
]


@pytest.mark.parametrize("lines,accepted,key", SHARE_CASES)
async def test_cost_share_oracle(client, session, auth, lines, accepted, key):
    """Shares and extra items, against a Decimal oracle of the split: each share at least 0
    and under 100, their total under 100; the extra a stocked live item that is neither the
    product nor a component, listed once."""
    tag = uuid.uuid4().hex[:6]
    seed = await _item(client, auth, 2000.0, qty=1000, sku=f"S-{tag}")
    oil = await _item(client, auth, 0.0, qty=0, sku=f"O-{tag}")
    items = {"seed": seed, "product": oil, "missing": f"item:{uuid.uuid4()}"}
    for name in ("a", "b", "deleted", "service"):
        items[name] = await _item(client, auth, 0.0, qty=0, sku=f"{name}-{tag}")
    r = await client.post("/items/bulk/delete", headers=auth["headers"], json={"entity_ids": [items["deleted"]]})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/items/{items['service']}", headers=auth["headers"],
                           json={"fields_changed": {"inventory_type": {"old": None, "new": "service"}}})
    assert r.status_code == 200, r.text
    one = await _item(client, auth, 0.0, qty=1, sku=f"m1-{tag}")
    two = await _item(client, auth, 0.0, qty=1, sku=f"m2-{tag}")
    await _merge(client, auth, [two, one])
    items["merged"] = one
    shares = [s if not isinstance(s, str) else {"nan": math.nan, "inf": math.inf}.get(s, s) for _, s in lines]
    extras = [{"item_id": items[w], "quantity": 2, "cost_share": s} for (w, _), s in zip(lines, shares)]
    body = {"output_qty": 1, "components": [{"item_id": seed, "quantity": 3}], "labor": [], "overhead": [],
            "also_produces": extras}
    import json as _json
    r = await client.put(f"{MFG}/items/{oil}/recipe", headers={**auth["headers"], "content-type": "application/json"},
                         content=_json.dumps(body, allow_nan=True))
    if not accepted:
        assert r.status_code == 422, r.text
        if key:
            assert _key(r) == key, r.text
        if key == "manufacturing.err_cost_shares" and len(lines) > 1:
            assert "100" in r.json()["detail"]["message"], r.text
        assert "also_produces" not in ((await _state(session, auth, oil)).get("recipe") or {})
        return
    assert r.status_code == 200, r.text
    recipe = r.json()["recipe"]
    total = Decimal(6)  # 3 seed at 2.00
    sum_share = sum(Decimal(str(s)) for s in shares)
    assert Decimal(str(recipe["unit_cost"])) == pytest.approx(total * (100 - sum_share) / 100, abs=Decimal("0.0001"))
    for line, s in zip(recipe["also_produces"], shares):
        assert Decimal(str(line["unit_cost"])) == pytest.approx(total * Decimal(str(s)) / 100 / 2,
                                                                abs=Decimal("0.0001"))


# ---------------------------------------------------------------------------
# Run creation
# ---------------------------------------------------------------------------

async def test_run_scales_extras(client, session, auth):
    """A run snapshots the recipe's extras scaled to its size; a request may list its own,
    [] for none; bad lines are refused; import takes them, never expected_outputs."""
    seed, oil, cake = await press(client, auth, cake_qty=0.3333333)
    r = await create(client, auth, oil, 10)
    assert r.status_code == 200, r.text
    run = await _state(session, auth, r.json()["id"])
    [line] = run["also_produces"]
    assert line["item_id"] == cake and line["cost_share"] == 20
    assert line["quantity"] == pytest.approx(3.333333)
    assert {"sku", "name", "unit"} <= set(line)

    r = await create(client, auth, oil, 10, also_produces=[])
    assert r.status_code == 200, r.text
    assert "also_produces" not in await _state(session, auth, r.json()["id"])

    r = await create(client, auth, oil, 4, also_produces=[{"item_id": cake, "quantity": 7, "cost_share": 10}])
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["id"]))["also_produces"][0]["quantity"] == 7

    for bad in ([{"item_id": cake, "quantity": "nan", "cost_share": 1}],
                [{"item_id": cake, "quantity": "inf", "cost_share": 1}],
                ["cake"], [{"item_id": cake, "quantity": 0, "cost_share": 1}],
                [{"item_id": cake, "quantity": 1, "cost_share": 1, "colour": "brown"}],
                [{"item_id": seed, "quantity": 1, "cost_share": 1}]):
        assert (await create(client, auth, oil, 1, also_produces=bad)).status_code == 422, bad

    def rec(data):
        return {"records": [{"entity_id": f"mfg:{uuid.uuid4()}", "event_type": "mfg.order.created",
                             "data": data, "idempotency_key": uuid.uuid4().hex}]}
    base = {"description": "imported", "output_item_id": oil, "quantity": 2}
    r = await client.post(f"{MFG}/import/batch", headers=auth["headers"], json=rec(
        {**base, "also_produces": [{"item_id": cake, "quantity": 5, "cost_share": 20}]}))
    assert r.status_code == 200 and r.json().get("created") == 1, r.text
    r = await client.post(f"{MFG}/import/batch", headers=auth["headers"], json=rec(
        {**base, "expected_outputs": [{"item_id": cake, "quantity": 5}]}))
    assert r.json().get("created") == 0, r.text


async def test_outputs_count_extras(client, session, auth):
    """An open run's extras still to come are supply for planning, and changing the unit of an
    item an open run will receive is refused, extras included."""
    seed, oil, cake = await press(client, auth)
    await start(client, auth, oil, 10)
    from celerp.services.record_quantities import outputs_to_receive  # new helper
    from celerp.models.projections import Projection
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "mfg_order"))).scalars().all()
    [run] = [r.state for r in rows if r.state.get("output_item_id") == oil]
    assert dict(outputs_to_receive(run)) == {oil: 10.0, cake: 20.0}
    r = await client.patch(f"/items/{cake}", headers=auth["headers"],
                           json={"fields_changed": {"sell_by": {"old": "piece", "new": "kg"}}})
    assert r.status_code == 409, r.text
    from celerp_manufacturing.routes import _in_progress_by_item
    supply = await _in_progress_by_item(session, auth["company_id"])
    assert supply.get(cake) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------

async def test_press_run_oil_and_cake(client, session, auth):
    """Pressing 10 litres: 30 seed out; 10 litres of oil and 20 of cake in, each its own lot;
    the oil carries 80% of the 60.00, the cake 20%; the books balance."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    r = await complete(client, auth, order, key="done")
    assert r.status_code == 200, r.text
    assert await qty_of(session, auth, seed) == 970
    lots = await lots_of(session, auth, order)
    assert lots[oil] == [{"lot": lots[oil][0]["lot"], "quantity": 10.0, "cost": 48.0}]
    assert lots[cake] == [{"lot": lots[cake][0]["lot"], "quantity": 20.0, "cost": 12.0}]
    run = await _state(session, auth, order)
    assert run["status"] == "completed" and run["received_qty"] == 10
    assert {o["item_id"]: o["quantity"] for o in run["actual_outputs"]} == {oil: 10.0, cake: 20.0}
    await assert_settled(client, session, auth)


async def test_extra_zero_share_back(client, session, auth, ids):
    """Cake at share 0 takes no value; a run that received only zero-share output and still
    holds value is refused; a run whose components were all wasted books cake at 0."""
    seed, oil, cake = await press(client, auth, share=0)
    order = await start(client, auth, oil, 10)
    assert (await complete(client, auth, order, key="a")).status_code == 200
    lots = await lots_of(session, auth, order)
    assert lots[oil][0]["cost"] == 60.0 and lots[cake][0]["cost"] == 0.0

    # Only the cake received: the 60.00 has nowhere to go.
    order = await start(client, auth, oil, 10)
    assert (await issue(client, auth, order)).status_code == 200
    from celerp_manufacturing import movements
    with pytest.raises(Exception) as exc:
        await movements.complete(session, auth["company_id"], ids["user_id"], order,
                                 {"also_produces": [{"item_id": cake, "quantity": 20}]}, "zero",
                                 at=datetime.now(timezone.utc).isoformat(), quantity=0.0)
    assert getattr(exc.value, "status_code", None) == 409
    assert exc.value.detail["message_key"] == "mfg.no_cost_share"
    await session.rollback()

    # Every component wasted, cake 5 entered: a lot of 5 at 0, no error.
    order = await start(client, auth, oil, 10)
    r = await complete(client, auth, order, key="w", waste_quantity=30, waste_reason="spoilt",
                       also_produces=[{"item_id": cake, "quantity": 5}])
    assert r.status_code == 200, r.text
    lots = await lots_of(session, auth, order)
    assert oil not in lots and lots[cake][0]["quantity"] == 5 and lots[cake][0]["cost"] == 0
    await assert_settled(client, session, auth)


async def test_extra_entry_rules(client, session, auth):
    """What Complete accepts for an extra: any amount from 0 (over the plan too), each item
    once after merging repeats; negative, unknown, nan, inf and non-object lines refused."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    for bad, status, key in (
            ([{"item_id": cake, "quantity": -1}], 422, "mfg.extra_quantity"),
            ([{"item_id": seed, "quantity": 1}], 422, "mfg.not_an_extra"),
            ([{"item_id": cake, "quantity": "nan"}], 422, None),
            ([{"item_id": cake, "quantity": "inf"}], 422, None),
            (["cake"], 422, None),
            ([{"item_id": cake, "quantity": 1, "extra": 1}], 422, None)):
        r = await complete(client, auth, order, key=f"bad-{uuid.uuid4().hex[:4]}", also_produces=bad)
        assert r.status_code == status, (bad, r.text)
        if key:
            assert _key(r) == key, r.text
    assert (await _state(session, auth, order))["status"] != "completed"

    # Repeats merge: 15 + 10 = 25, over the 20 planned, accepted.
    body = [{"item_id": cake, "quantity": 15}, {"item_id": cake, "quantity": 10}]
    r = await complete(client, auth, order, key="ok", also_produces=body)
    assert r.status_code == 200, r.text
    assert [x["quantity"] for x in (await lots_of(session, auth, order))[cake]] == [25.0]
    # The same request again (lines in another split) replays.
    r = await complete(client, auth, order, key="ok", also_produces=[{"item_id": cake, "quantity": 25}])
    assert r.status_code == 200, r.text

    # A partial entry.
    order = await start(client, auth, oil, 10)
    r = await complete(client, auth, order, key="p", also_produces=[{"item_id": cake, "quantity": 7}])
    assert r.status_code == 200, r.text
    assert [x["quantity"] for x in (await lots_of(session, auth, order))[cake]] == [7.0]


async def test_extra_key_reuse(client, session, auth):
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    assert (await complete(client, auth, order, key="k", also_produces=[{"item_id": cake, "quantity": 20}])).status_code == 200
    r = await complete(client, auth, order, key="k", also_produces=[{"item_id": cake, "quantity": 5}])
    refusal(r, 409, "key_reused")


@pytest.mark.parametrize("change", ["deleted", "merged", "not_stock"])
async def test_extra_item_changed(client, session, auth, change):
    """The cake deleted, merged or made a service after the run was created: completing with
    cake refuses and names it; entering 0 completes without it; finalizing a sale that
    auto-completes the run tells the user it did not complete."""
    seed, oil, cake = await press(client, auth)
    sku = (await _state(session, auth, cake))["sku"]
    order = await start(client, auth, oil, 10)
    if change == "deleted":
        r = await client.post("/items/bulk/delete", headers=auth["headers"], json={"entity_ids": [cake]})
    elif change == "merged":
        other = await _item(client, auth, 0.0, qty=1, sku=f"X-{uuid.uuid4().hex[:4]}")
        r = None
        await _merge(client, auth, [other, cake])
    else:
        r = await client.patch(f"/items/{cake}", headers=auth["headers"],
                               json={"fields_changed": {"inventory_type": {"old": None, "new": "service"}}})
    assert r is None or r.status_code == 200, r.text
    r = await complete(client, auth, order, key="c")
    assert r.status_code == 409, r.text
    assert sku in r.json()["detail"]["message"], r.text
    assert (await _state(session, auth, order))["status"] != "completed"
    r = await complete(client, auth, order, key="z", also_produces=[{"item_id": cake, "quantity": 0}])
    assert r.status_code == 200, r.text
    assert cake not in await lots_of(session, auth, order)

    # Finalize door: a sale of oil auto-completes a new run; the refused cake leaves a notice.
    r = await client.patch("/companies/me", headers=auth["headers"], json={"settings": {"manufacturing": {
        "auto_create_work_orders": True, "auto_complete_work_orders": True}}})
    assert r.status_code == 200, r.text
    osku = (await _state(session, auth, oil))["sku"]
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "total": 0, "line_items": [
        {"item_id": oil, "sku": osku, "name": osku, "quantity": 30, "unit_price": 10}]})
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    notes = (await client.get("/notifications", headers=auth["headers"])).json()
    text = str(notes)
    assert "not_completed" in text or sku in text, notes


async def test_receive_keeps_open(client, session, auth):
    """Receiving the last of the main product leaves a run with extras open: Complete is next."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    assert (await issue(client, auth, order)).status_code == 200
    r = await receive(client, auth, order, key="all")
    assert r.status_code == 200, r.text
    run = await _state(session, auth, order)
    assert run["status"] != "completed" and run["received_qty"] == 10
    assert (await complete(client, auth, order, key="c")).status_code == 200
    lots = await lots_of(session, auth, order)
    assert sum(x["cost"] for x in lots[oil]) == 48.0 and lots[cake][0]["cost"] == 12.0


async def test_recomplete_keeps_extras(client, session, auth):
    """What Complete received of an extra stays declared after a reopen: 0, 30 of a planned
    50, or 50; Completing again (API, AI or the unedited UI form) adds nothing; entering 20
    after 30 makes 50."""
    for first, again, total in ((0, None, 0), (30, None, 30), (50, None, 50), (30, 20, 50)):
        seed, oil, cake = await press(client, auth, cake_qty=5)
        order = await start(client, auth, oil, 10)
        r = await complete(client, auth, order, key="1", also_produces=[{"item_id": cake, "quantity": first}])
        assert r.status_code == 200, r.text
        assert (await reopen(client, auth, order, key="r")).status_code == 200
        run = await _state(session, auth, order)
        from celerp.services.record_quantities import extra_to_receive
        assert extra_to_receive(run, cake) == 0
        body = {} if again is None else {"also_produces": [{"item_id": cake, "quantity": again}]}
        r = await complete(client, auth, order, key="2", **body)
        assert r.status_code == 200, r.text
        got = sum(x["quantity"] for x in (await lots_of(session, auth, order)).get(cake, []))
        assert got == total, (first, again, got)
        # The unedited UI form posts [] (the prefill is 0): the same as no entry.
        assert (await reopen(client, auth, order, key="r2")).status_code == 200
        r = await complete(client, auth, order, key="3", also_produces=[])
        assert r.status_code == 200, r.text
        got = sum(x["quantity"] for x in (await lots_of(session, auth, order)).get(cake, []))
        assert got == total


async def test_doors_default_extras(client, session, auth):
    """Build-and-complete, make from a sale, finalize auto-complete and bulk complete all
    receive the extras shown on the run."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 2, complete=True)
    assert (await lots_of(session, auth, order))[cake][0]["quantity"] == 4

    order = await start(client, auth, oil, 3)
    r = await client.post(f"{MFG}/bulk-action", headers=auth["headers"], json={"run_ids": [order], "action": "complete"})
    assert r.status_code == 200, r.text
    assert (await lots_of(session, auth, order))[cake][0]["quantity"] == 6

    osku = (await _state(session, auth, oil))["sku"]

    async def invoice(qty):
        r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "total": 0, "line_items": [
            {"item_id": oil, "sku": osku, "name": osku, "quantity": qty, "unit_price": 10}]})
        doc = r.json()["id"]
        assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
        return doc

    doc = await invoice(4)
    r = await client.post(f"{MFG}/to-make/make", headers=auth["headers"],
                          json={"lines": [{"item_id": oil, "doc_id": doc}], "complete": True})
    assert r.status_code == 200, r.text
    made = r.json()["created"][0]["run_id"]
    assert (await lots_of(session, auth, made))[cake][0]["quantity"] == 8

    r = await client.patch("/companies/me", headers=auth["headers"], json={"settings": {"manufacturing": {
        "auto_create_work_orders": True, "auto_complete_work_orders": True}}})
    assert r.status_code == 200, r.text
    await invoice(5)
    runs = (await client.get(MFG, headers=auth["headers"])).json()["items"]
    auto = [o for o in runs if o.get("output_item_id") == oil and o.get("quantity") == 5]
    assert auto and (await lots_of(session, auth, auto[0]["id"]))[cake][0]["quantity"] == 10


# ---------------------------------------------------------------------------
# Undo, guards
# ---------------------------------------------------------------------------

async def test_undo_extra_receipt(client, session, auth):
    """Undoing a cake receipt keeps it apart from the main output: its event names the cake,
    and the run's received quantity of oil is unchanged."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    assert (await reopen(client, auth, order, key="r")).status_code == 200
    lot = (await lots_of(session, auth, order))[cake][0]["lot"]
    r = await undo_receipt(client, auth, order, lot, key="u")
    assert r.status_code == 200, r.text
    run = await _state(session, auth, order)
    assert run["received_qty"] == 10
    assert cake not in await lots_of(session, auth, order)
    session.expire_all()
    ev = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == order,
        LedgerEntry.event_type == "mfg.order.receipt_undone"))).scalar_one()
    assert ev.data["output_item_id"] == cake
    # Complete again books the cake back by the entry.
    r = await complete(client, auth, order, key="c2", also_produces=[{"item_id": cake, "quantity": 20}])
    assert r.status_code == 200, r.text
    assert (await lots_of(session, auth, order))[cake][0]["cost"] == 12.0
    await assert_settled(client, session, auth)


async def test_cancel_refused_extra_lot(client, session, auth):
    """A run holding only an extra's lot cannot be cancelled."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    assert (await reopen(client, auth, order, key="r")).status_code == 200
    oil_lot = (await lots_of(session, auth, order))[oil][0]["lot"]
    assert (await undo_receipt(client, auth, order, oil_lot, key="u")).status_code == 200
    assert (await give_back(client, auth, order, key="g")).status_code == 200
    refusal(await cancel(client, auth, order, key="x"), 409, "cancel_moved")


async def test_untracked_ignores_extras(client, session, auth):
    """Output the run cannot place in a lot counts the main product only: cake lots never make
    it negative."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    assert [x["quantity"] for x in (await lots_of(session, auth, order))[cake]] == [20.0]
    from celerp_manufacturing.movements import _untracked_qty
    assert _untracked_qty(await _state(session, auth, order)) == 0


async def test_search_extra_sku(client, session, auth):
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 1)
    sku = (await _state(session, auth, cake))["sku"]
    from celerp_manufacturing.search import _haystack
    assert sku.lower() in _haystack(await _state(session, auth, order)).lower()
    r = await client.get(MFG, headers=auth["headers"], params={"q": sku})
    assert order in [o["id"] for o in r.json()["items"]], r.text


async def test_extras_survive_disable(client, session, auth):
    """Turning manufacturing off keeps extra lots, values and history; a cake lot sold while
    off blocks Reopen until the sale is reversed."""
    seed, oil, cake = await press(client, auth)
    order = await start(client, auth, oil, 10)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    before = await lots_of(session, auth, order)
    name = "celerp-manufacturing"
    assert (await client.post(f"/companies/me/modules/{name}/disable", headers=auth["headers"])).status_code == 200
    assert await lots_of(session, auth, order) == before
    cake_lot = before[cake][0]["lot"]
    doc = await _sell(client, session, auth, cake_lot)
    assert (await client.post(f"/companies/me/modules/{name}/enable", headers=auth["headers"])).status_code == 200
    r = await reopen(client, auth, order, key="r")
    assert r.status_code == 409, r.text
    assert (await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})).status_code == 200
    assert (await reopen(client, auth, order, key="r2")).status_code == 200


# ---------------------------------------------------------------------------
# Close split
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed_n", [1, 2, 3, 4, 5])
async def test_close_split_oracle(client, session, auth, seed_n):
    """Seeded shares and quantities: each output's lots take its share of the run's value
    (main 100 less the extras), then each lot by quantity, in cents by largest remainder."""
    rng = random.Random(seed_n)
    tag = uuid.uuid4().hex[:6]
    raw = await _item(client, auth, round(rng.uniform(50, 900), 2), qty=100, sku=f"R-{tag}")
    main = await _item(client, auth, 0.0, qty=0, sku=f"M-{tag}")
    n = rng.randint(1, 3)
    extras, budget = [], 99
    for i in range(n):
        share = round(rng.uniform(0, budget / (n - i)), 2)
        budget -= share
        e = await _item(client, auth, 0.0, qty=0, sku=f"E{i}-{tag}")
        extras.append({"item_id": e, "quantity": rng.randint(1, 5), "cost_share": share})
    r = await put_recipe(client, auth, main, [(raw, 1)], extras)
    assert r.status_code == 200, r.text
    order = await start(client, auth, main, 7)
    assert (await issue(client, auth, order)).status_code == 200
    # Main in two receipts, each extra in its own entry.
    assert (await receive(client, auth, order, 3, key="a")).status_code == 200
    r = await complete(client, auth, order, key="c",
                       also_produces=[{"item_id": e["item_id"], "quantity": rng.randint(1, 9)} for e in extras])
    assert r.status_code == 200, r.text

    session.expire_all()
    run = await _state(session, auth, order)
    value = Decimal(str(run.get("wip_issued")))
    groups = [main] + [e["item_id"] for e in extras]
    weights = [Decimal(100) - sum(Decimal(str(e["cost_share"])) for e in extras)] + \
              [Decimal(str(e["cost_share"])) for e in extras]
    lots = await lots_of(session, auth, order)
    live = [(g, w) for g, w in zip(groups, weights) if g in lots]
    for (g, _), amount in zip(live, largest_remainder(value, [w for _, w in live])):
        mine = lots[g]
        expect = largest_remainder(amount, [Decimal(str(x["quantity"])) for x in mine])
        assert [cents(x["cost"]) for x in mine] == expect, (g, mine, expect)
    await assert_settled(client, session, auth)
