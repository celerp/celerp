# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock is placed whatever the invoices of an older release did with it.

An older release sold, shipped, took back, voided and corrected the cost of lots that
recorded no inventory account, and its invoices kept a cost allocation naming no
account either. Placing that stock reads what each invoice relieved from the entries it
posted: the cost an invoice recognized for goods it has not shipped stays on the
account its finalize entry took it from, so placing never posts that cost again, never
restates an entry, and the invoice ships later as any other does.

Each shape is built through the real documents while the lots still record their
account, then made to look as an older release left it (_as_older_release), and placed
at the next start. After it, and after the invoice ships, the books carry the stock
(stock_books.assert_settled), no opening stock was recognized for goods the books
already hold, and the invoice posted no new cost entry.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.auto_je import recognized_unshipped
from stock_books import assert_settled
from test_cost_restatement import _set_cost
from test_mfg_outstanding_demand import _credit
from test_older_stock_placement import _accounts, _marked, _off_books
from test_older_stock_reconciliation import _posted, _records
from test_posting_roles_lots import _sell
from test_posting_roles_older_stock import _as_older_release, _placement, _received
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio


async def _stock(client, auth, *costs: float, qty: float = 1) -> list[str]:
    """Lots brought in by the batch import, as a demo company's stock is."""
    records = [{"entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": "csv",
                "idempotency_key": f"older:{uuid.uuid4()}",
                "data": {"sku": f"OLD-{uuid.uuid4().hex[:6]}", "name": "Older lot", "quantity": qty,
                         "sell_by": "piece", "status": "available", "cost_total": cost}}
               for cost in costs]
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": records})
    assert r.status_code == 200, r.text
    return [rec["entity_id"] for rec in records]


async def _ship(client, auth, inv: str, *lots: str) -> None:
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": list(lots)})
    assert r.status_code == 200, r.text


async def _items(session, auth) -> list[str]:
    session.expire_all()
    return list((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars())


async def _available(session, auth) -> list[str]:
    session.expire_all()
    return list((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item",
        Projection.state["status"].as_string() == "available"))).scalars())


async def _doc_entries(session, auth, *docs: str) -> dict[str, int]:
    """How many entries each document has posted."""
    session.expire_all()
    out = {}
    for doc in docs:
        out[doc] = len((await session.execute(select(Projection.entity_id).where(
            Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
            Projection.entity_id.like(f"je:auto:{doc}:%")))).scalars().all())
    return out


async def _placed(session, client, auth, *invoices: str) -> None:
    """Make the company as an older release left it, place its stock at the next start,
    and check the placement: every lot records an account, the invoices posted nothing,
    no opening stock was recognized, and the books carry the stock."""
    before = await _doc_entries(session, auth, *invoices)
    await _as_older_release(session, auth, await _items(session, auth), list(invoices))
    await _marked(session, auth)
    await _startup(session)
    assert None not in await _accounts(session, auth, await _items(session, auth))
    assert await _doc_entries(session, auth, *invoices) == before
    assert await _placement(session, auth) is None  # no opening stock recognized
    await assert_settled(client, session, auth)


async def test_an_open_invoice_holding_older_stock_keeps_its_cost_where_it_was_relieved(session, client, auth):
    lots = await _stock(client, auth, 100.0, 100.0, 100.0)
    inv = await _sell(client, auth, (lots[0], 1))
    await _placed(session, client, auth, inv)
    assert set((await recognized_unshipped(session, auth["company_id"])).values()) == {100.0}

    await _ship(client, auth, inv, lots[0])
    await assert_settled(client, session, auth)


async def test_a_partly_shipped_invoice_ships_the_rest_after_placing(session, client, auth):
    lots = await _stock(client, auth, 100.0, 60.0, 40.0)
    inv = await _sell(client, auth, (lots[0], 1), (lots[1], 1))
    await _ship(client, auth, inv, lots[0])
    await _placed(session, client, auth, inv)

    await _ship(client, auth, inv, lots[1])
    await assert_settled(client, session, auth)


