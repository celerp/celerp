# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Nothing moves onto or off a bank account whose label and entries disagree.

The rule lives at the journal boundary every journal change passes (the accounting
module's ``guard_journal_change``, run by ``emit_event``), so the first test emits a journal
line onto such an account directly, as any writer could; then one test per door that writes
one: a direct transfer (the assistant's transfer is the same route), a reconciliation
transfer in both directions, a cash withdrawal, a manual journal (the assistant's confirmed
journal is the same route), a journal import and a payment of a foreign invoice into the
account. A correctly kept foreign account still posts, a repeat of a transfer made before the
label went wrong still answers, and an account whose history mixes books-currency and foreign
entries keeps its way out: voiding the foreign entries brings the correction back."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

import bank_statement_helpers as B
from test_reconciliation_history import deposit, fx_deposit
from ui.i18n import refusal_text, t

pytestmark = pytest.mark.asyncio

DAY = "2026-03-20"


def _want(currency="THB", base="USD") -> str:
    return t("acct.err_bank_mislabelled", currency=currency, base=base)


async def _relabel(session, bank: dict, currency: str) -> None:
    await session.execute(text("UPDATE bank_accounts SET currency = :c WHERE id = :b"),
                          {"c": currency, "b": bank["id"]})
    await session.commit()


async def _mislabelled(client, session, co, **kw) -> dict:
    """A bank whose postings carry books-currency amounts only, then labelled in another
    currency, as an install that seeded its default bank before the books' currency was
    set left it."""
    bk = await B.bank(client, co["h"], **kw)
    await deposit(client, co["h"], bk, "2026-03-02", 300.0)
    await _relabel(session, bk, "THB")
    return {**bk, "currency": "THB"}


async def _entries_on(session, co, code: str) -> int:
    return (await session.execute(text(
        "SELECT count(*) FROM projections WHERE company_id = :c AND entity_type = 'journal_entry' "
        "AND state::text LIKE :code"), {"c": co["company_id"], "code": f'%"account": "{code}"%'})).scalar()


async def _transfer(client, co, src, dst, **extra):
    return await client.post("/accounting/transfers", headers=co["h"], json={
        "idempotency_key": uuid.uuid4().hex, "from_bank_id": src["id"], "to_bank_id": dst["id"],
        "amount": 50.0, "date": DAY, **extra})


async def test_the_journal_boundary_refuses_a_line_on_a_mislabelled_account_from_any_writer(client, session):
    from celerp.events.engine import emit_event
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    with pytest.raises(HTTPException) as e:
        await emit_event(session, company_id=uuid.UUID(co["company_id"]), entity_id=f"je:{uuid.uuid4()}",
                         entity_type="journal_entry", event_type="acc.journal_entry.created",
                         data={"ts": DAY, "memo": "any writer", "status": "posted", "entries": [
                             {"account": bad["chart_account_code"], "debit": 30.0, "credit": 0.0},
                             {"account": "4100", "debit": 0.0, "credit": 30.0}]},
                         actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex,
                         metadata_={})
    assert e.value.status_code == 422 and refusal_text(e.value.detail) == _want()


@pytest.mark.parametrize("side", ["from", "to"])
async def test_a_direct_transfer_with_a_mislabelled_account_is_refused_and_writes_nothing(client, session, side):
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    good = await B.bank(client, co["h"], name="Home")
    before = await _entries_on(session, co, bad["chart_account_code"])
    r = await (_transfer(client, co, bad, good, rate=0.03) if side == "from"
               else _transfer(client, co, good, bad, to_amount=1700))
    assert r.status_code == 422 and refusal_text(r.json()["detail"]) == _want(), r.text
    assert await _entries_on(session, co, bad["chart_account_code"]) == before


@pytest.mark.parametrize("amount", ["-50.00", "50.00"])
async def test_a_reconciliation_transfer_to_or_from_a_mislabelled_account_is_refused(client, session, amount):
    co = await B.company(client)
    good = await B.bank(client, co["h"], name="Home")
    bad = await _mislabelled(client, session, co, name="Old default")
    _sid, lines = await B.lines_on(client, co, good["id"], [("2026-03-05", "TO SAVINGS", amount)],
                                   opening="0.00", closing=amount, start="2026-03-01", end="2026-03-31")
    r = await B.resolve(client, co, good["id"], decisions=[{
        "line_ids": [lines[0]["id"]], "kind": "transfer", "target_bank_id": bad["id"], "to_amount": 1700.0}])
    res = r.json()["results"][0]
    assert res["ok"] is False and refusal_text(res["reason"]) == _want(), r.text


async def test_a_cash_withdrawal_into_a_mislabelled_cash_account_is_refused(client, session):
    co = await B.company(client)
    good = await B.bank(client, co["h"], name="Home")
    await _mislabelled(client, session, co, name="Petty cash", bank_type="cash")
    _sid, lines = await B.lines_on(client, co, good["id"], [("2026-03-05", "ATM WITHDRAWAL", "-200.00")],
                                   opening="0.00", closing="-200.00", start="2026-03-01", end="2026-03-31")
    r = await B.resolve(client, co, good["id"], decisions=[{"line_ids": [lines[0]["id"]], "kind": "cash",
                                                            "to_amount": 6800.0}])
    res = r.json()["results"][0]
    assert res["ok"] is False and refusal_text(res["reason"]) == _want(), r.text


async def test_a_correctly_kept_foreign_account_still_transfers_both_ways(client, session):
    co = await B.company(client)
    thb = await B.bank(client, co["h"], name="Baht", currency="THB")
    await fx_deposit(client, co["h"], thb, 3400, 0.0294, 99.96, day="2026-03-02")
    home = await B.bank(client, co["h"], name="Home")
    assert (await _transfer(client, co, thb, home, rate=0.0294)).status_code == 200
    assert (await _transfer(client, co, home, thb, to_amount=1700)).status_code == 200
    assert (await B.bank_row(client, co["h"], thb["id"]))["relabel_to"] is None


async def test_a_repeat_of_a_transfer_made_before_the_label_went_wrong_still_answers(client, session):
    co = await B.company(client)
    src = await B.bank(client, co["h"], name="Old default")
    await deposit(client, co["h"], src, "2026-03-02", 300.0)
    home = await B.bank(client, co["h"], name="Home")
    body = {"idempotency_key": uuid.uuid4().hex, "from_bank_id": src["id"], "to_bank_id": home["id"],
            "amount": 50.0, "date": DAY}
    first = await client.post("/accounting/transfers", headers=co["h"], json=body)
    assert first.status_code == 200, first.text
    await _relabel(session, src, "THB")
    again = await client.post("/accounting/transfers", headers=co["h"], json=body)
    assert again.status_code == 200 and again.json()["je_id"] == first.json()["je_id"], again.text


async def test_a_mixed_history_account_keeps_its_way_out(client, session):
    """A foreign entry posted before the label went wrong mixes the account's history: the
    label can't be corrected while the foreign entry is there, and voiding that entry (a removal
    the boundary lets through) brings the correction back."""
    co = await B.company(client)
    bad = await B.bank(client, co["h"], name="Old default")
    await deposit(client, co["h"], bad, "2026-03-02", 300.0)
    je = await fx_deposit(client, co["h"], {**bad, "currency": "THB"}, 3400, 0.0294, 99.96, day="2026-03-04")
    await _relabel(session, bad, "THB")
    assert (await B.bank_row(client, co["h"], bad["id"]))["relabel_to"] is None
    r = await client.post(f"/accounting/journal-entries/{je}/void", headers=co["h"], json={})
    assert r.status_code == 200, r.text
    assert (await B.bank_row(client, co["h"], bad["id"]))["relabel_to"] == "USD"
    r = await client.patch(f"/accounting/bank-accounts/{bad['id']}", headers=co["h"], json={"currency": "USD"})
    assert r.status_code == 200, r.text


def _thb_line(bad: dict) -> dict:
    return {"account": bad["chart_account_code"], "debit": 30.0, "credit": 0.0,
            "fx_debit": 1000.0, "fx_credit": None, "fx_currency": "THB", "fx_rate": 0.03}


async def test_a_manual_journal_onto_a_mislabelled_account_is_refused_and_the_correction_stays(client, session):
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": DAY, "memo": "baht", "idempotency_token": uuid.uuid4().hex, "entries": [
            {"account": bad["chart_account_code"], "debit": 1000, "credit": 0, "currency": "THB", "rate": 0.03},
            {"account": "4100", "debit": 0, "credit": 30}]})
    assert r.status_code == 422 and refusal_text(r.json()["detail"]) == _want(), r.text
    r = await client.patch(f"/accounting/bank-accounts/{bad['id']}", headers=co["h"], json={"currency": "USD"})
    assert r.status_code == 200, r.text


