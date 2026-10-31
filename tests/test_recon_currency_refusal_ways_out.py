# CLASS money-in-the-wrong-currency (refusal side): every refusal about a bank account's
# currency offers a way out the user can take, and taking it gets the work done.
"""Each currency refusal on the bank and reconciliation path is listed in WAYS_OUT with a
check that provokes it, follows the remedy its text names, and finishes the job. A new
refusal of this kind that is not listed turns the registry test red."""
from __future__ import annotations

import json
import pathlib
import re
import uuid

import pytest

from test_reconciliation_history import (
    admin, bank, deposit, fx_deposit, legacy_deposit, legacy_session, match, post_je, start)
from test_reconciliation_reopen import _ui_as
from test_recon_writers_bank_currency import _eur_invoice
from ui.i18n import t

pytestmark = pytest.mark.asyncio

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SOURCES = (
    "default_modules/celerp-accounting/celerp_accounting/routes.py",
    "default_modules/celerp-accounting/celerp_accounting/recon_guard.py",
    "default_modules/celerp-docs/celerp_docs/routes.py",
    "celerp/services/auto_je.py",
    "ui/routes/reconciliation.py",
    "ui/routes/settings_accounting.py",
)


async def _complete(client, h, sid):
    return await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)


async def _currency_in_use(client, h, session, tok):
    """Way out: a new account in the new currency, and the balance moved to it with a
    journal entry whose lines are written in each account's own currency."""
    b = await bank(client, h, currency="EUR", opening=0.0)
    await fx_deposit(client, h, b, 100, 1.08, 108)
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_bank_currency_has_history", currency="EUR"), r.text
    usd = await bank(client, h, currency="USD", opening=0.0, name="Home Bank", number="0002")
    async with _ui_as(tok) as ui:
        form = (await ui.get("/accounting/journal/new")).text
        assert 'name="currency_0"' in form, "the journal form writes a line in another currency"
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-09-20", "memo": "Move balance", "idempotency_token": uuid.uuid4().hex, "entries": [
            {"account": b["chart_account_code"], "debit": 0, "credit": 100, "currency": "EUR", "rate": 1.08},
            {"account": usd["chart_account_code"], "debit": 108, "credit": 0}]})
    assert r.status_code == 200, r.text
    sid = (await start(client, h, b["id"], "2026-09-30", 0.0)).json()["id"]
    ids = [e["je_id"] for e in (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["unreconciled_entries"]]
    assert (await match(client, h, sid, ids)).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


async def _currency_reconciled(client, h, session, tok):
    """Way out: reopen the completed reconciliation, then the correction goes through."""
    b = await bank(client, h, currency="THB", opening=0.0)
    je = await legacy_deposit(client, h, session, b, "2026-09-10", 50.0)
    sid = await legacy_session(session, b, "2026-09-30", balance=50.0, je_ids=[je])
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_bank_currency_reconciled", currency="THB"), r.text
    assert (await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)).status_code == 200
    assert (await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"},
                               headers=h)).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


