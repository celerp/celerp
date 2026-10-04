# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Production runs as records of what actually happened: cost flows from the quantities the run
really issued and received, not from the recipe's planned figures.

Every run produces its output as a discrete lot (like a received purchase), so fungible items carry
a true weighted-average cost across batches. Issuing moves component value into the run's work in
progress, each receipt moves its share on to the lot it creates, and completion settles the lots
to the run's final cost with any waste going to cost of goods sold.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


async def _register(client, email: str | None = None) -> str:
    addr = email or f"admin-{uuid.uuid4().hex[:8]}@mfg.test"
    r = await client.post("/auth/register", json={"company_name": "Run Co", "email": addr, "name": "Admin", "password": "validpass1"})
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


async def _run_entries(client, token, run) -> list[list[dict]]:
    """The lines of every entry the run posted: its issues, its receipts and its completion."""
    led = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    return [e["data"]["entries"] for e in led
            if run in (e["data"].get("memo") or "") and e["event_type"] == "acc.journal_entry.created"]


def _net(run_entries: list[list[dict]], account: str) -> float:
    """Debits less credits on ``account`` across the run's entries."""
    return round(sum(float(x.get("debit") or 0) - float(x.get("credit") or 0)
                     for entries in run_entries for x in entries if x["account"] == account), 2)


def _input_relief(run_entries) -> float:
    """Components here are entered by hand, so they leave opening inventory."""
    return -_net(run_entries, "1130-OB")


def _output_cap(run_entries) -> float:
    return _net(run_entries, "1130-P")


def _waste_leg(run_entries) -> float:
    return _net(run_entries, "5100")


def _all_balanced(run_entries) -> None:
    for entries in run_entries:
        _balanced(entries)
    assert _net(run_entries, "1130-WIP") == 0  # a closed run holds nothing


async def _lots(client, token, run) -> list[dict]:
    items = (await client.get("/items", headers=_h(token))).json()["items"]
    return [i for i in items if i.get("manufacturing_order_id") == run and i.get("lot") is True]


# ---------------------------------------------------------------------------
# Input cost = what was actually issued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_cost_is_what_each_issue_moved(client):
    """The output carries the value of the components actually issued, issue by issue, and the
    output cannot be received while part of the recipe is still to be issued."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD1", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING1", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 10 for a build of 2

    run = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run, [{"item_id": gold, "quantity": 6}])).status_code == 200
    r = await client.post(f"/manufacturing/{run}/receive", headers=_h(token))
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "mfg.issue_first", r.text
    assert await _lots(client, token, run) == []
    assert (await _issue(client, token, run)).status_code == 200  # the other 4
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token))).status_code == 200  # completes
    entries = await _run_entries(client, token, run)
    _all_balanced(entries)
    assert _input_relief(entries) == 10 * 80
    assert [float(lot["cost_total"]) for lot in await _lots(client, token, run)] == [10 * 80]


# ---------------------------------------------------------------------------
# Output unit cost = run cost / quantity actually received
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receiving_more_than_the_run_makes_is_refused(client):
    """Receiving more than the run's outstanding output is refused and changes nothing; the run's
    output then carries the whole input cost."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING2", quantity=0, allow_splitting=False)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 50 for a build of 10

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200  # 50 gold -> input 4000
    r = await client.post(f"/manufacturing/{run}/receive", headers=_h(token), json={"quantity": 12})
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "mfg.over_receipt", r.text
    assert await _lots(client, token, run) == []
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token),
                              json={"quantity": 10})).status_code == 200  # completes

    lots = await _lots(client, token, run)
    assert len(lots) == 1
    assert lots[0]["quantity"] == 10 and float(lots[0]["cost_total"]) == 4000


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
async def test_produced_lot_gets_fresh_barcode(client):
    """A produced lot is a new physical parcel and must be born scannable: its
    item.created carries a freshly allocated barcode. Red at merge-base: the lot
    was created with no barcode at all."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDBC", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RINGBC", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = await _build(client, token, ring, 2, complete=True)

    lots = await _lots(client, token, run)
    assert len(lots) == 1
    lot = (await client.get(f"/items/{lots[0]['id']}", headers=_h(token))).json()
    assert lot.get("barcode"), f"a produced lot must carry a fresh barcode, got {lot.get('barcode')!r}"


@pytest.mark.asyncio
async def test_mfg_lot_none_product_splittable(client, session):
    """A lot manufactured from a product whose allow_splitting is unset/None inherits
    allow_splitting True (routed through splitting_allowed). At merge-base the lot's
    item.created event stores bool(None) = False, so the lot is wrongly non-splittable."""
    from celerp.models.projections import Projection
    token = await _register(client)
    gold = await _item(client, token, "GOLDN", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RINGN", quantity=0)
    # Force the product's allow_splitting to a present None (older imports left it unset).
    row = (await session.execute(select(Projection).where(Projection.entity_id == ring))).scalar_one()
    st = dict(row.state)
    st["allow_splitting"] = None
    row.state = st
    await session.commit()

    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    run = await _build(client, token, ring, 2, complete=True)

    lots = await _lots(client, token, run)
    assert len(lots) == 1
    lot = (await client.get(f"/items/{lots[0]['id']}", headers=_h(token))).json()
    assert lot["allow_splitting"] is True, (
        f"a lot from a None-allow_splitting product must be splittable, got {lot.get('allow_splitting')}")


@pytest.mark.asyncio
async def test_fungible_sale_cogs_actual_lot_cost(client):
    """Two fungible runs at different actual costs create two lots; a sale draws them FIFO so COGS
    is the real cost of the specific lots consumed, not a recipe-standard figure. The second run
    wastes half its material, so its output carries half the cost."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD4", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING4", quantity=0)  # splittable
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run_a = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run_a)).status_code == 200  # input 800
    assert (await client.post(f"/manufacturing/{run_a}/receive", headers=_h(token))).status_code == 200  # 2 @ 400

    run_b = await _build(client, token, ring, 2)
    assert (await _issue(client, token, run_b)).status_code == 200  # input 800
    assert (await client.post(f"/manufacturing/{run_b}/complete", headers=_h(token),
                              json={"waste_quantity": 5})).status_code == 200  # 400 wasted, 2 @ 200

    lot_a = (await _lots(client, token, run_a))[0]["id"]
    doc = await _create_and_finalize_invoice(client, token, [
        {"sku": "RING4", "name": "RING4", "quantity": 3, "unit_price": 10.0, "entity_id": lot_a},
    ])
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(token), json={"line_entity_ids": [lot_a]})
    assert r.status_code == 200, r.text
    assert (await _fulfilled_cogs(client, token, doc)) == pytest.approx(2 * 400 + 1 * 200)  # 1000


