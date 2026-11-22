# CLASS money-in-the-wrong-currency (writer side; second finding of review-1 class 2).
"""The reconciliation's own writers post a foreign-currency bank's figures as base lines
with no currency, which the reconciliation then reads as having no amount: a foreign
currency bank with an opening balance, or one finished with a write-off or a statement
line booked from the workspace, can never be completed."""
from __future__ import annotations

import pytest

import bank_statement_helpers as B
from test_reconciliation_history import admin, bank, complete, fx_deposit, match, start
from ui.i18n import t, refusal_text

pytestmark = pytest.mark.asyncio


async def test_foreign_bank_with_an_opening_balance_completes(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=1000, rate=1.08)
    sid = (await start(client, h, b["id"], "2026-09-30", 1000)).json()["id"]
    await complete(client, h, sid)


async def test_write_off_on_a_foreign_bank_lets_it_complete(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    je = await fx_deposit(client, h, b, 1850, 0.0067, 12.40)
    sid = (await start(client, h, b["id"], "2026-09-30", 1851)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", headers=h, json={"rate": 0.0067})
    assert r.status_code == 200, r.text
    await complete(client, h, sid)


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
    await complete(client, h, recon["id"])


async def test_payment_into_a_bank_kept_in_a_third_currency_is_refused_naming_the_accounts_to_use(client, session):
    from ui.i18n import t
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    inv = await _eur_invoice(client, h)
    r = await client.post(f"/docs/{inv}/payment", headers=h, json={
        "amount": 100.0, "payment_date": "2026-09-10", "bank_account": b["chart_account_code"]})
    assert r.status_code == 422 and refusal_text(r.json()["detail"]) == t(
        "documents.err_payment_bank_currency", bank_currency="JPY", doc_currency="EUR", base="USD"), r.text


@pytest.mark.parametrize("currency,rate", [("EUR", 1.08), ("JPY", 0.0067)])
async def test_the_screens_ask_for_the_rate_and_a_foreign_bank_reconciles_through_them(client, session, currency, rate):
    """Every screen that books an entry on a foreign-currency account asks for its rate:
    the new-account form, the bank page's answer and split forms and the write-off."""
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    async with _ui_as(tok) as ui:
        assert 'name="rate"' in (await ui.get("/settings/accounting/bank-accounts/new")).text
        r = await ui.post("/settings/accounting/bank-accounts/new", data={
            "bank_name": "Abroad", "account_number": "9", "bank_type": "checking",
            "currency": currency, "opening_balance": "1000", "rate": str(rate)})
        assert r.status_code == 204, r.text
        b = next(x for x in (await client.get("/accounting/bank-accounts", headers=h)).json()["items"]
                 if x["bank_name"] == "Abroad")
        sid = (await start(client, h, b["id"], "2026-09-30", 1056)).json()["id"]
        got = await B.import_into(client, h, sid, b"Date,Description,Amount\n2026-09-20,Interest,25\n2026-09-21,Mixed,30\n")
        sid = got["session_id"]
        lines = {x["description"]: x["id"] for x in got["lines"]}
        bank_page = f"/accounting/reconcile/bank/{b['id']}"
        page = (await ui.get(bank_page)).text
        for tpl in ("bank-tpl-answer", "bank-tpl-split"):
            body = page.split(f'id="{tpl}"', 1)[1].split("</template>", 1)[0]
            assert 'name="rate"' in body and t("recon.rate_label", currency=currency, base="USD") in body, tpl
        r = await ui.post(f"{bank_page}/resolve-line", data={
            "line_id": lines["Interest"], "form": "answer", "answer": "post:4100", "rate": str(rate),
            "idempotency_key": B.key()})
        assert r.status_code == 200 and "form-error" not in r.text, r.text[:400]
        r = await ui.post(f"{bank_page}/resolve-line", data={
            "line_id": lines["Mixed"], "form": "split", "account_code": ["4100", "4100"], "amount": ["20", "10"],
            "rate": str(rate), "idempotency_key": B.key()})
        assert r.status_code == 200 and "form-error" not in r.text, r.text[:400]
        page = (await ui.get(bank_page)).text
        assert f'hx-post="{bank_page}/statement/{sid}/write-off"' in page and 'name="rate"' in page
        r = await ui.post(f"{bank_page}/statement/{sid}/write-off", data={"rate": str(rate), "idempotency_key": B.key()})
        assert r.status_code == 200 and t("bank.written_off") in r.text, r.text[:400]
    await complete(client, h, sid)


async def test_a_bank_in_the_books_currency_is_not_asked_for_a_rate(client, session):
    from fasthtml.common import to_xml
    from ui.components.bank_groups import answer_form, split_form
    options = [("post:6100", "6100 Bank fees")]
    for form in (answer_form, split_form):
        assert 'name="rate"' not in to_xml(form({"id": "b1", "currency": "USD", "books_currency": "USD"}, options))
        assert 'name="rate"' in to_xml(form({"id": "b1", "currency": "EUR", "books_currency": "USD"}, options))
