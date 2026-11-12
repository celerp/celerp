# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A members' club from its first renewal to the donor statement, through the API the pages use.

Initial conditions: a fresh company with the seeded chart and the Nonprofit preset applied (its
Donation category counts as gifts), funds turned on with a restricted fund (Library gift), two
board-designated funds and the restricted net assets account (test_fund_movements._setup). The
business day is frozen per step. Opening balances: 1000.00 in the bank against opening equity,
400.00 of it assigned to the restricted fund.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from celerp.services.journal_accounts import fund_account_balances
from test_credit_note_settlement_owned import _post
from test_fund_movements import _setup
from test_income_accounts import _issued, _item, _line
from test_revenue_recognition import _freeze

pytestmark = pytest.mark.asyncio

BANK = "1111"
DATE = "2026-03-02"


async def _held(session, auth, fund, account=BANK) -> Decimal:
    return (await fund_account_balances(session, auth["company_id"])).get(fund, {}).get(account, Decimal(0))


async def _club(client, auth) -> dict:
    """The preset, funds on, the opening balances by fund, and the items the club sells."""
    h = auth["headers"]
    r = await client.post("/companies/me/apply-preset", headers=h, params={"vertical": "nonprofit"})
    assert r.status_code == 200, r.text
    f = await _setup(client, auth)
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-01-01", "memo": "Opening balances", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": BANK, "debit": 1000.0, "credit": 0.0},
                    {"account": "3200", "debit": 0.0, "credit": 1000.0}]})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/funds/opening", headers=h, json={
        "request_key": uuid.uuid4().hex, "date": "2026-01-01", "reason": "Opening balances by fund",
        "assignments": [{"account": BANK, "fund": f["R"], "amount": "400"}]})
    assert r.status_code == 200, r.text
    f["gift"] = await _item(client, auth, "Library gift", category="donation")
    f["ticket"] = await _item(client, auth, "Gala ticket", category="event_ticket")
    f["dues"] = await _item(client, auth, "Annual membership", category="membership")
    return f


async def _pay(client, auth, doc: str, amount: float, day: str = DATE):
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": day, "bank_account": BANK, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