# ---------------------------------------------------------------------------
# actual_outputs is honored by completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_records_what_was_received(client):
    """What a run made is what was received from it: completing records the received quantity of
    its product, and a declared yield is refused rather than recorded beside it."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD5", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RING5", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token), json={"quantity": 7})).status_code == 200
    r = await client.post(f"/manufacturing/{run}/complete", headers=_h(token),
                          json={"actual_outputs": [{"sku": "RING5", "name": "RING5", "quantity": 7}]})
    assert r.status_code == 422, r.text
    r = await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/manufacturing/{run}", headers=_h(token))).json()
    assert state["actual_outputs"] == [{"sku": "RING5", "name": "RING5", "quantity": 10.0, "category": None}]


# ---------------------------------------------------------------------------
# Completion JE reconciles with the lots it created (end to end, through a sale)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_je_reconciles_with_lots(client, session):
    """Multi-receipt run: the run's entries carry into produced stock exactly the sum of the lot
    costs, and selling all of them relieves exactly that cost as COGS. Nothing is stranded."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD6", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING6", quantity=0)  # splittable
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # planned 50 for a build of 10

    run = await _build(client, token, ring, 10)
    assert (await _issue(client, token, run)).status_code == 200  # input 4000
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token),
                              json={"quantity": 6})).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/receive", headers=_h(token),
                              json={"quantity": 4})).status_code == 200  # total 10, completes

    lots = await _lots(client, token, run)
    assert len(lots) == 2
    lots_total = sum(float(l["cost_total"]) for l in lots)
    entries = await _run_entries(client, token, run)
    _all_balanced(entries)
    assert lots_total == pytest.approx(4000, abs=0.1)
    assert _output_cap(entries) == pytest.approx(4000, abs=0.1)
    assert _input_relief(entries) == pytest.approx(4000, abs=0.1)

    lot_a = sorted(lots, key=lambda l: l["id"])[0]["id"]
    doc = await _create_and_finalize_invoice(client, token, [
        {"sku": "RING6", "name": "RING6", "quantity": 10, "unit_price": 10.0, "entity_id": lot_a},
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

    entries = await _run_entries(client, token, run)
    _all_balanced(entries)
    assert _waste_leg(entries) == pytest.approx(400)  # 4000 * 5/50
    assert _output_cap(entries) == pytest.approx(3600)
    assert sum(float(l["cost_total"]) for l in await _lots(client, token, run)) == pytest.approx(3600, abs=0.1)


@pytest.mark.asyncio
async def test_waste_over_input_refused(client):
    """Negative waste is rejected outright, and so is waste beyond what the run issued: it is
    refused, never cut down to fit, so nothing is recorded that was not asked for."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD8", quantity=1000, cost_total=80000)  # unit 80
    ring = await _item(client, token, "RING8", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    neg = await _build(client, token, ring, 2)
    assert (await _issue(client, token, neg)).status_code == 200
    assert (await client.post(f"/manufacturing/{neg}/complete", headers=_h(token),
                              json={"waste_quantity": -1})).status_code == 422

    over = await _build(client, token, ring, 2)
    assert (await _issue(client, token, over)).status_code == 200  # 10 issued, input 800
    r = await client.post(f"/manufacturing/{over}/complete", headers=_h(token), json={"waste_quantity": 1000})
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "mfg.over_waste", r.text
    assert (await client.get(f"/manufacturing/{over}", headers=_h(token))).json()["status"] != "completed"

    assert (await client.post(f"/manufacturing/{over}/complete", headers=_h(token),
                              json={"waste_quantity": 10})).status_code == 200
    entries = await _run_entries(client, token, over)
    _all_balanced(entries)
    assert _output_cap(entries) == pytest.approx(0)
    assert _waste_leg(entries) == pytest.approx(800)  # everything issued
    assert [float(lot["cost_total"]) for lot in await _lots(client, token, over)] == [0.0]


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
    """Completing the same run twice with the same key records a single completion: the retry
    replays the first answer rather than posting a duplicate."""
    token = await _register(client)
    gold = await _item(client, token, "GOLDA", quantity=1000, cost_total=80000)
    ring = await _item(client, token, "RINGA", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    run = await _build(client, token, ring, 2)

    body = {"waste_quantity": 1, "idempotency_key": "close-1"}
    for _ in range(2):
        r = await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json=body)
        assert r.status_code == 200, r.text

    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == run, LedgerEntry.event_type == "mfg.order.completed"))).scalars().all()
    assert len(rows) == 1
    assert len(await _lots(client, token, run)) == 1
    assert _input_relief(await _run_entries(client, token, run)) == 10 * 80


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
