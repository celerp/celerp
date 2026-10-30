# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A contact page's money cards and documents table show what the contact's documents hold,
counted as the invoice list counts them: issued invoices only (no draft, no void), in the
company currency, with a credit note on an invoice lowering what it still owes."""
from __future__ import annotations

import importlib.util
import re
from datetime import timedelta
from pathlib import Path

import pytest

from celerp.services.business_time import business_today
from test_helpers import mark_running
from ui.api_client import APIError
from ui.components.table import fmt_money, format_value

pytestmark = pytest.mark.asyncio

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_contact_cards", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


async def _ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


def _day(days_ago: int = 0) -> str:
    return (business_today(None) - timedelta(days=days_ago)).isoformat()


async def _pay(api, doc_id: str, amount: float, day: str) -> None:
    await _ok(await api.post(f"/docs/{doc_id}/payment", json={
        "amount": amount, "method": "cash", "payment_date": day, "bank_account": "1111"}))


async def _invoice(api, contact: dict, total: float, *, steps=("finalize", "send"), issued: int = 0, **extra) -> str:
    """An invoice for ``contact`` issued ``issued`` days ago (today by default), taken through
    ``steps`` as a user would."""
    today = _day(issued)
    doc = await _ok(await api.post("/docs", json={
        "doc_type": "invoice", "contact_id": contact["id"], "contact_name": contact["name"],
        "date": today, "issue_date": today, "due_date": today,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total, **extra}))
    for step in steps:
        if step == "pay":
            await _pay(api, doc["id"], total, today)
        elif step == "void":
            await _ok(await api.post(f"/docs/{doc['id']}/void", json={}))
        else:
            await _ok(await api.post(f"/docs/{doc['id']}/{step}", json={}))
    return doc["id"]


def _cards(html: str) -> dict[str, str]:
    """Each money card's label to the value it shows."""
    return {label.strip(): value.strip() for label, value in re.findall(
        r'class="financial-card-label">\s*<span>([^<]+)</span>.*?class="financial-card-value">([^<]*)<', html, re.S)}


async def _demo_contact(owner_ui, monkeypatch) -> dict:
    """A new customer in the demo company (its items, contacts and invoices on other customers)."""
    mark_running(monkeypatch, "celerp-docs")
    api = owner_ui.api
    tok = api.headers["Authorization"].split(" ", 1)[1]
    await seed_demo.seed_items(api, tok)
    contacts = await seed_demo.seed_contacts(api, tok)
    await seed_demo.seed_docs(api, tok, contacts, "invoice", 6)
    contact = await _ok(await api.post("/crm/contacts", json={"name": "Card Customer", "contact_type": "customer"}))
    return {"id": contact["id"], "name": "Card Customer"}


async def _contact_cards(owner_ui, contact: dict) -> dict[str, str]:
    r = await owner_ui.get(f"/contacts/{contact['id']}")
    assert r.status_code == 200, r.text
    return _cards(r.text)