async def test_goods_taken_back_on_a_credit_note_are_placed_and_sell_again(session, client, auth):
    lots = await _stock(client, auth, 100.0, 50.0)
    inv = await _sell(client, auth, (lots[0], 1))
    await _ship(client, auth, inv, lots[0])
    sku = (await client.get(f"/items/{lots[0]}", headers=auth["headers"])).json()["sku"]
    note = await _credit(client, auth, inv, (sku, 1, lots[0]))
    await _placed(session, client, auth, inv, note)

    (back,) = [i for i in await _available(session, auth) if i != lots[1]]
    again = await _sell(client, auth, (back, 1))
    await _ship(client, auth, again, back)
    await assert_settled(client, session, auth)


async def test_a_voided_invoice_holds_nothing_and_its_goods_sell_again(session, client, auth):
    lots = await _stock(client, auth, 100.0, 50.0)
    inv = await _sell(client, auth, (lots[0], 1))
    r = await client.post(f"/docs/{inv}/void", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    await _placed(session, client, auth, inv)
    assert await recognized_unshipped(session, auth["company_id"]) == {}

    again = await _sell(client, auth, (lots[0], 1))
    await _ship(client, auth, again, lots[0])
    await assert_settled(client, session, auth)


async def test_an_invoice_whose_goods_were_costed_again_ships_at_the_corrected_cost(session, client, auth):
    lots = await _stock(client, auth, 100.0, 50.0)
    inv = await _sell(client, auth, (lots[0], 1))
    r = await _set_cost(client, auth, lots[0], 120.0)
    assert r.status_code == 200, r.text
    await _placed(session, client, auth, inv)

    await _ship(client, auth, inv, lots[0])
    await assert_settled(client, session, auth)


async def test_an_invoice_relieving_one_account_ships_cleanly_after_its_goods_are_placed_on_another(
        session, client, auth):
    """The invoice took its cost from opening inventory; the stock is placed on purchased
    inventory, which an older release had booked most of it to. The invoice's cost stays
    relieved from opening inventory until it ships, then follows the goods."""
    lots = await _stock(client, auth, 100.0, 100.0, 100.0, 100.0)
    inv = await _sell(client, auth, (lots[0], 1))
    await _posted(session, auth, "1130-P", "1130-OB", 250.0, "Goods movements booked by an older release")
    await _placed(session, client, auth, inv)
    assert set(await _accounts(session, auth, lots)) == {"1130-P"}
    (record,) = await _records(session, auth)
    assert record["classification"] == "transfer"

    await _ship(client, auth, inv, lots[0])
    await assert_settled(client, session, auth)



async def _reconciliation(client, auth) -> dict:
    r = await client.get("/accounting/older-stock", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_cost_invoices_recognized_on_unplaced_stock_keeps_the_placement_under_review(session, client, auth):
    """An older release invoiced goods from two accounts in one invoice, so its entry names
    no one account their cost left: that cost is provisional. The placement is never
    called exact while it stands, the view names it, and the company stays under review,
    with sales going on. Once the invoice ships, the review shows what the books check
    finds on each account, never reconciled while an account is out of line."""
    lots = await _stock(client, auth, 100.0, 100.0)
    received = await _received(session, auth, 50.0)
    inv = await _sell(client, auth, (lots[0], 1), (received, 1))
    await _as_older_release(session, auth, await _items(session, auth), [inv])
    await _marked(session, auth)
    await _startup(session)

    (record,) = [r for r in await _records(session, auth) if r["provisional"]]
    assert (record["provisional"], record["classification"]) == (150.0, "provisional")
    view = await _reconciliation(client, auth)
    (placement,) = [p for p in view["placements"] if p["pid"] == record["placement"]]
    assert (placement["provisional"], placement["classification"]) == (150.0, "provisional")
    assert view["reconciliation"]["state"] == "review"
    assert view["reconciliation"]["open"] == [
        {"placement": record["placement"], "account": record["account"], "kind": "provisional", "amount": 150.0}]

    await _ship(client, auth, inv, lots[0], received)
    gaps = [f for f in await _off_books(session, auth) if f["kind"] == "stock_gap"]
    rec = (await _reconciliation(client, auth))["reconciliation"]
    assert rec["open"] == [{"account": f["account"], "kind": "stock_gap", "amount": round(f["books"] - f["stock"], 2),
                            "books": f["books"], "stock": f["stock"]} for f in gaps]
    assert rec["state"] == ("review" if gaps else "reconciled")