async def test_a_journal_import_onto_a_mislabelled_account_is_refused(client, session):
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    before = await _entries_on(session, co, bad["chart_account_code"])
    r = await client.post("/accounting/import/batch", headers=co["h"], json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": {"status": "posted", "ts": DAY, "memo": "import", "entries": [
            _thb_line(bad), {"account": "4100", "debit": 0.0, "credit": 30.0}]}}]})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 0 and _want() in str(result["errors"]), result
    assert await _entries_on(session, co, bad["chart_account_code"]) == before


async def test_a_payment_of_a_foreign_invoice_into_a_mislabelled_account_is_refused(client, session):
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    r = await client.post("/docs", headers=co["h"], json={
        "doc_type": "invoice", "currency": "THB", "conversion_rate": 0.03, "total": 1000.0,
        "line_items": [{"name": "X", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    assert (await client.post(f"/docs/{inv}/finalize", headers=co["h"])).status_code == 200
    before = await _entries_on(session, co, bad["chart_account_code"])
    r = await client.post(f"/docs/{inv}/payment", headers=co["h"], json={
        "amount": 1000.0, "payment_date": DAY, "conversion_rate": 0.03, "bank_account": bad["chart_account_code"]})
    assert r.status_code == 422 and refusal_text(r.json()["detail"]) == _want(), r.text
    assert await _entries_on(session, co, bad["chart_account_code"]) == before