async def test_contact_cards_and_documents_show_the_contact_s_invoices(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
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


async def test_avg_days_to_pay_counts_to_each_paid_invoice_s_last_active_payment(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
    # Issued 30 days ago, paid in two parts 25 and 10 days ago: 20 days to pay. A payment voided
    # 2 days ago no longer settles it.
    split = await _invoice(api, contact, 300.0, issued=30)
    await _pay(api, split, 300.0, _day(2))
    await _ok(await api.post(f"/docs/{split}/void-payment", json={"payment_index": 0, "refund_date": _day(2)}))
    await _pay(api, split, 100.0, _day(25))
    await _pay(api, split, 200.0, _day(10))
    # Issued 40 days ago, paid 30 days ago: 10 days to pay.
    once = await _invoice(api, contact, 100.0, issued=40)
    await _pay(api, once, 100.0, _day(30))
    # Still open, so it has not been paid and does not count.
    part = await _invoice(api, contact, 100.0, issued=50)
    await _pay(api, part, 50.0, _day(45))
    for doc_id in (split, once):
        assert (await _ok(await api.get(f"/docs/{doc_id}")))["status"] == "paid"

    cards = await _contact_cards(owner_ui, contact)
    assert cards["Avg Days to Pay"] == "15d", cards


async def test_avg_days_to_pay_is_empty_without_a_paid_invoice(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    await _invoice(owner_ui.api, contact, 100.0, issued=5)
    cards = await _contact_cards(owner_ui, contact)
    assert cards["Avg Days to Pay"] == "--", cards


def _card_links(html: str) -> dict[str, str]:
    """Each linked money card's label to the list it opens."""
    return {label.strip(): href.replace("&amp;", "&") for href, label in re.findall(
        r'<a href="([^"]+)" class="financial-card financial-card--link">\s*'
        r'<div class="financial-card-label">\s*<span>([^<]+)</span>', html, re.S)}


def _list_cards(html: str) -> dict[str, str]:
    """Each status card on a document list to the total it shows, in figures only."""
    return {label.strip(): re.sub(r"[^\d.,-]", "", total) for label, total in re.findall(
        r'class="status-card-label">([^<]+)</span>.*?class="status-card-total">([^<]*)<', html, re.S)}


async def test_each_contact_money_card_shows_the_figure_of_the_list_it_opens(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
    currency = (await _ok(await api.get("/companies/me"))).get("currency") or "USD"
    foreign = "EUR" if currency != "EUR" else "GBP"
    paid = await _invoice(api, contact, 500.0, steps=("finalize", "send", "pay"))
    partly = await _invoice(api, contact, 1000.0)
    await _pay(api, partly, 300.0, _day())
    # A foreign-currency invoice at rate 2, part paid: 200 invoiced, 100 paid, 100 owed.
    abroad = await _invoice(api, contact, 100.0, currency=foreign, conversion_rate=2)
    await _pay(api, abroad, 50.0, _day())
    unpaid = await _invoice(api, contact, 200.0)

    r = await owner_ui.get(f"/contacts/{contact['id']}")
    assert r.status_code == 200, r.text
    cards, links = _cards(r.text), _card_links(r.text)
    # The card each one opens on the invoice list, which counts the same invoices.
    for card, list_card in (("Total Invoiced", "All Issued"), ("Outstanding", "Awaiting Payment")):
        page = await owner_ui.get(links[card])
        assert page.status_code == 200, page.text
        assert _list_cards(page.text)[list_card] == cards[card], (card, links[card], cards)
    # Total Paid is every payment on this year's issued invoices, part payments included, and
    # opens those invoices: each one paid, in full or in part, is on the list it opens.
    assert cards["Total Paid"] == fmt_money(900.0, None), cards
    assert _sublabel(r.text, "Total Paid") == "Year To Date, Part Payments Included"
    page = await owner_ui.get(links["Total Paid"])
    assert page.status_code == 200, page.text
    assert _list_cards(page.text)["All Issued"] == cards["Total Invoiced"], (links["Total Paid"], cards)
    for doc_id in (paid, partly, abroad, unpaid):
        assert doc_id in page.text, (doc_id, links["Total Paid"])
    # The list it opens states the paid total beside what was billed and is still owed.
    chips = _summary_chips(page.text)
    assert chips["Paid"] == cards["Total Paid"], (chips, cards)
    assert chips["Billed"] == cards["Total Invoiced"], (chips, cards)
    assert chips["AR"] == cards["Outstanding"], (chips, cards)
    assert cards["Total Invoiced"] == fmt_money(1900.0, None), cards
    assert cards["Outstanding"] == fmt_money(1000.0, None), cards


async def test_the_chips_over_a_filtered_contact_list_total_only_the_rows_it_shows(owner_ui, monkeypatch):
    """The Outstanding card opens the contact's invoices awaiting payment. The chips above that
    list follow its filters, like the status cards, so they total those invoices only and not
    the contact's paid one, while the status cards still split the contact's invoices."""
    contact = await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
    await _invoice(api, contact, 500.0, steps=("finalize", "send", "pay"))
    partly = await _invoice(api, contact, 1000.0)
    await _pay(api, partly, 300.0, _day())
    unpaid = await _invoice(api, contact, 200.0)

    r = await owner_ui.get(f"/contacts/{contact['id']}")
    assert r.status_code == 200, r.text
    page = await owner_ui.get(_card_links(r.text)["Outstanding"])
    assert page.status_code == 200, page.text
    rows = set(re.findall(r'href="/docs/([^"/?]+)"', page.text))
    assert {partly, unpaid} <= rows, rows
    chips = _summary_chips(page.text)
    assert chips["Invoices"] == "2", chips
    assert chips["Billed"] == fmt_money(1200.0, None), chips
    assert chips["Paid"] == fmt_money(300.0, None), chips
    assert chips["AR"] == fmt_money(900.0, None), chips
    cards = _list_cards(page.text)
    assert cards["Paid"] == re.sub(r"[^\d.,-]", "", fmt_money(500.0, None)), cards


def _summary_chips(html: str) -> dict[str, str]:
    """Each summary figure of a document list (the /docs header line, or a chip), label to the
    figure it states."""
    return {label.strip(): value.strip() for label, value in re.findall(
        r'class="(?:val-chip(?: val-chip--alert)?|doc-summary-item(?: doc-summary-alert)?)">([^:<]+): ([^<]*)<', html)}


def _sublabel(html: str, label: str) -> str | None:
    """The small line under a money card's label, or None when it has none."""
    m = re.search(rf'class="financial-card-label">\s*<span>{re.escape(label)}</span>\s*'
                  r'(?:<span class="financial-card-sublabel">([^<]*)</span>)?', html)
    assert m, f"no {label} card"
    return m.group(1)


async def test_outstanding_shows_the_contact_s_open_credit_under_it(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
    currency = (await _ok(await api.get("/companies/me"))).get("currency") or "USD"
    foreign = "EUR" if currency != "EUR" else "GBP"
    await _invoice(api, contact, 250.0)
    r = await owner_ui.get(f"/contacts/{contact['id']}")
    assert _sublabel(r.text, "Outstanding") is None

    async def credit_note(total: float, **extra) -> None:
        cn = await _ok(await api.post("/docs", json={
            "doc_type": "credit_note", "contact_id": contact["id"], "contact_name": contact["name"],
            "line_items": [{"name": "Credit", "quantity": 1, "unit_price": total, "line_total": total}],
            "total": total, **extra}))
        await _ok(await api.post(f"/docs/{cn['id']}/finalize", json={}))

    # Two credit notes not applied to any invoice, one in a foreign currency at rate 2, and a
    # draft one, which is no credit yet: 15 + 10 at 2 = 35 open, in the company currency.
    await credit_note(15.0)
    await credit_note(10.0, currency=foreign, conversion_rate=2)
    await _ok(await api.post("/docs", json={
        "doc_type": "credit_note", "contact_id": contact["id"],
        "line_items": [{"name": "Credit", "quantity": 1, "unit_price": 40.0, "line_total": 40.0}], "total": 40.0}))

    r = await owner_ui.get(f"/contacts/{contact['id']}")
    cards = _cards(r.text)
    # Outstanding stays what the awaiting-payment list shows; the open credit is shown under it.
    assert cards["Outstanding"] == fmt_money(250.0, None), cards
    assert _sublabel(r.text, "Outstanding") == f"Open credit {fmt_money(35.0, None)}"


def _count_reads(monkeypatch, name: str, error: APIError | None = None) -> list:
    """Record each call the UI makes to api.``name``; with ``error``, each one raises it."""
    import ui.api_client as api_client
    calls, real = [], getattr(api_client, name)

    async def read(*args, **kwargs):
        calls.append(args[1:])
        if error is not None:
            raise error
        return await real(*args, **kwargs)
    monkeypatch.setattr(api_client, name, read)
    return calls


async def test_contact_page_reads_no_money_figures_while_documents_is_off(owner_ui, monkeypatch, docs_not_started):
    contact = await _ok(await owner_ui.api.post("/crm/contacts", json={"name": "No Docs", "contact_type": "customer"}))
    summaries, holdings = _count_reads(monkeypatch, "get_doc_summary"), _count_reads(monkeypatch, "list_items")
    r = await owner_ui.get(f"/contacts/{contact['id']}")
    assert r.status_code == 200, r.text
    # The cards are hidden, so nothing behind them is read.
    assert "financial-cards" not in r.text
    assert summaries == [] and holdings == []


async def test_a_session_ending_while_the_cards_load_goes_to_sign_in(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    _count_reads(monkeypatch, "get_doc_summary", APIError(401, "Session expired"))
    r = await owner_ui.get(f"/contacts/{contact['id']}", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    assert "/login" in r.headers["location"]


async def test_a_failed_summary_read_shows_the_cards_empty(owner_ui, monkeypatch):
    contact = await _demo_contact(owner_ui, monkeypatch)
    _count_reads(monkeypatch, "get_doc_summary", APIError(503, "Unavailable"))
    cards = await _contact_cards(owner_ui, contact)
    for card in ("Total Invoiced", "Total Paid", "Outstanding"):
        assert cards[card] == "--", cards


async def test_the_paid_summary_chip_has_its_own_wording(monkeypatch):
    """The Paid chip reads the chip key, like Billed and Invoices beside it, so a language
    can word the short chip apart from the Paid label on a document."""
    from fasthtml.common import to_xml
    import ui.i18n as i18n
    from ui.routes.documents import _doc_summary_line

    load = i18n._cached_load
    monkeypatch.setattr(i18n, "_cached_load", lambda lang: {**load(lang), "chip.paid": "Settled"})
    chips = _summary_chips(to_xml(_doc_summary_line({}, {"ar_paid": 75, "ar_total": 100}, {}, currency="USD")))
    assert chips["Settled"] == fmt_money(75.0, "USD"), chips
    assert "Paid" not in chips, chips


@pytest.mark.parametrize("status", ["draft", "void"])
async def test_a_view_of_unissued_documents_states_their_count_and_total(owner_ui, monkeypatch, status):
    """A drafts or voids view holds no receivable, billing or payment. A typed list shows how
    many rows it holds and what they total; the all-types list counts them only, since its total
    would add a quotation to invoices. A new company's drafts fill more than one page (50 rows):
    the bar and the status card count and total all of them, not the page."""
    from urllib.parse import parse_qs, urlparse

    mark_running(monkeypatch, "celerp-docs")
    api = owner_ui.api
    contact = await _ok(await api.post("/crm/contacts", json={"name": "Draft Customer", "contact_type": "customer"}))
    contact = {"id": contact["id"], "name": "Draft Customer"}
    steps = () if status == "draft" else ("finalize", "void")
    for _ in range(55):
        await _invoice(api, contact, 10.0, steps=steps)
    quote = await _ok(await api.post("/docs", json={
        "doc_type": "quotation", "contact_id": contact["id"], "contact_name": contact["name"], "issue_date": _day(),
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": 70.0, "line_total": 70.0}], "total": 70.0}))
    if status == "void":
        await _ok(await api.post(f"/docs/{quote['id']}/void", json={}))
    if status == "draft":
        # A first visit finds no issued document and opens the drafts, keeping the filters.
        r = await owner_ui.get(f"/docs?contact_id={contact['id']}&preset=all")
        assert r.status_code == 302, r.status_code
        assert parse_qs(urlparse(r.headers["location"]).query) == {
            "view": ["drafts"], "contact_id": [contact["id"]], "preset": ["all"]}, r.headers["location"]
        view = r.headers["location"]
    else:
        view = f"/docs?status=void&contact_id={contact['id']}&preset=all"
    page = await owner_ui.get(view)
    assert page.status_code == 200, page.text[:300]
    assert _summary_chips(page.text) == {"Documents": "56"}, _summary_chips(page.text)
    card = "Draft" if status == "draft" else "Void"
    # The card for the rows shown is the one marked.
    assert re.search(rf'status-card--active"[^>]*>\s*<span class="status-card-label">{card}<', page.text), card
    assert re.search(rf'status-card-label">{card}</span>\s*<span class="status-card-count">56</span>\s*</', page.text), card
    typed = await owner_ui.get(view + "&type=invoice")
    assert _summary_chips(typed.text) == {"Invoices": "55", "Total": fmt_money(550.0, None)}, _summary_chips(typed.text)
    typed_card = "Pro Forma" if status == "draft" else "Void"
    assert re.search(rf'status-card-label">{typed_card}</span>\s*<span class="status-card-count">55</span>\s*'
                     rf'<span class="status-card-total">{re.escape(fmt_money(550.0, None))}<', typed.text), typed_card


async def test_a_view_with_issued_invoices_keeps_its_money_chips():
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_summary_line

    issued = _summary_chips(to_xml(_doc_summary_line({"type": "invoice"}, {"total_count": 1, "invoice_count": 1, "ar_total": 40.0}, {},
                                                     currency="USD")))
    assert issued["Billed"] == fmt_money(40.0, "USD"), issued


async def test_money_headers_sit_over_their_figures(owner_ui, monkeypatch):
    """A money column's header is right-aligned over its right-aligned figures, on the document
    list and on a contact's documents."""
    contact = await _demo_contact(owner_ui, monkeypatch)
    await _invoice(owner_ui.api, contact, 10.0)
    for url, labels in ((f"/contacts/{contact['id']}", ("Total",)), ("/docs?type=invoice", ("Total", "Outstanding"))):
        r = await owner_ui.get(url)
        assert r.status_code == 200, r.text
        for label in labels:
            assert re.search(rf'<th class="cell--number">\s*(?:<a [^>]*>\s*)?{label}\b', r.text), (url, label)
