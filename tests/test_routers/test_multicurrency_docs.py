# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration tests for multi-currency document support."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    addr = f"fx-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={"company_name": "FX Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


async def _make_invoice(client, h, currency="USD", conversion_rate=35.0):
    """A 100.00 invoice. The rate is sent whenever one is given, including 1:
    whether an explicit rate is legitimate is what several of these tests are
    about, so the helper must not decide it."""
    payload = {
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": currency,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0,
        "tax": 0.0,
        "total": 100.0,
    }
    if conversion_rate is not None:
        payload["conversion_rate"] = conversion_rate
    r = await client.post("/docs", headers=h, json=payload)
    return r


async def _set_base_currency(client, h, currency: str) -> None:
    r = await client.patch("/companies/me", headers=h, json={"settings": {"currency": currency}})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_create_foreign_currency_invoice(client):
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    r = await _make_invoice(client, h, "USD", 35.0)
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc.get("currency") == "USD"
    assert float(doc.get("conversion_rate", 0)) == 35.0


# ---------------------------------------------------------------------------
# The conversion rate is validated at every door that can set it, because it
# multiplies every amount the document posts to the ledger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -35.0])
async def test_create_refuses_a_conversion_rate_that_is_not_a_rate(client, bad):
    """A rate of zero would post the foreign amount as though it were base
    currency; a negative one would post the mirror image of the document. Refused
    where the value enters, so neither is ever a stored fact."""
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    r = await _make_invoice(client, h, "USD", bad)
    assert r.status_code == 422, r.text
    assert "greater than zero" in r.text


@pytest.mark.asyncio
async def test_create_holds_the_conversion_rate_to_the_exchange_rate_ceiling(client):
    """A document's rate is stored at the same ceiling a journal line's rate and
    a payment's rate are: the journal shows a rate at twelve places, so a
    document kept finer than that posts a base amount no reader could reproduce
    from the rate in front of them."""
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    r = await _make_invoice(client, h, "USD", 0.00001117318355)
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=h)).json()
    # Half up at the thirteenth place: ...18355 -> ...184, not truncated.
    assert doc["conversion_rate"] == 0.000011173184


@pytest.mark.asyncio
async def test_patch_refuses_a_conversion_rate_that_is_not_a_rate(client):
    """The edit door needs its own guard: fields_changed is a free-form envelope,
    so nothing about the request body constrains what arrives in it."""
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")
    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"conversion_rate": {"old": 35.0, "new": -35.0}}})
    assert r.status_code == 422, r.text
    assert "greater than zero" in r.text

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc["conversion_rate"] == 35.0, "the rejected edit must not have landed"


@pytest.mark.asyncio
async def test_patch_stores_the_conversion_rate_as_a_number_at_the_ceiling(client):
    """The inline rate field posts a form value, so the edit arrives as a string.

    One rate is one stored fact: it is stored as a number at the ceiling
    whichever door set it, not as a float from create and a string from an edit.
    """
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")
    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"conversion_rate": {"old": 35.0, "new": "0.00001117318355"}}})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc["conversion_rate"] == 0.000011173184
    assert isinstance(doc["conversion_rate"], float)


@pytest.mark.asyncio
async def test_patch_clears_the_conversion_rate_with_an_empty_value(client):
    """Clearing has to stay open: it is the remedy for a rate that should not be
    on the document at all, which finalization refuses to post."""
    token = await _register(client)
    h = _auth(token)
    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"conversion_rate": {"old": 35.0, "new": ""}}})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc.get("conversion_rate") is None
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_finalize_refuses_a_base_currency_doc_carrying_a_rate(client, session):
    """A document in the company's own currency converts at 1 by definition, so
    any other stored rate restates it: 100 USD booked as 3500 in USD books.

    Checked at finalization rather than on entry because that is the last point
    before the journal entry is minted, it is where the stored currency and the
    stored rate can be compared against each other, and the rate is immutable
    afterwards. The manual journal door already refuses the same mismatch.
    """
    token = await _register(client)
    h = _auth(token)  # base currency stays USD

    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]

    r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "USD" in detail and "1" in detail

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    assert rows == [], "nothing may post when the rate is refused"


