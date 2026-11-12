# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Donor history and donor statements.

Initial conditions per test: a fresh company with the seeded chart (fixture ``auth``), a gift
category "Donation" whose income account is 4250, "Dues" and "Ticket" categories that are not
gifts, and service items in each made through POST /items. Invoices are finalized through
POST /docs; payments, refunds and credit notes go through their routes; a direct gift is a
manual journal entry crediting the gift income account with the donor named on the line.
History is read from GET /donors/{contact}/history and statements through /donors/statements.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.services.email import EmailResult
from test_income_accounts import _account, _category, _issued, _item, _line, _set_category_account

pytestmark = pytest.mark.asyncio

GIFT_ACCOUNT = "4250"
PERIOD = {"from": "2026-03-01", "to": "2026-03-31"}


async def _books(client, auth) -> dict:
    await _account(client, auth, GIFT_ACCOUNT)
    donation = await _category(client, auth, "Donation")
    assert (await _set_category_account(client, auth, donation, GIFT_ACCOUNT)).status_code == 200
    r = await client.put(f"/companies/me/categories/{donation}/gift", headers=auth["headers"], json={"gift": True})
    assert r.status_code == 200, r.text
    dues, ticket = await _category(client, auth, "Dues"), await _category(client, auth, "Ticket")
    return {"gift": await _item(client, auth, "Gift", category=donation),
            "dues": await _item(client, auth, "Annual dues", category=dues),
            "ticket": await _item(client, auth, "Dinner ticket", category=ticket)}


async def _donor(client, auth, name: str, email: str | None = None) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": name, **({"email": email} if email else {})})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _invoice(client, auth, donor: str, *lines, doc_type: str = "invoice") -> str:
    """A finalized document for ``donor`` at the line prices given: naming the price list keeps
    those prices instead of repricing the lines from the contact's list."""
    total = round(sum(li["line_total"] for li in lines), 2)
    return await _issued(client, auth, doc_type, list(lines), contact_id=donor, issue_date="2026-03-01",
                         price_list="Retail", subtotal=total, total=total)


async def _pay(client, auth, doc: str, amount: float, day: str = "2026-03-05"):
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": day, "bank_account": "1111", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


async def _refund(client, auth, doc: str, amount: float, **extra):
    r = await client.post(f"/docs/{doc}/refund", headers=auth["headers"], json={
        "payment_index": 0, "amount": amount, "payment_date": "2026-03-10", "idempotency_key": uuid.uuid4().hex,
        **extra})
    assert r.status_code == 200, r.text


async def _direct_gift(client, auth, donor: str, amount: float, day: str):
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": day, "memo": "Gift by bank transfer", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": amount, "credit": 0.0},
                    {"account": GIFT_ACCOUNT, "debit": 0.0, "credit": amount, "contact": donor}]})
    assert r.status_code == 200, r.text


async def _history(client, auth, donor: str) -> dict:
    r = await client.get(f"/donors/{donor}/history", headers=auth["headers"], params=PERIOD)
    assert r.status_code == 200, r.text
    return r.json()


async def test_donor_history_paid_gift_lines_plus_bank_gifts_no_double_count(client, auth):
    """A gift line counts by the cash received for it (its share of each receipt), a direct
    gift once, and neither again through the invoice's own revenue posting."""
    items = await _books(client, auth)
    donor = await _donor(client, auth, "Ada Donor", "ada@test.test")
    doc = await _invoice(client, auth, donor, _line(items["gift"], "Gift", 100), _line(items["ticket"], "Dinner ticket", 50))
    await _pay(client, auth, doc, 90)  # 60 of it the gift's share
    assert float((await _history(client, auth, donor))["total"]) == 60.0
    await _pay(client, auth, doc, 60)
    await _direct_gift(client, auth, donor, 25, "2026-03-12")
    await _direct_gift(client, auth, donor, 999, "2026-04-02")  # outside the period
    history = await _history(client, auth, donor)
    assert float(history["total"]) == 125.0
    assert sorted(float(g["amount"]) for g in history["gifts"]) == [25.0, 40.0, 60.0]


