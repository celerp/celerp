# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fund movements as real concurrent requests.

Each race queues its requests behind a transaction holding the company row, then lets
them all run against committed data. Two movements whose caps together exceed what a
fund holds (or what purpose spending backs) must come out serial: one posts and the
other is refused on the balance the first left. A movement racing an invoice finalize
and a payment must let all three finish: every writer takes the company lock, then the
fund ledger, then projection rows, so none waits on a lock another holds while holding
one it needs.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank,
4100 sales revenue, 6200 rent, 3200 retained earnings), funds turned on, a restricted fund
R "Library gift" for scholarships, a second restricted fund R2 for scholarships and two
unrestricted funds U1 and U2.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from company_backup_support import company, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_funds_serializes_pg import _blocked, _held

pytestmark = pytest.mark.asyncio

DATE = "2026-03-02"


class _Books:
    def __init__(self, client, tok: str, cid):
        self.client, self.headers, self.cid = client, auth(tok), cid

    async def post(self, path: str, **body):
        return await self.client.post(path, headers=self.headers, json=body)

    async def ok(self, path: str, **body) -> dict:
        r = await self.post(path, **body)
        assert r.status_code == 200, r.text
        return r.json()

    async def fund(self, name: str, restricted: bool) -> str:
        return (await self.ok("/accounting/funds", request_key=uuid.uuid4().hex, name=name, restricted=restricted,
                              purpose="Scholarships" if restricted else None))["id"]

    async def entry(self, *lines) -> str:
        """A manual entry of (account, debit, credit, fund) lines; returns its id."""
        return (await self.ok("/accounting/journal-entries", ts="2026-03-01", memo="Funds",
                              idempotency_token=uuid.uuid4().hex, entries=[
                                  {"account": a, "debit": d, "credit": c, **({"fund_id": f} if f else {})}
                                  for a, d, c, f in lines]))["je_id"]

    def movement(self, path: str, **body):
        return self.post(f"/accounting/funds/{path}", request_key=uuid.uuid4().hex, date=DATE,
                         reason="Board minute 12", **body)


async def _books(engine, client) -> tuple[_Books, dict]:
    from test_helpers import provision_company_books

    user = await owner(engine)
    cid = await company(engine, user, "Fund Race", "fund-race", settings={"currency": "USD"})
    async with maker(engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    b = _Books(client, await token(engine, user, cid), cid)
    r =await client.put("/accounting/funds/setting", headers=b.headers, json={"enabled": True})
    assert r.status_code == 200, r.text
    return b, {"R": await b.fund("Library gift", True), "R2": await b.fund("Library gift two", True),
               "U1": await b.fund("Board reserve", False), "U2": await b.fund("Building reserve", False)}


async def _race(engine, cid, *requests):
    """Start every request while the company row is held, wait until each queues on it,
    then release them; return the responses in request order."""
    conn, tx = await _held(engine, cid)
    tasks = []
    try:
        for n, request in enumerate(requests, 1):
            tasks.append(asyncio.create_task(request()))
            await _blocked(engine, n)
    finally:
        await tx.commit()
        await conn.close()
    return await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)


def _one_refused(responses, key: str):
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 422], [r.text for r in responses]
    refused = next(r for r in responses if r.status_code == 422)
    assert refused.json()["detail"]["message_key"] == key, refused.text
    return refused


async def _fund_balance(engine, cid, fund, account="1111"):
    from celerp.services.journal_accounts import fund_account_balances
    async with maker(engine)() as s:
        return (await fund_account_balances(s, cid)).get(fund, {}).get(account, 0)