@pytest.mark.asyncio
async def test_finalize_accepts_an_explicit_rate_of_one_on_a_base_currency_doc(client, session):
    """The guard must not over-block: 1 is the correct rate for a base-currency
    document, so an explicit 1 posts the document's own amounts unchanged."""
    token = await _register(client)
    h = _auth(token)

    doc_id = (await _make_invoice(client, h, "USD", 1.0)).json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    created = next((r for r in rows if r.event_type == "acc.journal_entry.created"), None)
    assert created is not None
    ar = next((e for e in created.data.get("entries", [])
               if e.get("account") == "1120" and e.get("debit", 0) > 0), None)
    assert ar is not None and abs(float(ar["debit"]) - 100.0) < 0.01


async def _net_by_account(session, doc_id: str) -> dict[str, float]:
    """Debits less credits per account, across every auto JE this document posted."""
    rows = (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.entity_id.like(f"je:auto:{doc_id}%"),
            LedgerEntry.event_type == "acc.journal_entry.created",
        )
    )).scalars().all()
    net: dict[str, float] = {}
    for row in rows:
        for e in row.data.get("entries", []):
            net[e["account"]] = round(
                net.get(e["account"], 0.0) + float(e.get("debit") or 0) - float(e.get("credit") or 0), 2)
    return net


@pytest.mark.asyncio
async def test_voiding_a_payment_reverses_it_at_the_rate_it_posted_at(client, session):
    """A reversal has to undo the numbers the posting it reverses actually made.

    A payment carries its own rate, because the rate moves between issuing a
    foreign-currency invoice and being paid for it. Reversing at the document's
    rate instead leaves the difference behind in both accounts the payment
    touched: the receivable is part relieved on a document that is back to
    unpaid, and the bank keeps money that was never banked.
    """
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")

    doc_id = (await _make_invoice(client, h, "USD", 35.0)).json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200

    r = await client.post(f"/docs/{doc_id}/payment", headers=h, json={
        "payment_date": "2026-01-15", "amount": 100.0,
        "bank_account": "1111", "conversion_rate": 36.0})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/void-payment", headers=h,
                          json={"payment_index": 0, "void_reason": "Wrong account"})
    assert r.status_code == 200, r.text

    net = await _net_by_account(session, doc_id)
    # Back to what finalization alone left: the receivable it raised, and a bank
    # account no cash ever reached.
    assert net.get("1111", 0.0) == 0.0
    assert net["1120"] == 3500.0
    assert net.get("6960", 0.0) == 0.0, "the exchange difference has to come back too"


# ---------------------------------------------------------------------------
# Settlement exchange differences.
#
# A receivable can only be cleared at the rate it was raised at, and cash can
# only be banked at the rate it actually converted at. When a document is paid
# at a different rate from the one it was issued at, the gap between those two
# is a realised exchange gain or loss. It belongs on the P&L, not left behind in
# the control account where nothing would ever flag it.
# ---------------------------------------------------------------------------


async def _foreign_invoice_paid_at(client, h, session, *, doc_rate: float, paid_at: float) -> dict[str, float]:
    """A USD 100 invoice raised at doc_rate in THB books, paid in full at paid_at."""
    await _set_base_currency(client, h, "THB")
    doc_id = (await _make_invoice(client, h, "USD", doc_rate)).json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{doc_id}/payment", headers=h, json={
        "payment_date": "2026-01-15", "amount": 100.0,
        "bank_account": "1111", "conversion_rate": paid_at})
    assert r.status_code == 200, r.text
    return await _net_by_account(session, doc_id)


@pytest.mark.asyncio
async def test_payment_above_the_document_rate_posts_the_gain(client, session):
    """Issued at 35, paid at 36: 3,600 arrived against a 3,500 receivable.

    The receivable clears exactly, and the 100 the company gained by being paid
    late at a better rate is a credit to the exchange difference account.
    """
    net = await _foreign_invoice_paid_at(client, _auth(await _register(client)), session,
                                         doc_rate=35.0, paid_at=36.0)
    assert net["1111"] == 3600.0
    assert net["1120"] == 0.0, "the receivable has to clear, with nothing left over"
    assert net["6960"] == -100.0, "a gain is a credit to the exchange account"


@pytest.mark.asyncio
async def test_payment_below_the_document_rate_posts_the_loss(client, session):
    """Issued at 35, paid at 34: only 3,400 arrived against a 3,500 receivable.

    The receivable still clears in full, because that is the amount it was
    raised at and the customer owes nothing more. The 100 shortfall is the
    company's loss on the rate, and it is a debit to the exchange account.
    """
    net = await _foreign_invoice_paid_at(client, _auth(await _register(client)), session,
                                         doc_rate=35.0, paid_at=34.0)
    assert net["1111"] == 3400.0
    assert net["1120"] == 0.0
    assert net["6960"] == 100.0, "a loss is a debit to the exchange account"


