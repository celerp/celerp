# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The same bank facts reach the same books whatever order they are entered in: keyed
first then the statements, the statements first then decided, and mixed orders (entries
keyed after import, a transfer posted from the other bank before this bank's statement
arrives). Ledger, document balance, line states and completion are compared across
orders, and undoing every line returns each order to the books it had before reconciling."""

from __future__ import annotations

from decimal import Decimal

import pytest

import bank_statement_helpers as B

pytestmark = pytest.mark.asyncio

# Bank A opens at 500 on 2025-12-01. Before the first statement: rent 40 (cleared in
# December) and cheque 101 for 30 (clears on the statement). In January: ACME 40, a
# transfer of 100 to bank B and the customer paying an invoice of 200 into A.
A_ROWS = [("2026-01-03", "CHEQUE 101", "-30.00"), ("2026-01-05", "ACME LTD", "-40.00"),
          ("2026-01-06", "TRANSFER TO SAVINGS", "-100.00"), ("2026-01-10", "CUSTOMER PAYMENT", "200.00")]
B_ROWS = [("2026-01-06", "TRANSFER FROM CURRENT", "100.00")]


async def _world(client):
    if client not in B._owners:  # the install's first company comes with its own setup entries
        await B.company(client)
    co = await B.company(client)
    a = await B.bank(client, co["h"], name="A", opening=500.0, opening_date="2025-12-01")
    b = await B.bank(client, co["h"], name="B")
    r = await client.post("/docs", headers=co["h"], json={
        "doc_type": "invoice", "contact_id": "contact:1", "issue_date": "2026-01-02",
        "line_items": [{"name": "Work", "quantity": 1, "unit_price": 200.0, "line_total": 200.0}],
        "subtotal": 200.0, "tax": 0.0, "total": 200.0})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    assert (await client.post(f"/docs/{inv}/finalize", json={}, headers=co["h"])).status_code == 200
    return {"co": co, "a": a, "b": b, "inv": inv}


async def _expense(client, w, amount, day, memo):
    r = await client.post("/accounting/journal-entries", headers=w["co"]["h"], json={
        "ts": day, "memo": memo, "idempotency_token": B.key(),
        "entries": [{"account": "5100", "debit": amount, "credit": 0.0},
                    {"account": w["a"]["chart_account_code"], "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text


async def _pre_start(client, w):
    await _expense(client, w, 40.0, "2025-12-20", "December rent")
    await _expense(client, w, 30.0, "2025-12-28", "CHEQUE 101")


async def _key_acme(client, w):
    await _expense(client, w, 40.0, "2026-01-05", "ACME LTD")


async def _key_transfer(client, w):
    r = await client.post("/accounting/transfers", headers=w["co"]["h"], json={
        "from_bank_id": w["a"]["id"], "to_bank_id": w["b"]["id"], "amount": 100.0, "date": "2026-01-06",
        "idempotency_key": B.key()})
    assert r.status_code == 200, r.text


async def _key_payment(client, w):
    r = await client.post(f"/docs/{w['inv']}/payment", headers=w["co"]["h"], json={
        "payment_date": "2026-01-10", "amount": 200.0, "bank_account": w["a"]["chart_account_code"]})
    assert r.status_code == 200, r.text


async def _import(client, w, which):
    rows, opening = (A_ROWS, "460.00") if which == "a" else (B_ROWS, "0.00")
    closing = f"{Decimal(opening) + sum(Decimal(x) for _, _, x in rows):.2f}"
    await B.statement(client, w["co"], w[which]["id"], rows, opening=opening, closing=closing,
                      start="2026-01-01", end="2026-01-31", tick_all=True)


async def _accept_all(client, w, which):
    """Refresh the suggestions and accept every one that answers its line (a question does not)."""
    co, bank_id = w["co"], w[which]["id"]
    r = await client.post(f"{B.API}/bank/{bank_id}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    table = await B.groups(client, co, bank_id)
    ids = [l["id"] for l in B.all_lines(table) if (l["suggestion"] or {}).get("kind")]
    keys = [g["key"] for g in table["groups"] if any(l["id"] in ids for l in g["lines"])]
    if keys:
        r = await B.resolve(client, co, bank_id, accept=B.accept(table, keys, ids))
        assert r.status_code == 200, (r.json(), [(l["description"], l["status"], (l["suggestion"] or {}).get("kind"))
                                               for l in B.all_lines(table)])


async def _decide(client, w, which, text, kind, **fields):
    line = B.line_by_text(await B.groups(client, w["co"], w[which]["id"]), text)
    r = await B.resolve(client, w["co"], w[which]["id"],
                        decisions=[{"line_ids": [line["id"]], "kind": kind, **fields}])
    assert r.status_code == 200, r.text
    assert all(res["ok"] for res in r.json()["results"]), r.json()


async def _decide_a(client, w):
    """Bank A's lines decided by hand on the statement."""
    await _decide(client, w, "a", "ACME", "post", account_code="5100")
    await _decide(client, w, "a", "TRANSFER", "transfer", target_bank_id=w["b"]["id"])
    await _decide(client, w, "a", "CUSTOMER", "pay_doc", docs=[{"doc_id": w["inv"], "amount": 200.0}])
    await _accept_all(client, w, "a")  # the cheque keyed before the statement


