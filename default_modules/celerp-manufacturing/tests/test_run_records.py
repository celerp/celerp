# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Production runs as records of what actually happened: cost flows from the quantities the run
really issued and received, not from the recipe's planned figures.

Every run produces its output as a discrete lot (like a received purchase), so fungible items carry
a true weighted-average cost across batches. Completion re-costs those lots to the run's actual
output cost, and the completion journal entry reconciles against the sum of the lots it created.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry


async def _register(client, email: str | None = None) -> str:
    addr = email or f"admin-{uuid.uuid4().hex[:8]}@mfg.test"
    r = await client.post("/auth/register", json={"company_name": "Run Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
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


async def _build(client, token, item_id, quantity, complete=False) -> str:
    r = await client.post(f"/manufacturing/items/{item_id}/build", headers=_h(token),
                          json={"quantity": quantity, "complete": complete})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _issue(client, token, run, items=None):
    body = {"items": items} if items is not None else {}
    return await client.post(f"/manufacturing/{run}/issue", headers=_h(token), json=body)


def _balanced(entries: list[dict]) -> None:
    d = sum(float(x.get("debit", 0) or 0) for x in entries)
    c = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(d - c) < 1e-6, entries


async def _completion_entries(client, token, run) -> list[dict]:
    led = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in led if (e["data"].get("memo") or "") == f"Auto JE for {run} completion")
    return je["data"]["entries"]


def _input_relief(entries: list[dict]) -> float:
    return next(float(x["credit"]) for x in entries if x["account"] == "1130-P" and float(x.get("credit") or 0) > 0)


def _output_cap(entries: list[dict]) -> float:
    return next((float(x["debit"]) for x in entries if x["account"] == "1130-P" and float(x.get("debit") or 0) > 0), 0.0)


def _waste_leg(entries: list[dict]) -> float:
    return next((float(x["debit"]) for x in entries if x["account"] == "5100" and float(x.get("debit") or 0) > 0), 0.0)


async def _lots(client, token, run) -> list[dict]:
    items = (await client.get("/items", headers=_h(token))).json()["items"]
    return [i for i in items if i.get("manufacturing_order_id") == run and i.get("lot") is True]


async def _actors(session):
    company = (await session.execute(select(Company))).scalars().first()
    user = (await session.execute(select(User))).scalars().first()
    return company.id, user


# ---------------------------------------------------------------------------
# Input cost = what was actually issued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_cost_uses_issued(client):
    """The completion input relief reflects the components actually issued, not the recipe plan;
    with nothing issued it falls back to the planned figure so a bare run still costs something."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD1", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING1", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 10 for a build of 2

    run = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run, [{"item_id": gold, "quantity": 6}])).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token))).status_code == 200  # auto-completes
    assert _input_relief(await _completion_entries(client, token, run)) == pytest.approx(6 * 80)

    bare = await _build(client, token, ring, 2)
    assert (await client.post(f"/manufacturing/{bare}/receive", headers=_h(token))).status_code == 200
    assert _input_relief(await _completion_entries(client, token, bare)) == pytest.approx(10 * 80)


# ---------------------------------------------------------------------------
# Output unit cost = run cost / quantity actually received
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_unit_cost_actual_received(client):
    """Over-yield: receiving more than the planned output spreads the same input cost over the
    real quantity, so the lot's total cost equals the run input and its unit cost drops."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING2", quantity=0, allow_splitting=False)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 50 for a build of 10

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200  # 50 gold -> input 4000
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token),
                              json={"quantity": 12})).status_code == 200  # over-yield, auto-completes

    lots = await _lots(client, token, run)
    assert len(lots) == 1
    lot = lots[0]
    assert lot["quantity"] == 12
    assert float(lot["cost_total"]) == pytest.approx(4000, abs=0.05)  # not 12 * (4000/10)