async def test_donor_history_excludes_dues_tickets_credits_refunds_chargebacks(client, auth):
    items = await _books(client, auth)
    donor = await _donor(client, auth, "Ben Donor", "ben@test.test")
    dues = await _invoice(client, auth, donor, _line(items["dues"], "Annual dues", 80), _line(items["ticket"], "Dinner ticket", 20))
    await _pay(client, auth, dues, 100)
    refunded = await _invoice(client, auth, donor, _line(items["gift"], "Gift", 100))
    await _pay(client, auth, refunded, 100)
    await _refund(client, auth, refunded, 30)
    charged_back = await _invoice(client, auth, donor, _line(items["gift"], "Gift", 50))
    await _pay(client, auth, charged_back, 50)
    await _refund(client, auth, charged_back, 50, reason="chargeback")
    credited = await _invoice(client, auth, donor, _line(items["gift"], "Gift", 40))
    note = await _invoice(client, auth, donor, _line(items["gift"], "Gift", 40), doc_type="credit_note")
    r = await client.post(f"/docs/{note}/apply-to-invoice", headers=auth["headers"], json={
        "target_doc_id": credited, "amount": 40, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    history = await _history(client, auth, donor)
    assert float(history["total"]) == 70.0
    assert {g.get("doc_id") for g in history["gifts"] if float(g["amount"])} <= {refunded}


async def _statement_donors(client, auth) -> dict:
    items = await _books(client, auth)
    with_email = await _donor(client, auth, "Cy Donor", "cy@test.test")
    no_email = await _donor(client, auth, "Di Donor")
    for donor, amount in ((with_email, 75), (no_email, 20)):
        doc = await _invoice(client, auth, donor, _line(items["gift"], "Gift", amount))
        await _pay(client, auth, doc, amount)
    return {"email": with_email, "none": no_email}


def _mailbox(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_send(to, subject, body_html, body_text="", **kw):
        sent.append({"to": to, "subject": subject, "html": body_html})
        return EmailResult(ok=True, detail="accepted")

    monkeypatch.setattr("celerp.services.email.send_email", fake_send)
    return sent


async def test_donor_statement_bulk_send_preview_idempotent(client, auth, monkeypatch):
    """Preview lists who a send reaches; the send needs explicit confirmation, reports each
    donor, skips one without an email, returns its first result when repeated, and sends again
    only to the donors resend names, or to all once the wording changed."""
    donors = await _statement_donors(client, auth)
    sent = _mailbox(monkeypatch)
    r = await client.get("/donors/statements/preview", headers=auth["headers"], params=PERIOD)
    assert r.status_code == 200, r.text
    preview = {row["contact_id"]: row for row in r.json()["donors"]}
    assert float(preview[donors["email"]]["total"]) == 75.0 and preview[donors["none"]]["no_email"] is True
    body = {**PERIOD, "confirm": False}
    assert (await client.post("/donors/statements/send", headers=auth["headers"], json=body)).status_code == 422
    first = await client.post("/donors/statements/send", headers=auth["headers"], json={**body, "confirm": True})
    assert first.status_code == 200, first.text
    results = {row["contact_id"]: row for row in first.json()["results"]}
    assert set(results) == set(preview)
    assert results[donors["email"]]["state"] == "sent" and results[donors["none"]]["state"] == "no_email"
    assert [m["to"] for m in sent] == ["cy@test.test"]
    again = await client.post("/donors/statements/send", headers=auth["headers"], json={**body, "confirm": True})
    assert {row["contact_id"]: row for row in again.json()["results"]}[donors["email"]] == {**results[donors["email"]], "repeat": True}
    assert len(sent) == 1
    resend = await client.post("/donors/statements/send", headers=auth["headers"],
                               json={**body, "confirm": True, "resend": [donors["email"]]})
    assert resend.status_code == 200 and len(sent) == 2
    assert [row["contact_id"] for row in resend.json()["results"]] == [donors["email"]]
    r = await client.put("/donors/statements/wording", headers=auth["headers"], json={"text": "Thank you, {name}."})
    assert r.status_code == 200, r.text
    corrected = await client.post("/donors/statements/send", headers=auth["headers"], json={**body, "confirm": True})
    assert corrected.status_code == 200 and len(sent) == 3 and "Thank you, Cy Donor." in sent[-1]["html"]
    pdf = await client.get(f"/donors/{donors['email']}/statement.pdf", headers=auth["headers"], params=PERIOD)
    assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")


async def test_donor_gifts_newest_first_and_statement_amounts_right_aligned(client, auth, monkeypatch):
    """A donor's gifts read newest first in their history and statement, and the statement's
    amount column, its header included, sits right-aligned over the figures, as on screen."""
    import re

    from ui.i18n import t

    await _books(client, auth)
    donor = await _donor(client, auth, "Eve Donor", "eve@test.test")
    for amount, day in ((10, "2026-03-05"), (30, "2026-03-20"), (20, "2026-03-12")):
        await _direct_gift(client, auth, donor, amount, day)
    assert [g["date"] for g in (await _history(client, auth, donor))["gifts"]] == ["2026-03-20", "2026-03-12", "2026-03-05"]
    sent = _mailbox(monkeypatch)
    r = await client.post("/donors/statements/send", headers=auth["headers"], json={**PERIOD, "confirm": True})
    assert r.status_code == 200 and len(sent) == 1, r.text
    html = sent[0]["html"]
    assert re.findall(r"2026-03-\d\d", html) == ["2026-03-01", "2026-03-31", "2026-03-20", "2026-03-12", "2026-03-05"]
    header = re.search(r"<th([^>]*)>%s</th>" % re.escape(t("label.amount")), html)
    assert header and "text-align: right" in header.group(1).replace("text-align:right", "text-align: right"), html
    figures = re.findall(r"<td([^>]*)>[\d,.]+ \w{3}</td>", html)
    assert len(figures) == 4 and all("right" in attrs for attrs in figures), html


async def test_thank_you_wording_tax_neutral_default(client, auth):
    r = await client.get("/donors/statements/wording", headers=auth["headers"])
    assert r.status_code == 200, r.text
    text = r.json()["text"].lower()
    assert text and not any(word in text for word in ("deductib", "tax", "receipt for tax", "irs", "charitable contribution"))
