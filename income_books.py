# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Income, gift and donor books built through the API, shared by the core tests and the
modules' own tests (a module test imports nothing from tests/): revenue accounts, item
categories with their income account, service items, finalized documents, a document
import record, the donor statement setup with its captured mail, and what posted entries
hold by fund and account."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.email import EmailResult

GIFT_ACCOUNT = "4250"
PERIOD = {"from": "2026-03-01", "to": "2026-03-31"}


async def _account(client, auth, code: str, account_type: str = "revenue", parent: str | None = "4000") -> None:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent})
    assert r.status_code == 200, r.text


async def _category(client, auth, name: str) -> str:
    r = await client.post("/companies/me/categories", headers=auth["headers"], json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["key"]


async def _set_category_account(client, auth, key: str, code: str | None):
    return await client.put(f"/companies/me/categories/{key}/income-account", headers=auth["headers"],
                            json={"income_account": code})


async def _item(client, auth, name: str, **fields) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "name": name, "sell_by": "piece", "inventory_type": "service", "quantity": 0, "status": "available",
        **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line(item: str | None, name: str, amount: float, **extra) -> dict:
    return {"item_id": item, "name": name, "quantity": 1, "unit_price": amount, "line_total": amount, **extra}


async def _draft(client, auth, doc_type: str, lines: list[dict], **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "ref_id": f"{doc_type[:3].upper()}-{uuid.uuid4().hex[:6]}",
        "line_items": lines, "total": round(sum(li["line_total"] for li in lines), 2), **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _issued(client, auth, doc_type: str, lines: list[dict], **extra) -> str:
    doc = await _draft(client, auth, doc_type, lines, **extra)
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    return doc


def _issued_import(entity_id: str, lines: list[dict]) -> dict:
    """An open invoice in an import file, booked now."""
    total = round(sum(li["line_total"] for li in lines), 2)
    return {"entity_id": entity_id, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "invoice", "status": "awaiting_payment", "ref_id": entity_id.split(":")[1],
                     "issue_date": "2026-01-20", "line_items": lines, "total": total, "subtotal": total,
                     "amount_outstanding": total, "import_treatment": "record_now"}}


OTHER_INCOME_ACCOUNT = "4260"


async def _books(client, auth, gift_mapped: bool = True) -> dict:
    """Gift, dues and ticket items. With ``gift_mapped`` the Donation category is credited to
    GIFT_ACCOUNT; without it gifts fall to the sales revenue account and dues and tickets go to
    OTHER_INCOME_ACCOUNT, so sales revenue holds only gifts."""
    await _account(client, auth, GIFT_ACCOUNT if gift_mapped else OTHER_INCOME_ACCOUNT)
    donation = await _category(client, auth, "Donation")
    if gift_mapped:
        assert (await _set_category_account(client, auth, donation, GIFT_ACCOUNT)).status_code == 200
    r = await client.put(f"/companies/me/categories/{donation}/gift", headers=auth["headers"], json={"gift": True})
    assert r.status_code == 200, r.text
    dues, ticket = await _category(client, auth, "Dues"), await _category(client, auth, "Ticket")
    if not gift_mapped:
        for key in (dues, ticket):
            assert (await _set_category_account(client, auth, key, OTHER_INCOME_ACCOUNT)).status_code == 200
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


def _mailbox(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_send(to, subject, body_html, body_text="", **kw):
        sent.append({"to": to, "subject": subject, "html": body_html, "operation_id": kw.get("operation_id")})
        return EmailResult(ok=True, detail="accepted")

    monkeypatch.setattr("celerp.services.email.send_email", fake_send)
    return sent


async def _statement_donors(client, auth) -> dict:
    items = await _books(client, auth)
    with_email = await _donor(client, auth, "Cy Donor", "cy@test.test")
    no_email = await _donor(client, auth, "Di Donor")
    for donor, amount in ((with_email, 75), (no_email, 20)):
        doc = await _invoice(client, auth, donor, _line(items["gift"], "Gift", amount))
        await _pay(client, auth, doc, amount)
    return {"email": with_email, "none": no_email}


async def _standing(session, auth, prefix: str) -> dict:
    """(fund, account) -> what the posted entries under ``prefix`` hold, zeros left out."""
    session.expire_all()
    out: dict = {}
    for row in (await session.scalars(select(Projection).where(
            Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
            Projection.entity_id.startswith(prefix, autoescape=True)))).all():
        if (row.state or {}).get("status") != "posted":
            continue
        for e in row.state.get("entries") or []:
            key = (e.get("fund_id"), e["account"])
            out[key] = out.get(key, Decimal(0)) + Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
    return {k: v for k, v in out.items() if v}
