# CLASS money-in-the-wrong-currency (currency identity of a completed statement): no change
# of a bank account's currency, by any writer, reads a completed reconciliation again.
"""Every writer of an existing account's currency goes through ``set_bank_currency``. Over
accounts in each state the rule distinguishes, every target currency is tried: a refused
change writes nothing, and an allowed one leaves every completed reconciliation of the
account reading exactly as it did (book balance, difference, needs review, status)."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from sqlalchemy import text

from test_reconciliation_history import (
    admin, bank, completed, deposit, fx_deposit, history, legacy_deposit, legacy_session, start)
from ui.i18n import t

pytestmark = pytest.mark.asyncio

ROOT = pathlib.Path(__file__).resolve().parent.parent
_TARGETS = ("USD", "THB", "EUR", "GBP")


async def _row(session, bank_id) -> tuple:
    return tuple((await session.execute(text(
        "SELECT currency, relabelled_from FROM bank_accounts WHERE id = :b"), {"b": bank_id})).one())


async def _completed_reads(client, h, bank_id) -> dict:
    out = {}
    for item in (await history(client, h, bank_id)).json()["items"]:
        if item["status"] == "completed":
            r = (await client.get(f"/accounting/reconciliation/{item['id']}", headers=h)).json()
            out[item["id"]] = (r["book_balance"], r["difference"], item["needs_review"], r["status"])
    return out


async def _accounts(client, h, session) -> list[dict]:
    """One account per state the rule tells apart, each with a completed reconciliation."""
    books = await bank(client, h, opening=0.0, name="Books Bank", number="0001")
    je = await deposit(client, h, books, "2026-09-10", 100.0)
    await completed(client, h, books, "2026-09-30", 100.0, [je])

    foreign = await bank(client, h, currency="EUR", opening=0.0, name="Euro Bank", number="0002")
    fx = await fx_deposit(client, h, foreign, 100, 1.08, 108)
    await completed(client, h, foreign, "2026-09-30", 100.0, [fx])

    # Labelled THB with USD-only entries (the correction applies) and a completed
    # reconciliation from before the label was checked.
    mislabelled = await bank(client, h, currency="THB", opening=0.0, name="Baht Bank", number="0003")
    await legacy_deposit(client, h, session, mislabelled, "2026-09-10", 50.0)
    await legacy_session(session, mislabelled, "2026-09-30", balance=50.0)

    # Corrected to USD (its undo applies), then reconciled and completed.
    corrected = await bank(client, h, currency="THB", opening=0.0, name="Fixed Bank", number="0004")
    le = await legacy_deposit(client, h, session, corrected, "2026-09-10", 70.0)
    assert (await client.patch(f"/accounting/bank-accounts/{corrected['id']}", json={"currency": "USD"},
                               headers=h)).status_code == 200
    await completed(client, h, corrected, "2026-09-30", 70.0, [le])

    # Only a completed reconciliation, no posting at all.
    empty = await bank(client, h, opening=0.0, name="Empty Bank", number="0005")
    await completed(client, h, empty, "2026-09-30", 0.0)
    return [books, foreign, mislabelled, corrected, empty]


async def test_no_currency_change_reads_a_completed_reconciliation_again(client, session):
    _, h = await admin(client)
    for b in await _accounts(client, h, session):
        for target in _TARGETS:
            before_row, before = await _row(session, b["id"]), await _completed_reads(client, h, b["id"])
            r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": target}, headers=h)
            session.expire_all()
            if r.status_code == 409:
                assert await _row(session, b["id"]) == before_row, (b["bank_name"], target)
            else:
                assert r.status_code == 200, r.text
            assert await _completed_reads(client, h, b["id"]) == before, (b["bank_name"], target, r.text)


async def test_a_correction_held_by_a_completed_reconciliation_names_reopen_and_then_goes_through(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    je = await legacy_deposit(client, h, session, b, "2026-09-10", 50.0)
    sid = await legacy_session(session, b, "2026-09-30", balance=50.0, je_ids=[je])
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_bank_currency_reconciled", currency="THB"), r.text
    assert (await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)).status_code == 200
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"}, headers=h)
    assert r.status_code == 200 and r.json()["balance"] == 50.0, r.text
    assert (await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)).status_code == 200


async def test_books_currency_set_keeps_a_default_bank_a_completed_reconciliation_reads(client, session):
    """The books-currency hook goes through the same rule: the default bank with a completed
    reconciliation keeps its label when the books' currency is first set."""
    from celerp_accounting.routes import _DEFAULT_BANK_CODE, default_bank_follows_books_hook
    _, h = await admin(client)
    company_id, bank_id = (await session.execute(text(
        "SELECT company_id, id FROM bank_accounts WHERE chart_account_code = :c"), {"c": _DEFAULT_BANK_CODE})).one()
    assert (await start(client, h, str(bank_id), "2026-09-30", 0.0)).status_code == 200
    sid = (await history(client, h, str(bank_id))).json()["items"][0]["id"]
    assert (await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)).status_code == 200
    await session.execute(text("UPDATE bank_accounts SET currency = 'THB' WHERE id = :b"), {"b": bank_id})
    await session.commit()
    await default_bank_follows_books_hook(session=session, company_id=company_id)
    await session.commit()
    assert (await _row(session, bank_id))[0] == "THB"


async def test_every_writer_of_a_bank_currency_goes_through_the_rule():
    """Assigning an existing account's currency happens in set_bank_currency only: no other
    function in the accounting module assigns a ``.currency`` attribute, and no code writes
    the column with SQL. Creating an account (BankAccount(..., currency=...)) is not a change."""
    module = ROOT / "default_modules/celerp-accounting/celerp_accounting"
    writers = []
    for path in sorted(module.glob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
                    node, (ast.AugAssign, ast.AnnAssign)) else []
                for target in targets:
                    for leaf in target.elts if isinstance(target, ast.Tuple) else [target]:
                        if isinstance(leaf, ast.Attribute) and leaf.attr == "currency":
                            writers.append((path.name, fn.name))
    assert writers == [("routes.py", "set_bank_currency")], writers
    sql = [f"{p.relative_to(ROOT)}" for p in [*ROOT.glob("celerp/**/*.py"), *ROOT.glob("default_modules/**/*.py"),
                                              *ROOT.glob("ui/**/*.py")]
           if "/tests/" not in str(p) and re.search(r"UPDATE\s+bank_accounts\b", p.read_text(encoding="utf-8"), re.I)]
    assert not sql, sql