# ---------------------------------------------------------------------------
# Every output is a discrete lot at actual cost, fungible included
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fungible_output_creates_lot_at_actual_cost(client):
    """A splittable (fungible) product no longer folds into a single SKU pile: each run yields its
    own lot carrying that run's actual cost, leaving the catalog product itself at zero on-hand."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD3", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING3", quantity=0)  # splittable by default
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = await _build(client, token, ring, 2, complete=True)  # input 10 * 80 = 800

    lots = await _lots(client, token, run)
    assert len(lots) == 1
    lot = lots[0]
    assert lot["parent_item_id"] == ring and lot["quantity"] == 2
    assert float(lot["cost_total"]) == pytest.approx(800, abs=0.05)
    assert (await client.get(f"/items/{ring}", headers=_h(token))).json()["quantity"] == 0


@pytest.mark.asyncio
async def test_fungible_sale_cogs_actual_lot_cost(client):
    """Two fungible runs at different actual costs create two lots; a sale draws them FIFO so COGS
    is the real cost of the specific lots consumed, not a recipe-standard figure."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD4", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING4", quantity=0)  # splittable
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run_a = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run_a)).status_code == 200  # input 800
    assert (await client.post(f"/manufacturing/{run_a}/receive", headers=_h(token))).status_code == 200  # 2 @ 400

    run_b = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run_b)).status_code == 200  # input 800
    assert (await client.post(f"/manufacturing/{run_b}/receive", headers=_h(token),
                              json={"quantity": 4})).status_code == 200  # over-yield, 4 @ 200

    lot_a = (await _lots(client, token, run_a))[0]["id"]
    doc = await _create_and_finalize_invoice(client, token, [
        {"sku": "RING4", "name": "RING4", "quantity": 3, "unit_price": 10.0, "entity_id": lot_a},
    ])
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [lot_a]})
    assert r.status_code == 200, r.text
    assert (await _fulfilled_cogs(client, token, doc)) == pytest.approx(2 * 400 + 1 * 200)  # 1000


# ---------------------------------------------------------------------------
# actual_outputs is honored by _close_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_run_honors_actual_outputs(client):
    """Completing with an explicit actual_outputs records that yield on the run, rather than echoing
    the recipe's expected output back as the actual."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD5", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RING5", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200
    r = await client.post(f"/manufacturing/{run}/complete", headers=_h(token),
                          json={"actual_outputs": [{"sku": "RING5", "name": "RING5", "quantity": 7}]})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert float(state["actual_outputs"][0]["quantity"]) == 7


# ---------------------------------------------------------------------------
# Completion JE reconciles with the lots it created (end to end, through a sale)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_je_reconciles_with_lots(client, session):
    """Multi-receipt run: the completion journal entry's output cost equals the sum of the lot costs
    it produced, and selling all of them relieves exactly that cost as COGS. Nothing is stranded."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD6", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING6", quantity=0)  # splittable
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 50 for a build of 10

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200  # input 4000
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token),
                              json={"quantity": 6})).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token),
                              json={"quantity": 6})).status_code == 200  # total 12, over-yield, auto-completes

    lots = await _lots(client, token, run)
    assert len(lots) == 2
    lots_total = sum(float(l["cost_total"]) for l in lots)
    entries = await _completion_entries(client, token, run)
    _balanced(entries)
    assert lots_total == pytest.approx(4000, abs=0.1)
    assert _output_cap(entries) == pytest.approx(4000, abs=0.1)
    assert _input_relief(entries) == pytest.approx(4000, abs=0.1)

    lot_a = sorted(lots, key=lambda l: l["id"])[0]["id"]
    doc = await _create_and_finalize_invoice(client, token, [
        {"sku": "RING6", "name": "RING6", "quantity": 12, "unit_price": 10.0, "entity_id": lot_a},
    ])
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [lot_a]})
    assert r.status_code == 200, r.text
    assert (await _fulfilled_cogs(client, token, doc)) == pytest.approx(4000, abs=0.1)