@pytest.mark.asyncio
async def test_payment_at_the_document_rate_posts_no_difference_line(client, session):
    """Nothing to recognise when the rates agree, so no line is written.

    An exchange difference of zero is not a difference. A line for it would put
    an account with no movement on the statement of every document ever paid.
    """
    net = await _foreign_invoice_paid_at(client, _auth(await _register(client)), session,
                                         doc_rate=35.0, paid_at=35.0)
    assert net["1111"] == 3500.0
    assert net["1120"] == 0.0
    assert "6960" not in net


@pytest.mark.asyncio
async def test_paying_a_bill_above_its_rate_posts_the_loss(client, session):
    """The payable side of the same rule, and the sign flips with it.

    A USD 200 bill raised at 35 owes 7,000. Settling it at 36 costs 7,200, so
    the payable clears at what it was raised at and the extra 200 the company
    had to find is its loss.
    """
    token = await _register(client)
    h = _auth(token)
    await _set_base_currency(client, h, "THB")
    r = await client.post("/docs", headers=h, json={
        "doc_type": "bill", "currency": "USD", "conversion_rate": 35.0, "total": 200.0,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": 200.0, "line_total": 200.0}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200

    r = await client.post(f"/docs/{doc_id}/payment", headers=h, json={
        "payment_date": "2026-01-15", "amount": 200.0,
        "bank_account": "1111", "conversion_rate": 36.0})
    assert r.status_code == 200, r.text

    net = await _net_by_account(session, doc_id)
    assert net["1111"] == -7200.0, "the cash that actually left the bank"
    assert net["2110"] == 0.0, "the payable has to clear, with nothing left over"
    assert net["6960"] == 200.0


@pytest.mark.asyncio
async def test_invalid_currency_code_rejected(client):
    token = await _register(client)
    h = _auth(token)

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "XYZ",
        "conversion_rate": 1.0,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0}],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_finalize_base_currency_doc_succeeds(client):
    """Regression: base-currency docs must finalize unchanged (no rate required)."""
    token = await _register(client)
    h = _auth(token)

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 200


@pytest.mark.asyncio
async def test_finalize_foreign_currency_without_rate_fails(client):
    token = await _register(client)
    h = _auth(token)

    # Set base currency to THB so USD docs are foreign
    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        # No conversion_rate
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 422
    assert "conversion rate" in finalize.json()["detail"].lower()


@pytest.mark.asyncio
async def test_finalize_with_rate_creates_base_currency_jes(client, session):
    """JE debit on AR account must equal total_usd * rate in base currency."""
    token = await _register(client)
    h = _auth(token)

    # Set base currency to THB
    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        "conversion_rate": 35.0,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc = r.json()
    doc_id = doc.get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 200, finalize.text

    # Read the auto-JE ledger entries for this doc
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()

    created_row = next(
        (row for row in rows if row.event_type == "acc.journal_entry.created"),
        None,
    )
    assert created_row is not None, "JE created event not found"
    entries = created_row.data.get("entries", [])
    ar_entry = next((e for e in entries if e.get("account") == "1120" and e.get("debit", 0) > 0), None)
    assert ar_entry is not None, "AR debit entry not found"
    # 100 USD * 35.0 = 3500 THB
    assert abs(float(ar_entry["debit"]) - 3500.0) < 0.01, f"Expected 3500, got {ar_entry['debit']}"


@pytest.mark.asyncio
async def test_finalize_with_rate_revenue_entry_in_base(client, session):
    """Revenue credit entry must also be in base currency."""
    token = await _register(client)
    h = _auth(token)

    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        "conversion_rate": 40.0,
        "line_items": [{"name": "Item", "quantity": 2, "unit_price": 50.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    await client.post(f"/docs/{doc_id}/finalize", headers=h)

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    created_row = next((row for row in rows if row.event_type == "acc.journal_entry.created"), None)
    assert created_row is not None
    entries = created_row.data.get("entries", [])
    rev_entry = next((e for e in entries if e.get("account") == "4100" and e.get("credit", 0) > 0), None)
    assert rev_entry is not None
    # 100 USD * 40 = 4000 THB
    assert abs(float(rev_entry["credit"]) - 4000.0) < 0.01, f"Expected 4000, got {rev_entry['credit']}"
