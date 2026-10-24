# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A contact page's money cards and documents table show what the contact's documents hold,
counted as the invoice list counts them: issued invoices only (no draft, no void), in the
company currency, with a credit note on an invoice lowering what it still owes."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from celerp.services.business_time import business_today
from test_helpers import mark_running
from ui.components.table import fmt_money, format_value

pytestmark = pytest.mark.asyncio

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_contact_cards", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


async def _ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _invoice(api, contact: dict, total: float, *, steps=("finalize", "send"), **extra) -> str:
    """An invoice for ``contact`` issued today, taken through ``steps`` as a user would."""
    today = business_today(None).isoformat()
    doc = await _ok(await api.post("/docs", json={
        "doc_type": "invoice", "contact_id": contact["id"], "contact_name": contact["name"],
        "date": today, "issue_date": today, "due_date": today,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total, **extra}))
    for step in steps:
        if step == "pay":
            await _ok(await api.post(f"/docs/{doc['id']}/payment", json={
                "amount": total, "method": "cash", "payment_date": today, "bank_account": "1111"}))
        elif step == "void":
            await _ok(await api.post(f"/docs/{doc['id']}/void", json={}))
        else:
            await _ok(await api.post(f"/docs/{doc['id']}/{step}", json={}))
    return doc["id"]


def _cards(html: str) -> dict[str, str]:
    """Each money card's label to the value it shows."""
    return {label.strip(): value.strip() for label, value in re.findall(
        r'class="financial-card-label">\s*<span>([^<]+)</span>.*?class="financial-card-value">([^<]*)<', html, re.S)}


async def test_contact_cards_and_documents_show_the_contact_s_invoices(owner_ui, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    api = owner_ui.api
    # The demo company: its items, contacts and invoices on other customers.
    tok = api.headers["Authorization"].split(" ", 1)[1]
    await seed_demo.seed_items(api, tok)
    contacts = await seed_demo.seed_contacts(api, tok)
    await seed_demo.seed_docs(api, tok, contacts, "invoice", 6)
    contact = await _ok(await api.post("/crm/contacts", json={"name": "Card Customer", "contact_type": "customer"}))
    contact = {"id": contact["id"], "name": "Card Customer"}
    currency = (await _ok(await api.get("/companies/me"))).get("currency") or "USD"
    foreign = "EUR" if currency != "EUR" else "GBP"

    credited = await _invoice(api, contact, 1000.0)
    await _invoice(api, contact, 500.0, steps=("finalize", "send", "pay"))
    await _invoice(api, contact, 300.0, steps=("finalize", "void"))
    await _invoice(api, contact, 700.0, steps=())
    await _invoice(api, contact, 100.0, currency=foreign, conversion_rate=2)
    # A credit note on the first invoice: it now owes 800, and the note itself is no invoice.
    cn = await _ok(await api.post("/docs", json={
        "doc_type": "credit_note", "original_doc_id": credited, "contact_id": contact["id"],
        "line_items": [{"name": "Credit", "quantity": 1, "unit_price": 200.0, "line_total": 200.0}],
        "total": 200.0}))
    await _ok(await api.post(f"/docs/{cn['id']}/finalize", json={}))
    assert float((await _ok(await api.get(f"/docs/{credited}")))["amount_outstanding"]) == 800.0

    r = await owner_ui.get(f"/contacts/{contact['id']}")
    assert r.status_code == 200, r.text
    cards = _cards(r.text)
    # Invoiced: 1000 + 500 + 100 at 2 = 1700; the void and the draft are not invoiced.
    assert cards["Total Invoiced"] == fmt_money(1700.0, None), cards
    # Paid: the payment received; the credit note is no payment.
    assert cards["Total Paid"] == fmt_money(500.0, None), cards
    # Outstanding: 800 on the credited invoice + 200 on the foreign one; the paid one owes nothing.
    assert cards["Outstanding"] == fmt_money(1000.0, None), cards
    # Each document shows its own total, in its own currency.
    for total, cur in ((1000.0, currency), (500.0, currency), (300.0, currency), (700.0, currency),
                       (200.0, currency), (100.0, foreign)):
        shown = format_value(total, "money", cur)
        assert f'cell--number">{shown}</td>' in r.text, shown