async def _rate_required(client, h, session, tok):
    """Way out: the screen that books the line asks for the rate; with it the line books."""
    b = await bank(client, h, currency="EUR", opening=0.0)
    sid = (await start(client, h, b["id"], "2026-09-30", 25.0)).json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/import-csv", headers=h, files={"file": (
        "s.csv", b"Date,Description,Amount\n2026-09-20,Interest,25\n", "text/csv")})
    assert r.status_code == 200, r.text
    line = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()["items"][0]
    path = f"/accounting/reconciliation/{sid}/lines/{line['id']}/create"
    r = await client.post(path, headers=h, json={"account_code": "4100"})
    assert r.status_code == 422 and r.json()["detail"] == t(
        "acct.err_bank_rate_required", currency="EUR", base="USD"), r.text
    async with _ui_as(tok) as ui:
        form = (await ui.get(f"/accounting/reconcile/{sid}/lines/{line['id']}/create-form")).text
        assert 'name="rate"' in form
    assert (await client.post(path, headers=h, json={"account_code": "4100", "rate": 1.08})).status_code == 200
    ids = [e["je_id"] for e in (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["unreconciled_entries"]]
    await match(client, h, sid, ids)
    assert (await _complete(client, h, sid)).status_code == 200


async def _je_bank_currency(client, h, session, tok):
    """Way out: the journal form takes the line's currency and rate."""
    b = await bank(client, h, currency="JPY", opening=0.0)
    code = b["chart_account_code"]
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-09-10", "memo": "Sale", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": code, "debit": 15, "credit": 0}, {"account": "4100", "debit": 0, "credit": 15}]})
    assert r.status_code == 422 and r.json()["detail"] == t(
        "acct.err_je_bank_currency", account=code, currency="JPY"), r.text
    async with _ui_as(tok) as ui:
        form = (await ui.get("/accounting/journal/new")).text
        assert 'name="currency_0"' in form and 'name="rate_0"' in form
    je = await fx_deposit(client, h, b, 1850, 0.0067, 12.40)
    sid = (await start(client, h, b["id"], "2026-09-30", 1850)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


async def _amount_unknown(client, h, session, tok):
    """Way out: Undo on the entry with no amount, offered in the workspace."""
    b = await bank(client, h, currency="EUR", opening=0.0)
    eur = await fx_deposit(client, h, b, 100, 1.08, 108)
    base_only = await legacy_deposit(client, h, session, b, "2026-09-12", 50.0)
    sid = await legacy_session(session, b, "2026-09-30", status="open", je_ids=[eur, base_only], balance=100.0)
    r = await _complete(client, h, sid)
    assert r.status_code == 422 and r.json()["detail"] == t("acct.err_recon_amount_unknown", currency="EUR"), r.text
    undo = f"/accounting/reconcile/{sid}/entries/{base_only}/unmatch"
    async with _ui_as(tok) as ui:
        assert undo in (await ui.get(f"/accounting/reconcile/{sid}")).text
        assert (await ui.post(undo)).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


async def _amount_unknown_relabel(client, h, session, tok):
    """Way out: the button at the top of the workspace sets the account to the books' currency."""
    b = await bank(client, h, currency="THB", opening=0.0)
    je = await legacy_deposit(client, h, session, b, "2026-09-10", 200.0)
    sid = await legacy_session(session, b, "2026-09-30", status="open", je_ids=[je], balance=200.0)
    r = await _complete(client, h, sid)
    assert r.status_code == 422 and r.json()["detail"] == t(
        "acct.err_recon_amount_unknown_relabel", currency="THB", base="USD"), r.text
    relabel = f"/accounting/reconcile/relabel/{b['id']}"
    async with _ui_as(tok) as ui:
        assert f'action="{relabel}"' in (await ui.get(f"/accounting/reconcile/{sid}")).text
        assert (await ui.post(relabel, data={"next": f"/accounting/reconcile/{sid}"})).status_code == 303
    assert (await _complete(client, h, sid)).status_code == 200


async def _book_entry_no_amount(client, h, session, tok):
    """Way out where the account cannot be relabelled (it holds EUR amounts): void the
    entry and post it again with its EUR amount, then match it."""
    b = await bank(client, h, currency="EUR", opening=0.0)
    eur = await fx_deposit(client, h, b, 100, 1.08, 108)
    base_only = await legacy_deposit(client, h, session, b, "2026-09-12", 54.0)
    sid = (await start(client, h, b["id"], "2026-09-30", 150.0)).json()["id"]
    r = await match(client, h, sid, [base_only])
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_recon_book_entry_no_amount"), r.text
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["bank_account"]["relabel_to"] is None
    r = await client.post(f"/accounting/journal-entries/{base_only}/void", headers=h, json={})
    assert r.status_code == 200, r.text
    again = await fx_deposit(client, h, b, 50, 1.08, 54, day="2026-09-12")
    assert (await match(client, h, sid, [eur, again])).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


async def _transfer_to_amount(client, h, session, tok):
    """Way out: the transfer states what the destination receives in its own currency."""
    usd = await bank(client, h, currency="USD", opening=500.0, name="Home Bank", number="0002")
    b = await bank(client, h, currency="EUR", opening=0.0)
    body = {"from_bank_id": usd["id"], "to_bank_id": b["id"], "amount": 54.0, "date": "2026-09-10"}
    r = await client.post("/accounting/transfers", headers=h, json=body)
    assert r.status_code == 422 and r.json()["detail"] == t("acct.err_transfer_to_amount", currency="EUR"), r.text
    assert (await client.post("/accounting/transfers", headers=h, json=body | {"to_amount": 50})).status_code == 200
    sid = (await start(client, h, b["id"], "2026-09-30", 50.0)).json()["id"]
    ids = [e["je_id"] for e in (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["unreconciled_entries"]]
    assert (await match(client, h, sid, ids)).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


async def _payment_bank_currency(client, h, session, tok):
    """Way out: the payment form lists the other accounts; one in the invoice's currency
    takes the payment and reconciles."""
    jpy = await bank(client, h, currency="JPY", opening=0.0)
    eur = await bank(client, h, currency="EUR", opening=0.0, name="Euro Bank", number="0003")
    inv = await _eur_invoice(client, h)
    pay = {"amount": 100.0, "payment_date": "2026-09-10", "conversion_rate": 1.1}
    r = await client.post(f"/docs/{inv}/payment", headers=h, json=pay | {"bank_account": jpy["chart_account_code"]})
    assert r.status_code == 422 and r.json()["detail"] == t(
        "documents.err_payment_bank_currency", bank_currency="JPY", doc_currency="EUR", base="USD"), r.text
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/docs/{inv}")).text
        assert f'value="{eur["chart_account_code"]}"' in page, "the payment form offers the EUR account"
    r = await client.post(f"/docs/{inv}/payment", headers=h, json=pay | {"bank_account": eur["chart_account_code"]})
    assert r.status_code == 200, r.text
    sid = (await start(client, h, eur["id"], "2026-09-30", 100.0)).json()["id"]
    ids = [e["je_id"] for e in (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["unreconciled_entries"]]
    assert (await match(client, h, sid, ids)).status_code == 200
    assert (await _complete(client, h, sid)).status_code == 200


WAYS_OUT = {
    "acct.err_bank_currency_has_history": _currency_in_use,
    "acct.err_bank_currency_reconciled": _currency_reconciled,
    "acct.err_bank_rate_required": _rate_required,
    "acct.err_je_bank_currency": _je_bank_currency,
    "acct.err_recon_amount_unknown": _amount_unknown,
    "acct.err_recon_amount_unknown_relabel": _amount_unknown_relabel,
    "acct.err_recon_book_entry_no_amount": _book_entry_no_amount,
    "acct.err_transfer_to_amount": _transfer_to_amount,
    "documents.err_payment_bank_currency": _payment_bank_currency,
}


def _currency_refusals() -> set[str]:
    """Refusals on the bank and reconciliation path whose text is about a currency."""
    en = json.loads((ROOT / "ui/locales/en.json").read_text())
    keys: set[str] = set()
    for src in _SOURCES:
        keys |= set(re.findall(r'"((?:acct|documents|error|recon)\.err_[a-z_]+)"', (ROOT / src).read_text()))
    return {k for k in keys if re.search(r"bank|recon|transfer|split", k) and "currenc" in en.get(k, "").lower()}


async def test_every_currency_refusal_has_a_way_out_listed():
    assert _currency_refusals() == set(WAYS_OUT)


@pytest.mark.parametrize("key", sorted(WAYS_OUT))
async def test_the_way_out_of_each_currency_refusal_works(client, session, key):
    tok, h = await admin(client)
    await WAYS_OUT[key](client, h, session, tok)
