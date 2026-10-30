# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
# CLASS money-in-the-wrong-currency (guard over every bank reader).
"""A bank account's journal lines have one reading in the account's own currency,
``_bank_amounts``, and everything that reads them for the account uses it.

Two halves. The first generates journal lines in EUR, JPY and VND (some too small to
show in the books' currency), and checks that the balance on the bank list is the
figure the reconciliation reaches, and that a completed reconciliation refuses any
change of a line's amount in the account's currency. The second reads the accounting
module's source for every function that reads a journal line's amounts and fails on
one this file has not reviewed, so a new reader is checked before it ships.
"""
from __future__ import annotations

import ast
import pathlib
import random
import uuid

import pytest

from celerp.services.money import currency_dp, round_money, to_stored_float
from test_recon_guard import _company, _created_data, _refused
from test_reconciliation_history import admin, bank, complete, match, start

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "default_modules" / "celerp-accounting" / "celerp_accounting"

# (currency, rate to USD books): VND at 0.000039 puts anything under VND 129 at USD 0.00.
CASES = [("EUR", 1.08), ("JPY", 0.0067), ("VND", 0.000039)]


def _lines(currency: str, seed: int) -> list[float]:
    """Signed amounts in *currency*, in its own minor units, from a fixed seed: some of
    them too small to show in the books' currency, money in and money out."""
    rng = random.Random(seed)
    step = 10 ** -currency_dp(currency)
    tiny = [rng.randint(1, 120) for _ in range(3)]
    larger = [rng.randint(1, 5_000_000) for _ in range(4)]
    signed = [a if rng.random() < 0.7 else -a for a in tiny + larger]
    return [to_stored_float(round_money(a * step, currency)) for a in signed]


async def _post(client, h, b: dict, amount: float, rate: float) -> str:
    """A bank posting in the account's currency on both sides, so it balances in the
    books' currency whatever the rounding."""
    side = {"debit": amount, "credit": 0} if amount > 0 else {"debit": 0, "credit": -amount}
    other = {"debit": side["credit"], "credit": side["debit"]}
    fx = {"currency": b["currency"], "rate": rate}
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-09-10", "memo": "Generated", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": b["chart_account_code"], **side, **fx}, {"account": "4100", **other, **fx}]})
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("je_id") or body["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("currency,rate", CASES)
async def test_balance_shown_is_the_reconciliation_figure_and_completed_lines_are_held(
        client, session, currency, rate, seed):
    _, h = await admin(client)
    b = await bank(client, h, currency=currency, opening=0)
    amounts = _lines(currency, seed)
    je_ids = [await _post(client, h, b, a, rate) for a in amounts]
    listed = next(x for x in (await client.get("/accounting/bank-accounts", headers=h)).json()["items"]
                  if x["id"] == b["id"])
    expected = to_stored_float(round_money(sum(amounts), currency))
    assert listed["balance"] == expected
    sid = (await start(client, h, b["id"], "2026-09-30", listed["balance"])).json()["id"]
    assert (await match(client, h, sid, je_ids)).status_code == 200
    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert recon["book_balance"] == listed["balance"] and recon["balanced"] is True, recon
    await complete(client, h, sid)

    cid = await _company(session, b)
    step = 10 ** -currency_dp(currency)
    for je in je_ids:
        data = await _created_data(session, cid, je)
        for key in ("fx_debit", "fx_credit"):
            changed = {**data, "entries": [
                {**line, key: to_stored_float(round_money((line.get(key) or 0) + step, currency))}
                if line["account"] == b["chart_account_code"] else line
                for line in data["entries"]]}
            await _refused(session, cid, je, "acc.journal_entry.created", changed)


# Every function in the accounting module that reads a journal line's amounts, and why
# it reads them as it does. Reading through ``_bank_amounts`` (or ``_bank_line_amounts``,
# which calls it) needs no entry: that is the bank-currency reading.
BOOKS_CURRENCY_READERS = {
    ("routes.py", "_line_amounts"): "the books-currency reading itself, under the one empty-line rule",
    ("routes.py", "_bank_amounts"): "the bank-currency reading itself",
    ("routes.py", "_build_balances"): "trial balance and balance sheet, books currency, every account",
    ("routes.py", "_journal_payload"): "the journal, books currency with each line's own currency beside it",
    ("routes.py", "create_manual_journal_entry"): "a writer validating the lines it is given",
    ("routes.py", "account_ledger"): "one account's ledger in books currency, with foreign amounts beside them",
    ("routes.py", "trial_balance"): "books currency, every account",
    ("routes.py", "general_ledger"): "books currency, every account",
    ("routes.py", "opening_tie_out"): "opening balances against the books, books currency",
    ("routes.py", "statement_of_account"): "a contact's statement, books currency",
    ("routes.py", "_carries_foreign_amount"): "whether a line carries a foreign amount, not how much",
    ("routes.py", "_bank_has_foreign_amounts"): "whether any line on the account carries a foreign amount, not how much",
    ("routes.py", "create_transfer"): "a writer reading back the line it just built",
    ("routes.py", "cash_flow"): "the cash flow statement, books currency over every cash account",
    ("routes.py", "matches"): "journal search matching a typed figure against books-currency amounts",
    ("routes.py", "_norm"): "retry comparison of a manual entry, base and foreign amounts both compared",
    ("migration_sink.py", "reconcile"): "migration totals against the source books, books currency",
}
_AMOUNTS = {"debit", "credit", "fx_debit", "fx_credit"}
_BANK_READING = {"_bank_amounts", "_bank_line_amounts"}


def _reads_line_amounts(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_line_amounts":
            return True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value in _AMOUNTS):
            return True
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value in _AMOUNTS
                and isinstance(n.ctx, ast.Load)):
            return True
    return False


def _line_readers() -> dict[tuple[str, str], bool]:
    """Every function reading a journal line's amounts: whether it reads them in the bank
    account's currency."""
    out = {}
    for path in sorted(MODULE.glob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _reads_line_amounts(fn):
                names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                out[(path.name, fn.name)] = bool(names & _BANK_READING)
    return out


def test_every_reader_of_journal_line_amounts_is_reviewed():
    readers = _line_readers()
    unreviewed = sorted(key for key, by_bank in readers.items() if not by_bank and key not in BOOKS_CURRENCY_READERS)
    assert not unreviewed, (
        "These read journal line amounts without _bank_amounts. A reader of a bank account's lines must "
        f"use it; a books-currency reader goes in BOOKS_CURRENCY_READERS with its reason: {unreviewed}")
    stale = sorted(set(BOOKS_CURRENCY_READERS) - {k for k, by_bank in readers.items() if not by_bank})
    assert not stale, f"No longer books-currency readers, remove them from the list: {stale}"
    assert {k for k, by_bank in readers.items() if by_bank} >= {
        ("routes.py", "_bank_dicts"), ("routes.py", "_je_entries_for_account"), ("recon_guard.py", "_bank_rows")}
