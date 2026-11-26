# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Nothing moves onto or off a bank account whose label and entries disagree.

The rule lives where every transfer between two of the company's banks is built
(``transfer_entries``), for both accounts, so the first test calls it directly, as any
writer could; then one test per path that reaches it: a direct transfer (the assistant's
transfer is the same route), a reconciliation transfer in both directions and a cash
withdrawal. A correctly kept foreign account still posts, a repeat of a transfer made
before the label went wrong still answers, and an account whose history mixes
books-currency and foreign entries keeps its way out: voiding the foreign entries
brings the correction back."""
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


@pytest.mark.parametrize("side", ["from", "to"])
async def test_the_transfer_boundary_refuses_a_mislabelled_account_on_either_side(client, session, side):
    from celerp_accounting.models import BankAccount
    from celerp_accounting.routes import transfer_entries
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    good = await B.bank(client, co["h"], name="Home")
    rows = {str(b.id): b for b in (await session.execute(select(BankAccount).where(
        BankAccount.id.in_([uuid.UUID(bad["id"]), uuid.UUID(good["id"])])))).scalars()}
    src, dst = (rows[bad["id"]], rows[good["id"]]) if side == "from" else (rows[good["id"]], rows[bad["id"]])
    with pytest.raises(HTTPException) as e:
        await transfer_entries(session, uuid.UUID(co["company_id"]), src, dst, 50, DAY, rate=0.03, to_amount=1700)
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
    """A foreign entry posted onto a mislabelled account by a path outside the transfer
    boundary (a manual entry in the label's currency) mixes its history: the label can't
    be corrected while the foreign entry is there, and voiding that entry brings the correction back."""
    co = await B.company(client)
    bad = await _mislabelled(client, session, co, name="Old default")
    je = await fx_deposit(client, co["h"], bad, 3400, 0.0294, 99.96, day="2026-03-04")
    assert (await B.bank_row(client, co["h"], bad["id"]))["relabel_to"] is None
    r = await client.post(f"/accounting/journal-entries/{je}/void", headers=co["h"], json={})
    assert r.status_code == 200, r.text
    assert (await B.bank_row(client, co["h"], bad["id"]))["relabel_to"] == "USD"
    r = await client.patch(f"/accounting/bank-accounts/{bad['id']}", headers=co["h"], json={"currency": "USD"})
    assert r.status_code == 200, r.text