async def _books(client, w) -> dict:
    h = w["co"]["h"]
    tb = (await client.get("/accounting/trial-balance", headers=h)).json()
    doc = (await client.get(f"/docs/{w['inv']}", headers=h)).json()
    lines = {}
    for which in ("a", "b"):
        for line in B.all_lines(await B.groups(client, w["co"], w[which]["id"], view="all")):
            lines[(which, line["description"])] = line["status"] != "unmatched"
    return {"ledger": {l["code"]: (round(l["total_debit"], 2), round(l["total_credit"], 2)) for l in tb["lines"]},
            "outstanding": Decimal(str(doc.get("amount_outstanding", doc.get("balance_due")))),
            "lines": lines}


async def _keyed_first(client, w):
    for step in (_pre_start, _key_acme, _key_transfer, _key_payment):
        await step(client, w)
    await _import(client, w, "a")
    await _import(client, w, "b")
    before = await _books(client, w)
    await _accept_all(client, w, "a")
    await _accept_all(client, w, "b")
    return before


async def _statement_first(client, w):
    await _import(client, w, "a")
    await _import(client, w, "b")
    await _pre_start(client, w)  # dated before the statement, keyed after it was read
    before = await _books(client, w)
    await _decide_a(client, w)  # the transfer settles B's one counterpart too
    return before


async def _mixed(client, w):
    """Both statements read, then ACME keyed and the invoice paid by hand before deciding:
    the posting decision for ACME matches the keyed entry instead of posting it again."""
    await _pre_start(client, w)
    await _import(client, w, "a")
    await _import(client, w, "b")
    await _key_acme(client, w)
    await _key_payment(client, w)
    before = await _books(client, w)
    await _decide(client, w, "a", "ACME", "post", account_code="5100")
    await _decide(client, w, "a", "TRANSFER", "transfer", target_bank_id=w["b"]["id"])
    await _accept_all(client, w, "a")
    return before


async def _other_bank_first(client, w):
    """The transfer is posted from bank B's statement; A's statement arrives later and
    matches the entry B posted."""
    await _pre_start(client, w)
    await _key_acme(client, w)
    await _key_payment(client, w)
    await _import(client, w, "b")
    before = await _books(client, w)
    await _decide(client, w, "b", "TRANSFER", "transfer", target_bank_id=w["a"]["id"])
    await _import(client, w, "a")
    await _accept_all(client, w, "a")
    return before


ORDERS = {"keyed_first": _keyed_first, "statement_first": _statement_first, "mixed": _mixed,
          "other_bank_first": _other_bank_first}


async def _complete(client, w, which) -> tuple:
    r = await client.post(f"{B.API}/bank/{w[which]['id']}/complete", json={"idempotency_key": B.key()},
                          headers=w["co"]["h"])
    assert r.status_code == 200, r.text
    return bool(r.json()["completed"]), r.json()["stopped_at"]


async def _undo_all(client, w):
    for which in ("a", "b"):
        ids = [l["id"] for l in B.all_lines(await B.groups(client, w["co"], w[which]["id"], view="all"))
               if l["status"] != "unmatched"]
        if ids:
            r = await client.post(f"{B.API}/bank/{w[which]['id']}/unmatch", headers=w["co"]["h"],
                                  json={"line_ids": ids, "idempotency_key": B.key()})
            assert r.status_code == 200, r.text


async def test_every_order_reaches_the_same_books(client):
    got = {}
    for name, run in ORDERS.items():
        w = await _world(client)
        await run(client, w)
        books = await _books(client, w)
        assert all(books["lines"].values()), (name, books["lines"])
        got[name] = books
    first = got["keyed_first"]
    assert first["outstanding"] == Decimal("0")
    for name, books in got.items():
        assert books["ledger"] == first["ledger"], (name, sorted(set(books["ledger"].items()) ^ set(first["ledger"].items())))
        assert books == first, (name, books, first)


async def test_every_order_completes_alike(client):
    got = {}
    for name, run in ORDERS.items():
        w = await _world(client)
        await run(client, w)
        got[name] = (await _complete(client, w, "a"), await _complete(client, w, "b"))
    assert got["keyed_first"] == ((True, None), (True, None)), got
    assert len(set(map(str, got.values()))) == 1, got


@pytest.mark.parametrize("order", list(ORDERS))
async def test_undo_returns_each_order_to_the_books_before_reconciling(client, order):
    w = await _world(client)
    before = await ORDERS[order](client, w)
    await _undo_all(client, w)
    after = await _books(client, w)
    assert after["ledger"] == before["ledger"], (order, after["ledger"], before["ledger"])
    assert after["outstanding"] == before["outstanding"]
    assert not any(after["lines"].values()), after["lines"]
