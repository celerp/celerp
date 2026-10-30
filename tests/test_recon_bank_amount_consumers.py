# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
# CLASS money-in-the-wrong-currency (reader side).
"""Everything that reads a bank account's journal lines reads them in the account's own
currency, the way its reconciliation does: the balance on the bank list, the protection
of a completed reconciliation, and a movement too small to show in the books' currency."""
from __future__ import annotations

import uuid

import pytest

from celerp.output.doc_print import EMPTY, fmt_money
from test_recon_guard import _company, _created_data, _emit, _refused
from test_reconciliation_history import (
    admin, bank, complete, fx_deposit, legacy_deposit, match, start,
)
from test_reconciliation_reopen import _ui_as
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def _listed(client, h, bank_id) -> dict:
    r = await client.get("/accounting/bank-accounts", headers=h)
    assert r.status_code == 200, r.text
    return next(b for b in r.json()["items"] if b["id"] == bank_id)


async def test_foreign_bank_balance_is_shown_in_its_own_currency(client, session):
    """EUR 100 posted at 1.08 in USD books is EUR 100.00 on the bank list, not EUR 108.00."""
    tok, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=0)
    await fx_deposit(client, h, b, 100, 1.08, 108)
    assert (await _listed(client, h, b["id"]))["balance"] == 100.0
    async with _ui_as(tok) as ui:
        page = (await ui.get("/settings/accounting?tab=bank-accounts")).text
    assert fmt_money(100.0, "EUR") in page and fmt_money(108.0, "EUR") not in page


async def test_balance_of_a_history_that_cannot_be_read_is_unknown(client, session):
    """A bank line with no amount in the account's currency makes the balance unknown,
    never a books-currency figure under the account's currency."""
    tok, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    await legacy_deposit(client, h, session, b, "2026-09-10", 12.40)
    assert (await _listed(client, h, b["id"]))["balance"] is None
    async with _ui_as(tok) as ui:
        page = (await ui.get("/settings/accounting?tab=bank-accounts")).text
    assert t("acct.balance_unknown", currency="JPY") in page and "12.40" not in page and EMPTY in page


async def test_foreign_amount_change_on_a_completed_reconciliation_is_refused(client, session):
    """EUR 100 changed to EUR 90 keeps the same USD 108, and is still a change."""
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=0)
    je = await fx_deposit(client, h, b, 100, 1.08, 108)
    sid = (await start(client, h, b["id"], "2026-09-30", 100)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    await complete(client, h, sid)
    cid = await _company(session, b)
    data = await _created_data(session, cid, je)
    changed = {**data, "entries": [
        {**line, "fx_debit": 90.0, "fx_rate": 1.2} if line["account"] == b["chart_account_code"] else line
        for line in data["entries"]]}
    message = await _refused(session, cid, je, "acc.journal_entry.created", changed)
    assert "2026-09-30" in message


async def _invoice(client, h, currency, rate, total) -> str:
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "currency": currency, "conversion_rate": rate, "total": total,
        "line_items": [{"name": "X", "quantity": 1, "unit_price": total, "line_total": total}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200
    return doc_id


async def test_movement_too_small_for_the_books_currency_is_kept_and_reconciles(client, session):
    """VND 100 at USD 0.000039 is USD 0.00 in the books, and still VND 100 on a VND
    account: the payment and the refund are posted, shown and reconciled in VND."""
    _, h = await admin(client)
    b = await bank(client, h, currency="VND", opening=0)
    inv = await _invoice(client, h, "VND", 0.000039, 100)
    r = await client.post(f"/docs/{inv}/payment", headers=h, json={
        "amount": 100, "payment_date": "2026-09-10", "bank_account": b["chart_account_code"],
        "conversion_rate": 0.000039})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{inv}/refund", headers=h, json={
        "payment_index": 0, "amount": 40, "payment_date": "2026-09-12"})
    assert r.status_code == 200, r.text
    assert (await _listed(client, h, b["id"]))["balance"] == 60.0
    sid = (await start(client, h, b["id"], "2026-09-30", 60)).json()["id"]
    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    entries = {e["je_id"]: e["amount"] for e in recon["unreconciled_entries"]}
    assert sorted(entries.values()) == [-40.0, 100.0], entries
    assert (await match(client, h, sid, list(entries))).status_code == 200
    await complete(client, h, sid)


async def test_manual_entry_too_small_for_the_books_currency_is_posted_and_shown(client, session):
    """A journal entry of VND 100 at USD 0.000039 on both sides moves VND 100: it is
    posted, listed in the journal with its VND amounts, and counts on the VND account."""
    _, h = await admin(client)
    b = await bank(client, h, currency="VND", opening=0)
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-09-10", "memo": "Small deposit", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": b["chart_account_code"], "debit": 100, "credit": 0, "currency": "VND", "rate": 0.000039},
                    {"account": "4100", "debit": 0, "credit": 100, "currency": "VND", "rate": 0.000039}]})
    assert r.status_code == 200, r.text
    assert (await _listed(client, h, b["id"]))["balance"] == 100.0
    journal = (await client.get("/accounting/journal", headers=h)).json()
    lines = [l for e in journal["entries"] if e["memo"] == "Small deposit" for l in e["lines"]]
    assert len(lines) == 2 and {l["debit"] for l in lines} == {0.0}, lines