async def test_concurrent_transfers_cannot_overdraw_fund(real_engine, real_client):
    """U1 holds 100 in the bank; two transfers of 70 to U2 race: one posts, the other is
    refused on the 30 left, and U1 never goes below zero."""
    b, f = await _books(real_engine, real_client)
    await b.entry(("1111", 100, 0, f["U1"]), ("4100", 0, 100, f["U1"]))
    transfer = lambda: b.movement("transfer", from_fund=f["U1"], to_fund=f["U2"], amount=70)  # noqa: E731
    refused = _one_refused(await _race(real_engine, b.cid, transfer, transfer),
                           "posting.fund.movement_exceeds_balance")
    assert refused.json()["detail"]["params"]["available"] == "30.00"
    assert await _fund_balance(real_engine, b.cid, f["U1"]) == 30
    assert await _fund_balance(real_engine, b.cid, f["U2"]) == 70


async def test_concurrent_releases_cannot_exceed_purpose_spending(real_engine, real_client):
    """R holds 100; one unrestricted purpose bill of 60. Two releases of 60 on that bill race:
    one posts, the other is refused as spending already released."""
    b, f = await _books(real_engine, real_client)
    await b.entry(("1111", 100, 0, f["R"]), ("4100", 0, 100, f["R"]))
    await b.entry(("1111", 100, 0, None), ("4100", 0, 100, None))
    bill = await b.entry(("6200", 60, 0, None), ("1111", 0, 60, None))
    release = lambda: b.movement("release", fund=f["R"], to_fund=None,  # noqa: E731
                                 backing=[{"je_id": bill, "line_index": 0, "amount": 60}])
    _one_refused(await _race(real_engine, b.cid, release, release), "posting.fund.release_backing_used")
    assert await _fund_balance(real_engine, b.cid, f["R"]) == 40


async def test_concurrent_reallocations_cannot_overdraw_fund(real_engine, real_client):
    """R holds 100; two authorized reallocations of 70 to R2 (same purpose) race: one posts,
    the other is refused on the 30 left."""
    b, f = await _books(real_engine, real_client)
    await b.entry(("1111", 100, 0, f["R"]), ("4100", 0, 100, f["R"]))
    reallocate = lambda: b.movement("reallocate", from_fund=f["R"], to_fund=f["R2"], amount=70,  # noqa: E731
                                    donor_authorization="Donor letter 4 March")
    refused = _one_refused(await _race(real_engine, b.cid, reallocate, reallocate),
                           "posting.fund.movement_exceeds_balance")
    assert refused.json()["detail"]["params"]["available"] == "30.00"
    assert await _fund_balance(real_engine, b.cid, f["R"]) == 30
    assert await _fund_balance(real_engine, b.cid, f["R2"]) == 70


async def test_concurrent_openings_cannot_exceed_balance(real_engine, real_client):
    """The unrestricted group holds 100 in the bank; two openings assigning 70 of it to R race:
    one posts, the other is refused on the 30 left."""
    b, f = await _books(real_engine, real_client)
    await b.entry(("1111", 100, 0, None), ("3200", 0, 100, None))
    opening = lambda: b.movement("opening", assignments=[  # noqa: E731
        {"account": "1111", "fund": f["R"], "amount": 70}])
    refused = _one_refused(await _race(real_engine, b.cid, opening, opening),
                           "posting.fund.movement_exceeds_balance")
    assert refused.json()["detail"]["params"]["available"] == "30.00"
    assert await _fund_balance(real_engine, b.cid, f["R"]) == 70


async def test_fund_lock_order_no_deadlock_with_concurrent_finalize(real_engine, real_client):
    """A fund transfer, an invoice finalize and a payment on an issued invoice, released
    together in each order: all three finish, none is aborted as a deadlock, and each
    posted its entry."""
    b, f = await _books(real_engine, real_client)
    await b.entry(("1111", 300, 0, f["U1"]), ("4100", 0, 300, f["U1"]))
    item = (await b.ok("/items", name="Workshop seat", sell_by="piece", inventory_type="service", quantity=0,
                       status="available"))["id"]

    async def invoice() -> str:
        doc = (await b.ok("/docs", doc_type="invoice", ref_id=f"INV-{uuid.uuid4().hex[:6]}", total=40,
                          line_items=[{"item_id": item, "name": "Workshop seat", "quantity": 1,
                                       "unit_price": 40, "line_total": 40}]))["id"]
        return doc

    orders = [("transfer", "finalize", "payment"), ("finalize", "payment", "transfer"),
              ("payment", "transfer", "finalize")]
    for order in orders:
        draft, issued = await invoice(), await invoice()
        await b.ok(f"/docs/{issued}/finalize")
        requests = {
            "transfer": lambda: b.movement("transfer", from_fund=f["U1"], to_fund=f["U2"], amount=10),
            "finalize": lambda: b.post(f"/docs/{draft}/finalize"),
            "payment": lambda: b.post(f"/docs/{issued}/payment", amount=40, payment_date=DATE, bank_account="1111",
                                      idempotency_key=uuid.uuid4().hex),
        }
        responses = await _race(real_engine, b.cid, *(requests[name] for name in order))
        assert [r.status_code for r in responses] == [200, 200, 200], (order, [r.text for r in responses])
    assert await _fund_balance(real_engine, b.cid, f["U2"]) == 10 * len(orders)


