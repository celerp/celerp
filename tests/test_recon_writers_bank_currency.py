# CLASS money-in-the-wrong-currency (writer side; second finding of review-1 class 2).
"""The reconciliation's own writers post a foreign-currency bank's figures as base lines
with no currency, which the reconciliation then reads as having no amount: a foreign
currency bank with an opening balance, or one finished with a write-off or a statement
line booked from the workspace, can never be completed."""
from __future__ import annotations

import pytest

from test_reconciliation_history import admin, bank, fx_deposit, match, start

pytestmark = pytest.mark.asyncio


async def test_foreign_bank_with_an_opening_balance_completes(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=1000, rate=1.08)
    sid = (await start(client, h, b["id"], "2026-09-30", 1000)).json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text


async def test_write_off_on_a_foreign_bank_lets_it_complete(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    je = await fx_deposit(client, h, b, 1850, 0.0067, 12.40)
    sid = (await start(client, h, b["id"], "2026-09-30", 1851)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", headers=h, json={"rate": 0.0067})
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text


async def _eur_invoice(client, h, total=100.0) -> str:
    """A finalized EUR invoice in USD books."""
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "currency": "EUR", "conversion_rate": 1.08, "total": total,
        "line_items": [{"name": "X", "quantity": 1, "unit_price": total, "line_total": total}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200
    return doc_id


async def test_payment_and_refund_into_a_bank_kept_in_the_invoice_currency_reconcile(client, session):
    """A EUR invoice paid into a EUR account, then partly refunded: both entries carry
    their EUR amounts, so the statement reconciles in EUR."""
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=0)
    inv = await _eur_invoice(client, h)
    r = await client.post(f"/docs/{inv}/payment", headers=h, json={
        "amount": 100.0, "payment_date": "2026-09-10", "bank_account": b["chart_account_code"], "conversion_rate": 1.1})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{inv}/refund", headers=h, json={
        "payment_index": 0, "amount": 40.0, "payment_date": "2026-09-12"})
    assert r.status_code == 200, r.text
    sid = (await start(client, h, b["id"], "2026-09-30", 60)).json()["id"]
    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    entries = {e["je_id"]: e["amount"] for e in recon["unreconciled_entries"]}
    assert sorted(entries.values()) == [-40.0, 100.0], entries
    assert (await match(client, h, recon["id"], list(entries))).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{recon['id']}/complete", headers=h)
    assert r.status_code == 200, r.text


async def test_payment_into_a_bank_kept_in_a_third_currency_is_refused_naming_the_accounts_to_use(client, session):
    from ui.i18n import t
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    inv = await _eur_invoice(client, h)
    r = await client.post(f"/docs/{inv}/payment", headers=h, json={
        "amount": 100.0, "payment_date": "2026-09-10", "bank_account": b["chart_account_code"]})
    assert r.status_code == 422 and r.json()["detail"] == t(
        "documents.err_payment_bank_currency", bank_currency="JPY", doc_currency="EUR", base="USD"), r.text