async def _member(client, auth, name: str, email: str) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": name, "email": email})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_e2e1_renewal_receipts_mixed_invoice_and_refund(client, session, auth, monkeypatch):
    """Stage 1: opening balances sit on their funds; a member's annual renewal falls due and
    Generate due bills it; a partial bank receipt leaves the rest open and a second receipt
    settles it; a mixed invoice of a restricted gift and a gala ticket posts each line to its own
    fund; a second gift refunded in full gives its money back from the restricted fund, and the
    donor's history counts the kept gift only."""
    h = auth["headers"]
    _freeze(monkeypatch, "2026-01-02")
    f = await _club(client, auth)
    assert await _held(session, auth, f["R"]) == 400 and await _held(session, auth, None) == 600

    member = await _member(client, auth, "Ada Member", "ada@club.test")
    r = await client.post("/docs", headers=h, json={
        "doc_type": "subscription_invoice", "kind": "membership", "contact_id": member,
        "frequency": "annually", "billing_timing": "advance", "start_date": "2026-03-01",
        "price_list": "Retail", "line_items": [_line(f["dues"], "Annual membership", 120.0)]})
    assert r.status_code in (200, 201), r.text
    agreement = r.json()["id"]
    assert (await _post(client, auth, f"/subscriptions/{agreement}/activate")).status_code == 200

    _freeze(monkeypatch, DATE)
    r = await _post(client, auth, f"/subscriptions/{agreement}/generate-due", {"request_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    renewal = r.json()["doc_id"]
    _freeze(monkeypatch, "2026-03-03")
    await _pay(client, auth, renewal, 50.0, "2026-03-03")
    doc = (await client.get(f"/docs/{renewal}", headers=h)).json()
    assert (doc["status"], float(doc["amount_outstanding"])) == ("partial", 70.0), doc
    await _pay(client, auth, renewal, 70.0, "2026-03-03")
    doc = (await client.get(f"/docs/{renewal}", headers=h)).json()
    assert (doc["status"], float(doc["amount_outstanding"])) == ("paid", 0.0), doc
    assert await _held(session, auth, None) == 720

    donor = await _member(client, auth, "Bea Donor", "bea@club.test")
    mixed = await _issued(client, auth, "invoice", [
        _line(f["gift"], "Library gift", 300.0, fund_id=f["R"]), _line(f["ticket"], "Gala ticket", 80.0)],
        contact_id=donor, issue_date="2026-03-03", price_list="Retail", subtotal=380.0, total=380.0)
    await _pay(client, auth, mixed, 380.0, "2026-03-03")
    assert await _held(session, auth, f["R"]) == 700 and await _held(session, auth, None) == 800

    gift = await _issued(client, auth, "invoice", [_line(f["gift"], "Library gift", 50.0, fund_id=f["R"])],
                         contact_id=donor, issue_date="2026-03-03", price_list="Retail", subtotal=50.0, total=50.0)
    await _pay(client, auth, gift, 50.0, "2026-03-03")
    assert await _held(session, auth, f["R"]) == 750
    r = await client.post(f"/docs/{gift}/refund", headers=h, json={
        "payment_index": 0, "amount": 50.0, "payment_date": "2026-03-03", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == 700 and await _held(session, auth, None) == 800
    r = await client.get(f"/donors/{donor}/history", headers=h, params={"from": "2026-03-01", "to": "2026-03-31"})
    assert r.status_code == 200, r.text
    assert float(r.json()["total"]) == 300.0


async def _paid_bill(client, auth, name: str, amount: float, fund: str | None) -> str:
    """A purpose bill finalized and paid from the bank; returns its posting's id."""
    line = {**_line(None, name, amount), "receive_as": "expense", **({"fund_id": fund} if fund else {})}
    bill = await _issued(client, auth, "bill", [line], issue_date=DATE, subtotal=amount, total=amount)
    await _pay(client, auth, bill, amount)
    return f"je:auto:{bill}:bill"


async def _expense_line(session, auth, je_id: str) -> int:
    from celerp.models.projections import Projection
    session.expire_all()
    entries = (await session.get(Projection, (auth["company_id"], je_id))).state["entries"]
    return next(i for i, e in enumerate(entries) if (e.get("debit") or 0) > 0)


async def _fund_post(client, auth, path: str, **body):
    return await client.post(f"/accounting/funds/{path}", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "date": DATE, "reason": "Board minute 12", **body})


def _key(r) -> str:
    return r.json()["detail"]["message_key"]


async def test_e2e1_spending_release_transfer_year_close_and_donor_statement(client, session, auth, monkeypatch):
    """Stage 2: library books charged to the restricted fund leave nothing to release; books
    paid from unrestricted money back one release and a second release on the same bill is
    refused; a manual entry moving only net assets between funds is refused; moving the bank's
    money to a second bank keeps each fund's share; the year closes, reverses and closes again
    with each fund's results on its own net assets; the fund reconciliation is clean; the donor
    statement previews and sends; and every fund on the fund balance sheet matches the ledger."""
    from test_donors import _mailbox

    h = auth["headers"]
    _freeze(monkeypatch, DATE)
    f = await _club(client, auth)
    donor = await _member(client, auth, "Bea Donor", "bea@club.test")
    quiet = await _member(client, auth, "Cy Donor", "cy@club.test")
    for who, amount in ((donor, 300.0), (quiet, 20.0)):
        gift = await _issued(client, auth, "invoice", [_line(f["gift"], "Library gift", amount, fund_id=f["R"])],
                             contact_id=who, issue_date=DATE, price_list="Retail", subtotal=amount, total=amount)
        await _pay(client, auth, gift, amount)
    assert await _held(session, auth, f["R"]) == 720

    charged = await _paid_bill(client, auth, "Library books", 120.0, f["R"])
    r = await _fund_post(client, auth, "release", fund=f["R"], to_fund=None, backing=[
        {"je_id": charged, "line_index": await _expense_line(session, auth, charged), "amount": "120"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.release_backing_charged", r.text
    assert await _held(session, auth, f["R"]) == 600

    spent = await _paid_bill(client, auth, "Library shelving", 200.0, None)
    items = (await client.get("/accounting/funds/release-spending", headers=h)).json()["items"]
    assert [(i["je_id"], i["left"]) for i in items] == [(spent, 200)]
    backing = [{"je_id": spent, "line_index": items[0]["line_index"], "amount": "200"}]
    r = await _fund_post(client, auth, "release", fund=f["R"], to_fund=None, backing=backing)
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == 400 and await _held(session, auth, None) == 600
    r = await _fund_post(client, auth, "release", fund=f["R"], to_fund=None, backing=backing)
    assert r.status_code == 422 and _key(r) == "posting.fund.release_backing_used", r.text

    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DATE, "memo": "Move net assets", "idempotency_token": uuid.uuid4().hex, "entries": [
            {"account": f["rna"], "debit": 100.0, "credit": 0.0, "fund_id": f["R"]},
            {"account": BANK, "debit": 0.0, "credit": 100.0, "fund_id": f["R"]},
            {"account": BANK, "debit": 100.0, "credit": 0.0},
            {"account": "3200", "debit": 0.0, "credit": 100.0}]})
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_outside", r.text
    assert await _held(session, auth, f["R"]) == 400

    async def bank(name, number, code=None):
        r = await client.post("/accounting/bank-accounts", headers=h, json={
            "bank_name": name, "account_number": number, "bank_type": "checking", "currency": "USD",
            "opening_balance": 0, **({"account_code": code} if code else {})})
        assert r.status_code == 200, r.text
        return r.json()

    main, savings = await bank("Harbor Bank", "0001", BANK), await bank("Coast Savings", "0002")
    r = await client.post("/accounting/transfers", headers=h, json={
        "from_bank_id": main["id"], "to_bank_id": savings["id"], "amount": 500.0, "date": DATE})
    assert r.status_code == 200, r.text
    to = savings["chart_account_code"]
    assert await _held(session, auth, f["R"], to) == 200 and await _held(session, auth, None, to) == 300
    assert await _held(session, auth, f["R"]) == 200 and await _held(session, auth, None) == 300

    def results():
        return fund_account_balances(session, auth["company_id"], as_of="2026-12-31")

    _freeze(monkeypatch, "2027-01-10")
    closed = await client.post("/accounting/close-year", headers=h, json={"fiscal_year_end": "2026-12-31"})
    assert closed.status_code == 200, closed.text
    after_close = await results()
    assert after_close[f["R"]].get(f["rna"]) == -400 and not any(
        v for k, v in after_close[f["R"]].items() if k.startswith(("4", "6")))
    assert (await client.post("/accounting/period-lock", headers=h, json={"lock_date": None})).status_code == 200
    r = await client.post("/accounting/close-year/2026-12-31/reverse", headers=h, json={"reason": "Late bills"})
    assert r.status_code == 200, r.text
    reopened = (await results())[f["R"]]
    assert (reopened.get(f["rna"]), reopened.get("4100")) == (-200, -320)
    reclosed = await client.post("/accounting/close-year", headers=h, json={"fiscal_year_end": "2026-12-31"})
    assert reclosed.status_code == 200 and reclosed.json()["je_id"] != closed.json()["je_id"], reclosed.text
    assert await results() == after_close

    recon = (await client.get("/accounting/funds/reconciliation", headers=h)).json()
    assert recon["balanced"] is True and recon["differences"] == [] and recon["overdrawn"] == []

    period = {"from": "2026-03-01", "to": "2026-03-31"}
    sent = _mailbox(monkeypatch)
    preview = (await client.get("/donors/statements/preview", headers=h, params=period)).json()["donors"]
    assert {row["contact_id"]: float(row["total"]) for row in preview} == {donor: 300.0, quiet: 20.0}
    r = await client.post("/donors/statements/send", headers=h, json={**period, "confirm": True})
    assert r.status_code == 200, r.text
    assert {row["state"] for row in r.json()["results"]} == {"sent"}
    assert sorted(m["to"] for m in sent) == ["bea@club.test", "cy@club.test"]
    history = (await client.get(f"/donors/{donor}/history", headers=h, params=period)).json()
    assert float(history["total"]) == 300.0

    sheet = (await client.get("/accounting/funds/balance-sheet", headers=h)).json()
    ledger = await fund_account_balances(session, auth["company_id"])
    for row in sheet["accounts"]:
        assert {fund: Decimal(str(v)) for fund, v in row["amounts"].items()} == {
            fund or "unrestricted": held[row["code"]] for fund, held in ledger.items() if held.get(row["code"])}
    net = {fund["id"]: fund["net_assets"] for fund in sheet["funds"]}
    assert (net[f["R"]], net["unrestricted"], net[f["U1"]]) == (400.0, 600.0, 0.0)


async def _holds(session, auth, fund) -> Decimal:
    """What ``fund`` holds: its assets less its liabilities."""
    held = (await fund_account_balances(session, auth["company_id"])).get(fund, {})
    return sum((v for code, v in held.items() if code[0] in "12"), Decimal(0))


async def test_e2e1_change_fund_moves_recognition_runs_under_the_fund_rule(client, session, auth, monkeypatch):
    """Stage 3: two imported reading programme invoices on the restricted gift recognize their
    income monthly (gifts themselves are never deferred); after six posted
    runs a bookkeeper without Reallocate restricted funds cannot move the first to another
    restricted fund of the same purpose and nothing is posted; the treasurer can, and the runs
    follow. With the gift's cash spent on its purpose, moving the second invoice to Unrestricted
    is refused until a new gift covers it, then its runs follow too. A Change fund reaching a
    run in a closed period is refused naming the lock date."""
    from test_change_fund import REASON, _change_doc, _standing
    from test_fund_movement_posting import _set_role
    from test_outside_lines import _issued_import
    from test_revenue_recognition import _deferred_item, _post_month, _schedules, _service

    h = auth["headers"]
    _freeze(monkeypatch, "2026-01-05")
    f = await _club(client, auth)
    r = await client.post("/accounting/funds", headers=h, json={
        "request_key": uuid.uuid4().hex, "name": "Reading room", "restricted": True, "purpose": "Scholarships"})
    assert r.status_code == 200, r.text
    room = r.json()["id"]
    plan = await _deferred_item(client, auth, "Reading programme")
    a, b = f"doc:IMP-{uuid.uuid4().hex[:8]}", f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=h, json={
        "records": [_issued_import(a, [_service(plan, 1460, end="2026-12-31", name="Reading programme")]),
                    _issued_import(b, [_service(plan, 600, end="2026-06-30", name="Reading programme")])],
        "fund_choices": {"all": f["R"]}})
    assert r.status_code == 200 and r.json()["created"] == 2, r.text

    _freeze(monkeypatch, "2026-07-05")
    for month in range(1, 7):
        assert (await _post_month(client, auth, f"2026-{month:02d}")).status_code == 200
    [schedule_a] = await _schedules(session, auth, a)
    deferred = schedule_a["deferred_account"]
    runs = await _standing(session, auth, "je:auto:rev:")
    assert runs == {(f["R"], deferred): Decimal("1324.00"), (f["R"], "4100"): Decimal("-1324.00")}, runs
    assert await _holds(session, auth, f["R"]) == 1724

    await _set_role(session, auth, "manager")
    await session.commit()
    r = await _change_doc(client, session, auth, a, document=room)
    assert r.status_code == 422 and _key(r) == "posting.fund.change_needs_reallocation", r.text
    assert (await _schedules(session, auth, a))[0]["fund_id"] == f["R"]
    assert await _standing(session, auth, "je:auto:rev:") == runs
    await _set_role(session, auth, "admin")
    await session.commit()
    r = await _change_doc(client, session, auth, a, document=room)
    assert r.status_code == 200, r.text
    assert (await _schedules(session, auth, a))[0]["fund_id"] == room
    assert await _standing(session, auth, "je:auto:rev:") == {
        (f["R"], deferred): Decimal("600.00"), (f["R"], "4100"): Decimal("-600.00"),
        (room, deferred): Decimal("724.00"), (room, "4100"): Decimal("-724.00")}
    assert await _holds(session, auth, f["R"]) == 1000 and await _holds(session, auth, room) == 724

    books = await _issued(client, auth, "bill", [{**_line(None, "Scholarship awards", 800), "receive_as": "expense",
                                                 "fund_id": f["R"]}], issue_date="2026-07-05", subtotal=800, total=800)
    await _pay(client, auth, books, 400.0, "2026-07-05")
    assert await _held(session, auth, f["R"]) == 0 and await _holds(session, auth, f["R"]) == 200
    r = await _change_doc(client, session, auth, b, document="unrestricted")
    assert r.status_code == 422 and _key(r) == "posting.fund.change_exceeds_balance", r.text
    assert (await _schedules(session, auth, b))[0]["fund_id"] == f["R"]

    donor = await _member(client, auth, "Bea Donor", "bea@club.test")
    gift = await _issued(client, auth, "invoice", [_line(f["gift"], "Library gift", 500.0, fund_id=f["R"])],
                         contact_id=donor, issue_date="2026-07-05", price_list="Retail", subtotal=500.0, total=500.0)
    await _pay(client, auth, gift, 500.0, "2026-07-05")
    r = await _change_doc(client, session, auth, b, document="unrestricted")
    assert r.status_code == 200, r.text
    assert (await _schedules(session, auth, b))[0]["fund_id"] is None
    standing = await _standing(session, auth, "je:auto:rev:")
    assert standing[(None, "4100")] == Decimal("-600.00") and (f["R"], "4100") not in standing
    assert await _holds(session, auth, f["R"]) == 100

    assert (await client.post("/accounting/period-lock", headers=h, json={"lock_date": "2026-06-30"})).status_code == 200
    r = await _change_doc(client, session, auth, a, document=f["R"], reason=REASON)
    assert r.status_code == 422 and _key(r) == "error.period_locked", r.text
    assert r.json()["detail"]["params"]["date"] == "2026-06-30"
    assert (await _schedules(session, auth, a))[0]["fund_id"] == room


async def test_e2e1_imports_take_their_funds_on_the_import(client, session, auth, monkeypatch):
    """Stage 5: what the club brings in from files. A journal import chosen Unrestricted for one
    record with two of its lines on the restricted fund posts each line on its choice, and the
    record left unchosen is rejected naming it while the rest posts. A document import with Set
    all on the restricted fund and its ticket line changed to Unrestricted posts the gift on the
    fund and the ticket on unrestricted money. An invoice in the file with header money only is
    refused until the import's Set all is chosen, then posts every leg on that fund."""
    from celerp.models.projections import Projection
    from test_income_accounts import _finalize_entry
    from test_outside_lines import _entry_state, _issued_import

    h = auth["headers"]
    _freeze(monkeypatch, DATE)
    f = await _club(client, auth)
    R = f["R"]

    def _journal(eid: str, entries: list[dict]) -> dict:
        return {"entity_id": eid, "event_type": "acc.journal_entry.created", "source": "csv",
                "idempotency_key": uuid.uuid4().hex,
                "data": {"status": "posted", "ts": DATE, "memo": "Gift box takings", "entries": entries}}

    chosen, left = f"je:{uuid.uuid4()}", f"je:{uuid.uuid4()}"
    entries = [{"account": BANK, "debit": 30.0, "credit": 0}, {"account": "4100", "debit": 0, "credit": 30.0},
               {"account": BANK, "debit": 20.0, "credit": 0}, {"account": "4100", "debit": 0, "credit": 20.0}]
    r = await client.post("/accounting/import/batch", headers=h, json={
        "records": [_journal(chosen, entries), _journal(left, entries[:2])],
        "fund_choices": {"records": {chosen: "unrestricted"}, "lines": {f"{chosen}:3": R, f"{chosen}:4": R}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert any(e.startswith(f"{left}: ") and "choose the fund for this document" in e for e in r.json()["errors"]), r.text
    state = await _entry_state(session, auth["company_id"], chosen)
    assert [(e.get("fund_id"), e.get("fund_choice")) for e in state["entries"]] == [
        (None, "unrestricted"), (None, "unrestricted"), (R, R), (R, R)]
    assert await session.get(Projection, {"company_id": auth["company_id"], "entity_id": left}) is None
    assert await _held(session, auth, R) == 420

    doc = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=h, json={
        "records": [_issued_import(doc, [_line(f["gift"], "Library gift", 40), _line(f["ticket"], "Gala ticket", 60)])],
        "fund_choices": {"all": R, "lines": {f"{doc}:2": "unrestricted"}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    entry = await _finalize_entry(session, auth, doc)
    assert sorted((e.get("fund_id") or "", e["credit"]) for e in entry["entries"] if e.get("credit")) == sorted(
        [(R, 40), ("", 60)])

    def _header_only(eid: str) -> dict:
        return {"entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
                "data": {"doc_type": "invoice", "status": "final", "ref_id": eid.split(":")[1], "issue_date": DATE,
                         "line_items": [], "shipping": 15, "total": 15, "import_treatment": "record_now"}}

    lineless = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=h, json={"records": [_header_only(lineless)]})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert any(e.startswith(f"{lineless}: ") and "choose the fund for this document" in e for e in r.json()["errors"])
    assert (await client.get(f"/docs/{lineless}", headers=h)).status_code == 404
    r = await client.post("/docs/import/batch", headers=h, json={
        "records": [_header_only(lineless)], "fund_choices": {"all": R}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert {e.get("fund_id") for e in (await _finalize_entry(session, auth, lineless))["entries"]} == {R}