async def _gift_invoice(b: _Books, f: dict, gift: float, ticket: float) -> str:
    """An issued invoice: a gift line of ``gift`` for R and a ticket line of ``ticket`` with no fund."""
    item = (await b.ok("/items", name=f"Seat {uuid.uuid4().hex[:4]}", sell_by="piece", inventory_type="service",
                       quantity=0, status="available"))["id"]
    doc = (await b.ok("/docs", doc_type="invoice", ref_id=f"INV-{uuid.uuid4().hex[:6]}", total=gift + ticket,
                      subtotal=gift + ticket, line_items=[
                          {"item_id": item, "name": "Gift", "quantity": 1, "unit_price": gift, "line_total": gift,
                           "fund_id": f["R"]},
                          {"item_id": item, "name": "Ticket", "quantity": 1, "unit_price": ticket,
                           "line_total": ticket}]))["id"]
    await b.ok(f"/docs/{doc}/finalize")
    return doc


async def test_concurrent_partial_receipts_respect_open_fund_shares(real_engine, real_client):
    """An invoice open R 100 and unrestricted 50; two receipts of 70 allocated to R race:
    one posts, the other is refused on the 30 R has left open."""
    b, f = await _books(real_engine, real_client)
    doc = await _gift_invoice(b, f, 100, 50)
    receipt = lambda: b.post(f"/docs/{doc}/payment", amount=70, payment_date=DATE, bank_account="1111",  # noqa: E731
                             idempotency_key=uuid.uuid4().hex, fund_allocation={f["R"]: 70})
    refused = _one_refused(await _race(real_engine, b.cid, receipt, receipt), "posting.fund.allocation_exceeds_open")
    assert refused.json()["detail"]["params"]["open"] == "30.00"
    assert await _fund_balance(real_engine, b.cid, f["R"]) == 70


async def test_concurrent_note_applications_respect_open_fund_shares(real_engine, real_client):
    """An invoice open R 20 and unrestricted 80, a credit note of R 50; two applications of
    15 race: one posts, the other is refused on the 5 of R the invoice has left open."""
    b, f = await _books(real_engine, real_client)
    doc = await _gift_invoice(b, f, 20, 80)
    item = (await b.ok("/items", name="Gift back", sell_by="piece", inventory_type="service", quantity=0,
                       status="available"))["id"]
    note = (await b.ok("/docs", doc_type="credit_note", ref_id=f"CN-{uuid.uuid4().hex[:6]}", total=50, subtotal=50,
                       line_items=[{"item_id": item, "name": "Gift", "quantity": 1, "unit_price": 50,
                                    "line_total": 50, "fund_id": f["R"]}]))["id"]
    await b.ok(f"/docs/{note}/finalize")
    apply = lambda: b.post(f"/docs/{note}/apply-to-invoice", target_doc_id=doc, amount=15,  # noqa: E731
                           idempotency_key=uuid.uuid4().hex)
    refused = _one_refused(await _race(real_engine, b.cid, apply, apply), "posting.fund.note_exceeds_open")
    assert refused.json()["detail"]["params"]["open"] == "5.00"