# ---------------------------------------------------------------------------
# Waste routes to COGS; lots reconcile to input minus waste; boundaries are clamped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waste_to_cogs_and_reconcile(client):
    """Declared waste at completion posts to COGS and the lots reconcile to input cost minus waste,
    so the output inventory carries only the cost of what was kept."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD7", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING7", quantity=0)  # splittable
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 50 for a build of 10

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200  # input 4000
    r = await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={"waste_quantity": 5})
    assert r.status_code == 200, r.text

    entries = await _completion_entries(client, token, run)
    _balanced(entries)
    assert _waste_leg(entries) == pytest.approx(400)  # 4000 * 5/50
    assert _output_cap(entries) == pytest.approx(3600)
    assert sum(float(l["cost_total"]) for l in await _lots(client, token, run)) == pytest.approx(3600, abs=0.1)


@pytest.mark.asyncio
async def test_waste_over_input_clamped(client):
    """Negative waste is rejected outright, and waste exceeding the input cost is clamped so the
    completion entry can never go unbalanced or drive output cost below zero."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD8", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING8", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    neg = await _build(client, token, ring, 2)
    assert (await _issue(client, token, neg)).status_code == 200
    assert (await client.post(f"/manufacturing/{neg}/complete", headers=_h(token),
                              json={"waste_quantity": -1})).status_code == 422

    over = await _build(client, token, ring, 2)
    assert (await _issue(client, token, over)).status_code == 200  # input 800
    assert (await client.post(f"/manufacturing/{over}/complete", headers=_h(token),
                              json={"waste_quantity": 1000})).status_code == 200
    entries = await _completion_entries(client, token, over)
    _balanced(entries)
    assert _output_cap(entries) == pytest.approx(0)
    assert _waste_leg(entries) == pytest.approx(800)  # clamped to input cost


# ---------------------------------------------------------------------------
# Idempotency: a re-submitted receipt or a repeated close does not double up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_idempotent_on_double_submit(client):
    """A partial receipt re-submitted with the same idempotency key produces one lot and counts the
    quantity once, so a retried network request never creates a phantom second lot."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD9", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RING9", quantity=0, allow_splitting=False)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200
    body = {"quantity": 3, "idempotency_key": "recv-1"}
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token), json=body)).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token), json=body)).status_code == 200

    lots = await _lots(client, token, run)
    assert len(lots) == 1 and lots[0]["quantity"] == 3
    assert float((await client.get(f"/manufacturing/{run}", headers=_h(token))).json()["received_qty"]) == 3


@pytest.mark.asyncio
async def test_complete_idempotent_double_call(client, session):
    """Closing the same run twice emits a single completion event: the deterministic completion key
    dedups the second call rather than posting a duplicate."""
    import celerp_manufacturing.routes as mfg

    token = await _register(client)
    gold = await _item(client, token, "GOLDA", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RINGA", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    run = await _build(client, token, ring, 2)

    company_id, user = await _actors(session)
    row = await mfg._get_order(session, company_id, run)
    states = await mfg._all_item_states(session, company_id)
    await mfg._close_run(session, company_id, user, run, row.state, states, None)
    await mfg._close_run(session, company_id, user, run, row.state, states, None)
    await session.commit()

    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == run, LedgerEntry.event_type == "mfg.order.completed"))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_complete_zero_received_relieves_actual_input(client, session):
    """A run closed with components issued but nothing received relieves the actually-issued input
    cost, creates no lot, and does not divide by a zero received quantity."""
    import celerp_manufacturing.routes as mfg

    token = await _register(client)
    gold = await _item(client, token, "GOLDB", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RINGB", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 10 for a build of 2
    run = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run, [{"item_id": gold, "quantity": 6}])).status_code == 200  # actual 480

    company_id, user = await _actors(session)
    row = await mfg._get_order(session, company_id, run)
    states = await mfg._all_item_states(session, company_id)
    await mfg._close_run(session, company_id, user, run, row.state, states, None)
    await session.commit()

    assert _input_relief(await _completion_entries(client, token, run)) == pytest.approx(6 * 80)
    assert await _lots(client, token, run) == []


# ---------------------------------------------------------------------------
# Local sale helpers (one company/token, mirrors the fulfillment suite's flow)
# ---------------------------------------------------------------------------


async def _create_and_finalize_invoice(client, token, line_items) -> str:
    payload = {
        "doc_type": "invoice",
        "ref_id": f"RUN-{uuid.uuid4().hex[:6]}",
        "line_items": line_items,
        "total": sum(li.get("quantity", 0) * li.get("unit_price", 0) for li in line_items),
    }
    r = await client.post("/docs", headers=_h(token), json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
    assert r2.status_code == 200, r2.text
    return doc_id


async def _fulfilled_cogs(client, token, doc) -> float:
    led = (await client.get(f"/ledger?entity_id={doc}", headers=_h(token))).json()["items"]
    fulfilled = next(e for e in led if e.get("event_type") == "doc.fulfilled")
    return float(fulfilled["data"].get("total_cogs", 0))
