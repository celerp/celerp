# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Money settling a document stays with the funds its lines posted to (attribution rules S,
SM, P and V).

Initial conditions per test: a fresh company with the seeded chart, funds turned on, a
restricted fund and two board-designated (unrestricted) funds (test_fund_movements._setup),
service items made through POST /items. Invoices are finalized through POST /docs; payments,
refunds and note applications go through their routes; the posted entries are read from
their projections.
"""
from __future__ import annotations

import uuid

import pytest
from celerp.models.projections import Projection
from test_fund_legs import _by_fund
from test_fund_movements import _setup
from test_income_accounts import _issued, _item, _line, ui_client  # noqa: F401

pytestmark = pytest.mark.asyncio

BANK = "1111"


async def _entry(session, auth, je_id: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})
    assert row is not None and row.state.get("status") == "posted", je_id
    return row.state


def _account_by_fund(entries: list[dict], account: str) -> dict:
    out: dict = {}
    for e in entries:
        if e["account"] == account:
            out[e.get("fund_id")] = round(out.get(e.get("fund_id"), 0) + (e.get("debit") or 0) - (e.get("credit") or 0), 2)
    return out


async def _pay(client, auth, doc: str, amount: float, **extra):
    return await client.post(f"/docs/{doc}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": "2026-03-02", "bank_account": BANK,
        "idempotency_key": uuid.uuid4().hex, **extra})


async def _gift_and_ticket(client, auth, f, gift_amount=100, ticket_amount=50, **extra) -> str:
    gift, ticket = await _item(client, auth, "Gift"), await _item(client, auth, "Ticket")
    total = gift_amount + ticket_amount
    return await _issued(client, auth, "invoice", [
        _line(gift, "Gift", gift_amount, fund_id=f["R"]), _line(ticket, "Ticket", ticket_amount)],
        subtotal=total, total=total, **extra)


def _refusal(r, key: str) -> dict:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key, detail
    return detail["params"]


async def test_partial_receipt_fixed_rule_and_explicit_override(client, session, auth):
    """A partial receipt settles each fund's open share of the receivable pro rata; an
    explicit allocation by fund replaces the split and is recorded on the entry; an
    allocation that does not add up to the receipt or asks more of a fund than it has open
    is refused naming the fund."""
    f = await _setup(client, auth)
    doc = await _gift_and_ticket(client, auth, f)

    r = await _pay(client, auth, doc, 60)
    assert r.status_code == 200, r.text
    first = await _entry(session, auth, f"je:auto:{doc}:pay:0")
    assert _by_fund(first["entries"], "receivable") == {f["R"]: -40.0, None: -20.0}
    assert _account_by_fund(first["entries"], BANK) == {f["R"]: 40.0, None: 20.0}
    assert first["fund_allocation"] == {"rule": "open_share", "shares": {f["R"]: 40.0, "unrestricted": 20.0}}
    assert all(e.get("posted_from") == f"je:auto:{doc}:fin" for e in first["entries"])

    # Open now: R 60, unrestricted 30.
    params = _refusal(await _pay(client, auth, doc, 70, fund_allocation={f["R"]: 70}),
                      "posting.fund.allocation_exceeds_open")
    assert params["fund"] == "Library gift" and params["open"] == "60.00"
    _refusal(await _pay(client, auth, doc, 30, fund_allocation={f["R"]: 10}), "fund.payment_allocation_total")
    _refusal(await _pay(client, auth, doc, 30, fund_allocation={f["U1"]: 30}),
             "posting.fund.allocation_exceeds_open")

    r = await _pay(client, auth, doc, 30, fund_allocation={"unrestricted": 30})
    assert r.status_code == 200, r.text
    second = await _entry(session, auth, f"je:auto:{doc}:pay:1")
    assert _by_fund(second["entries"], "receivable") == {None: -30.0}
    assert second["fund_allocation"] == {"rule": "explicit", "shares": {"unrestricted": 30.0}}

    # The rest is all R's: the fixed rule takes it from R alone.
    r = await _pay(client, auth, doc, 60)
    assert r.status_code == 200, r.text
    third = await _entry(session, auth, f"je:auto:{doc}:pay:2")
    assert _by_fund(third["entries"], "receivable") == {f["R"]: -60.0}


async def test_fx_and_fee_lines_follow_receipt_fund_split(client, session, auth):
    """A foreign-currency receipt at a rate other than the invoice's books its exchange
    difference fund by fund as the receipt splits; the processor fees of that receipt
    split the same way."""
    from datetime import datetime, timezone

    from celerp.services import auto_je

    f = await _setup(client, auth)
    doc = await _gift_and_ticket(client, auth, f, currency="EUR", conversion_rate=1.10)
    r = await _pay(client, auth, doc, 150, conversion_rate=1.20)
    assert r.status_code == 200, r.text
    receipt = await _entry(session, auth, f"je:auto:{doc}:pay:0")
    assert _by_fund(receipt["entries"], "receivable") == {f["R"]: -110.0, None: -55.0}
    assert _by_fund(receipt["entries"], "fx_gain") == {f["R"]: -10.0, None: -5.0}

    session.expire_all()
    payment = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})).state["payments"][0]
    await auto_je.create_for_online_fees(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id=doc, reference="pi_fund",
        payment=payment, fees={"version": 1, "currency": "eur", "transaction_id": f"txn_{doc}",
                               "total_minor": 300, "processor_minor": 300, "platform_minor": 0, "other_minor": 0},
        occurred_at=datetime(2026, 3, 3, 12, tzinfo=timezone.utc))
    await session.commit()
    rows = [row for row in (await session.execute(Projection.__table__.select().where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.startswith(f"je:auto:{doc}:online-fees:")))).all()]
    assert len(rows) == 1
    fees = rows[0].state["entries"]
    assert _by_fund(fees, "online_payment_fees") == {f["R"]: 2.4, None: 1.2}
    assert _account_by_fund(fees, BANK) == {f["R"]: -2.4, None: -1.2}


async def test_refund_and_payment_void_keep_receipt_fund(client, session, auth):
    """A refund (or a chargeback recorded as one) and a payment void give back each fund's
    share of the receipt they reverse, read from the receipt's own entry."""
    f = await _setup(client, auth)
    doc = await _gift_and_ticket(client, auth, f)
    assert (await _pay(client, auth, doc, 90)).status_code == 200  # R 60, unrestricted 30
    r = await client.post(f"/docs/{doc}/refund", headers=auth["headers"], json={
        "payment_index": 0, "amount": 30, "payment_date": "2026-03-04", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    session.expire_all()
    rows = (await session.execute(Projection.__table__.select().where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.startswith(f"je:auto:{doc}:payrefund:")))).all()
    assert len(rows) == 1
    refund = rows[0].state["entries"]
    assert _by_fund(refund, "receivable") == {f["R"]: 20.0, None: 10.0}
    assert _account_by_fund(refund, BANK) == {f["R"]: -20.0, None: -10.0}
    assert all(e.get("posted_from") == f"je:auto:{doc}:pay:0" for e in refund)

    r = await client.post(f"/docs/{doc}/void-payment", headers=auth["headers"], json={
        "payment_index": 0, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    session.expire_all()
    rows = (await session.execute(Projection.__table__.select().where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.startswith(f"je:auto:{doc}:payvoid:")))).all()
    assert len(rows) == 1
    assert _by_fund(rows[0].state["entries"], "receivable") == {f["R"]: 40.0, None: 20.0}


async def test_note_application_matched_by_fund(client, session, auth):
    """A credit note of R 50 applied to an invoice open U 80 / R 20: at most 20 applies,
    all of it R's; more is refused naming the fund. The note never moves value between
    funds."""
    f = await _setup(client, auth)
    gift, ticket = await _item(client, auth, "Gift"), await _item(client, auth, "Ticket")
    invoice = await _issued(client, auth, "invoice", [
        _line(ticket, "Ticket", 80), _line(gift, "Gift", 20, fund_id=f["R"])], subtotal=100, total=100)
    note = await _issued(client, auth, "credit_note", [_line(gift, "Gift", 50, fund_id=f["R"])],
                         subtotal=50, total=50)

    r = await client.post(f"/docs/{note}/apply-to-invoice", headers=auth["headers"], json={
        "target_doc_id": invoice, "amount": 30, "idempotency_key": uuid.uuid4().hex})
    params = _refusal(r, "posting.fund.note_exceeds_open")
    assert params["fund"] == "Library gift" and params["open"] == "20.00"

    r = await client.post(f"/docs/{note}/apply-to-invoice", headers=auth["headers"], json={
        "target_doc_id": invoice, "amount": 20, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    session.expire_all()
    rows = (await session.execute(Projection.__table__.select().where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.startswith(f"je:auto:{invoice}:cnapply:")))).all()
    assert len(rows) == 1
    entries = rows[0].state["entries"]
    assert {e.get("fund_id") for e in entries} == {f["R"]}
    assert sorted(e["posted_from"] for e in entries) == sorted([f"je:auto:{invoice}:fin", f"je:auto:{note}:fin"])

    # The invoice's R share is settled: a payment now settles only the unrestricted 80. The
    # application holds the invoice's payment index 0, so the receipt is index 1.
    r = await _pay(client, auth, invoice, 80)
    assert r.status_code == 200, r.text
    receipt = await _entry(session, auth, f"je:auto:{invoice}:pay:1")
    assert _by_fund(receipt["entries"], "receivable") == {None: -80.0}


async def test_accounting_system_document_takes_fund_choice(client, session, auth, monkeypatch, connector_modules_running):
    """An open QuickBooks invoice in a company with funds has no posted entry of its own: the
    fund-choices route accepts it, the standing default for the accounting system fills it
    at its first arrival tagged with that source, and once finalized here a local payment
    posts on the chosen fund by rule S."""
    import celerp.connectors.upsert as upsert
    from celerp.models.connector_config import ConnectorConfig
    from test_helpers import own_sessions_on

    f = await _setup(client, auth)
    session.add(ConnectorConfig(company_id=str(auth["company_id"]), connector="quickbooks"))
    await session.commit()
    monkeypatch.setattr("celerp.db.SessionLocal", await own_sessions_on(session))

    async def arrive(qid: str, outcome: str = "created", number: str = "INV") -> str:
        assert await upsert.upsert_invoice_from_quickbooks(str(auth["company_id"]), {
            "Id": qid, "DocNumber": f"{number}-{qid}", "Balance": 30.0, "TotalAmt": 30.0, "CurrencyRef": {"value": "USD"},
            "Line": [{"DetailType": "SalesItemLineDetail", "Amount": 30.0, "Description": "Service",
                      "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 30.0}}]}) == outcome
        doc = f"doc:quickbooks:invoice:{qid}"
        state = (await client.get(f"/docs/{doc}", headers=auth["headers"])).json()
        assert state["status"] == "open", state
        return doc

    before = await arrive(uuid.uuid4().hex[:8])
    state = (await client.get(f"/docs/{before}", headers=auth["headers"])).json()
    assert state.get("fund_choice") is None, state
    # An open document with no entry of its own waits for its choice until it has one.
    waiting = (await client.get("/accounting/funds/waiting", headers=auth["headers"])).json()
    assert (waiting["count"], waiting["documents"]) == (1, [before]), waiting
    r = await client.post(f"/docs/{before}/fund-choices", headers=auth["headers"],
                          json={"expected_version": state["version"], "document": f["U1"]})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/docs/{before}", headers=auth["headers"])).json()
    assert (state["fund_choice"], state.get("fund_choice_source")) == (f["U1"], "user"), state

    r = await client.put("/accounting/funds/connection-defaults", headers=auth["headers"], json={
        "origin": "accounting_system", "source_key": "quickbooks", "value": f["R"], "expected_version": 0,
        "request_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    doc = await arrive(uuid.uuid4().hex[:8])
    state = (await client.get(f"/docs/{doc}", headers=auth["headers"])).json()
    assert (state["fund_choice"], state["fund_choice_source"]) == (f["R"], "default:accounting_system:quickbooks"), state
    waiting = (await client.get("/accounting/funds/waiting", headers=auth["headers"])).json()
    assert waiting["count"] == 0, waiting
    # A later sync of an invoice already here keeps the choice it has.
    await arrive(before.rsplit(":", 1)[1], "updated", number="REF")
    state = (await client.get(f"/docs/{before}", headers=auth["headers"])).json()
    assert (state["fund_choice"], state["fund_choice_source"]) == (f["U1"], "user"), state
    session.expire_all()
    assert await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc}:fin"}) is None

    # An imported open invoice takes a payment once it is finalized here.
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    fin = await _entry(session, auth, f"je:auto:{doc}:fin")
    assert _by_fund(fin["entries"], "receivable") == {f["R"]: 30.0}, fin["entries"]
    r = await _pay(client, auth, doc, 30)
    assert r.status_code == 200, r.text
    paid = await _entry(session, auth, f"je:auto:{doc}:pay:0")
    assert _by_fund(paid["entries"], "receivable") == {f["R"]: -30.0}, paid["entries"]
    assert _account_by_fund(paid["entries"], BANK) == {f["R"]: 30.0}, paid["entries"]


async def test_doc_fund_open_lists_each_funds_open_part(client, auth):
    """An invoice's open balance by fund, as a receipt splits it: each fund's open part,
    shrinking as receipts settle it; nothing in books that don't use funds."""
    async def fund_open(doc):
        r = await client.get(f"/docs/{doc}/fund-open", headers=auth["headers"])
        assert r.status_code == 200, r.text
        return r.json()

    plain = await _issued(client, auth, "invoice", [_line(await _item(client, auth, "Plain"), "Plain", 40)],
                          subtotal=40, total=40)
    assert await fund_open(plain) == {"currency": "USD", "funds": []}

    f = await _setup(client, auth)
    doc = await _gift_and_ticket(client, auth, f)
    assert await fund_open(doc) == {"currency": "USD", "funds": [
        {"fund": f["R"], "name": "Library gift", "open": 100.0},
        {"fund": "unrestricted", "name": None, "open": 50.0}]}
    assert (await _pay(client, auth, doc, 60)).status_code == 200
    assert [(x["fund"], x["open"]) for x in (await fund_open(doc))["funds"]] == [(f["R"], 60.0), ("unrestricted", 30.0)]


async def test_payment_form_shows_split_by_fund_and_override():
    """With funds in use, an invoice's payment form shows each fund's open part and the rule
    that splits a payment by it, and offers Allocate by fund: one amount per fund, empty
    until the user fills it in, each hinting the share the rule would give."""
    from ui.components.encode import to_markup
    from ui.routes.documents import _payment_section

    doc = {"entity_id": "doc:1", "doc_type": "invoice", "status": "final", "currency": "USD",
           "total": 150.0, "amount_outstanding": 150.0, "payments": []}
    fund_open = {"currency": "USD", "funds": [{"fund": "fund:1", "name": "Library gift", "open": 100.0},
                                              {"fund": "unrestricted", "name": None, "open": 50.0}]}
    page = to_markup(_payment_section(doc, bank_accounts=[{"chart_account_code": BANK, "name": "Bank"}],
                                      fund_open=fund_open))
    assert "By fund" in page and "Library gift" in page and "100.00" in page and "50.00" in page
    assert "in proportion to what it has open" in page
    assert "Allocate by fund" in page
    assert 'name="fund_allocation:fund:1"' in page and 'name="fund_allocation:unrestricted"' in page
    assert 'placeholder="100.00"' in page and 'placeholder="50.00"' in page

    plain = to_markup(_payment_section(doc, bank_accounts=[{"chart_account_code": BANK, "name": "Bank"}]))
    assert "Allocate by fund" not in plain and "fund_allocation" not in plain


async def test_payment_form_sends_allocation_by_fund(ui_client):
    """Amounts typed under Allocate by fund reach the payment as its split by fund (an empty
    one counting as nothing); with every amount left empty the payment carries no split."""
    from unittest.mock import AsyncMock

    from test_income_accounts import _ui_api

    record = AsyncMock(return_value={"event_id": "e1"})
    form = {"amount": "60", "payment_date": "2026-03-02", "method": "transfer", "bank_account": BANK,
            "conversion_rate": "1", "reference": "", "idempotency_key": "k-1"}
    with _ui_api(record_payment=record):
        await ui_client.post("/docs/doc:1/payment", data=form | {
            "fund_allocation:fund:1": "45", "fund_allocation:unrestricted": ""})
    assert record.await_args.args[2]["fund_allocation"] == {"fund:1": "45", "unrestricted": "0"}

    with _ui_api(record_payment=record):
        await ui_client.post("/docs/doc:1/payment", data=form | {
            "fund_allocation:fund:1": "", "fund_allocation:unrestricted": " "})
    assert "fund_allocation" not in record.await_args.args[2]
