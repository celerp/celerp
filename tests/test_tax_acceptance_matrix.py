# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every document type charges the same tax for the same line, and books exactly that.

The matrix runs on a demo company in each currency precision (JPY, USD, KWD), for an invoice,
a bill, a purchase order, an inbound consignment, a memo and a list, with the line's tax in each
state it can hold: still to be chosen ("--"), No Tax, one tax, two taxes, a legacy rate, a tax
on the whole document and a compound tax. The line is three at 1234 / 12.34 / 12.341.

A line still to be chosen is refused at Issue and changes nothing; once No Tax is chosen it
issues with no tax. Every other state issues with the tax worked out by hand below, the document
stores it, and the ledger moves by exactly that: an invoice adds its total to receivables and
its tax to output tax, a bill or purchase order adds its total to payables and its tax to input
tax, and a consignment, a memo or a list books nothing. The later tests walk the steps that move
goods or money (edit, two tabs, receive, fulfil, convert, return, revert, void) and check stock
and the ledger after every one, refused or done.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.money import round_money, to_decimal
from test_money_stock_and_contact_invariants import _auth_company

_SEED = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo", _SEED)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


def _tax(code: str, rate: float, order: int = 0, compound: bool = False) -> dict:
    return {"code": code, "rate": rate, "order": order, "is_compound": compound, "label": ""}


VAT = [_tax("VAT 7%", 7)]
STATES = {
    "unset": {"taxes": None},
    "no_tax": {"taxes": []},
    "one": {"taxes": VAT},
    "two": {"taxes": [_tax("CGST 9%", 9), _tax("SGST 9%", 9, order=1)]},
    "legacy_rate": {"tax_rate": 7},
    "document": {"taxes": []},  # VAT 7% on the whole document
    "compound": {"taxes": [_tax("T1", 10), _tax("T2", 5, order=1, compound=True)]},
}

# currency -> unit price, subtotal, tax by state (worked by hand at the currency's precision).
ORACLE = {
    "JPY": (1234, 3702, {"one": 259, "two": 666, "legacy_rate": 259, "document": 259, "compound": 574}),
    "USD": (12.34, 37.02, {"one": 2.59, "two": 6.66, "legacy_rate": 2.59, "document": 2.59, "compound": 5.74}),
    "KWD": (12.341, 37.023, {"one": 2.592, "two": 6.664, "legacy_rate": 2.592, "document": 2.592,
                             "compound": 5.738}),
}

INBOUND = ("bill", "purchase_order", "consignment_in")
DOC_TYPES = ("invoice", "bill", "purchase_order", "consignment_in", "memo", "list")


async def _demo_company(client, session, currency: str) -> tuple[dict, list[dict]]:
    auth = await _auth_company(session, currency)
    token = auth["headers"]["Authorization"].split()[1]
    await seed_demo.seed_items(client, token)
    return auth, await seed_demo.seed_contacts(client, token)


def _contact(contacts: list[dict], doc_type: str) -> str:
    side = "vendor" if doc_type in INBOUND else "customer"
    return next(c["id"] for c in contacts if c.get("contact_type") in (side, "both"))


async def _books(session, auth) -> tuple[dict, dict, int]:
    """The ledger by account role (debit minus credit of posted journal entries), every item's
    quantity and the company's event count."""
    session.expire_all()
    cid = auth["company_id"]
    rows = (await session.execute(select(Projection).where(Projection.company_id == cid))).scalars().all()
    roles: dict = {}
    for row in rows:
        if row.entity_type == "journal_entry" and row.state.get("status") == "posted":
            for entry in row.state.get("entries") or []:
                for role in entry.get("account_roles") or []:
                    roles[role] = round(roles.get(role, 0) + float(entry.get("debit") or 0)
                                        - float(entry.get("credit") or 0), 6)
    stock = {row.entity_id: row.state.get("quantity") for row in rows if row.entity_type == "item"}
    events = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid))).scalar_one()
    return roles, stock, events


def _moved(before: dict, after: dict) -> dict:
    return {k: round(after.get(k, 0) - before.get(k, 0), 6) for k in set(before) | set(after)
            if round(after.get(k, 0) - before.get(k, 0), 6)}


def _expected_roles(doc_type: str, total, tax) -> dict:
    if doc_type == "invoice":
        moved = {"receivable": total, "sales_revenue": -(total - tax), "tax_output": -tax}
    elif doc_type in ("bill", "purchase_order"):
        # A service line (no item) is an expense.
        moved = {"payable": -total, "general_expense": total - tax, "tax_input": tax}
    else:
        return {}
    return {k: round(v, 6) for k, v in moved.items() if round(v, 6)}


async def _create(client, auth, contacts, doc_type: str, currency: str, state: str) -> tuple[str, str]:
    price = ORACLE[currency][0]
    line = {"description": "Service", "quantity": 3, "unit_price": price, **STATES[state]}
    if doc_type == "list":
        body = {"list_type": "quotation", "currency": currency, "line_items": [line]}
        if state == "document":
            body["tax"] = 7  # a list's tax on the whole list is its header rate
        r = await client.post("/lists", headers=auth["headers"], json=body)
        kind = "lists"
    else:
        body = {"doc_type": doc_type, "currency": currency, "contact_id": _contact(contacts, doc_type),
                "line_items": [line]}
        if state == "document":
            body["doc_taxes"] = VAT
        r = await client.post("/docs", headers=auth["headers"], json=body)
        kind = "docs"
    assert r.status_code == 200, r.text
    return kind, r.json()["id"]


async def _get(client, auth, kind: str, entity_id: str) -> dict:
    r = await client.get(f"/{kind}/{entity_id}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _tax_of(kind: str, doc: dict):
    return doc["tax_amount"] if kind == "lists" else doc["tax"]


async def _choose_no_tax(client, auth, kind: str, entity_id: str) -> None:
    doc = await _get(client, auth, kind, entity_id)
    lines = [{**line, "taxes": []} for line in doc["line_items"]]
    r = await client.patch(f"/{kind}/{entity_id}", headers=auth["headers"], json={
        "expected_version": doc["version"],
        "fields_changed": {"line_items": {"old": doc["line_items"], "new": lines}}})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("currency", list(ORACLE))
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("doc_type", DOC_TYPES)
async def test_issue_charges_and_books_the_tax_worked_by_hand(client, session, doc_type, state, currency):
    auth, contacts = await _demo_company(client, session, currency)
    _, subtotal, taxes = ORACLE[currency]
    kind, entity_id = await _create(client, auth, contacts, doc_type, currency, state)
    before = await _books(session, auth)

    if state == "unset":
        r = await client.post(f"/{kind}/{entity_id}/finalize", headers=auth["headers"])
        assert r.status_code == 422, r.text
        assert await _books(session, auth) == before
        assert (await _get(client, auth, kind, entity_id))["status"] == "draft"
        await _choose_no_tax(client, auth, kind, entity_id)
        before = await _books(session, auth)

    draft = await _get(client, auth, kind, entity_id)
    tax = taxes.get(state, 0)
    total = round(subtotal + tax, 6)
    assert (draft["subtotal"], _tax_of(kind, draft), draft["total"]) == (
        pytest.approx(subtotal), pytest.approx(tax), pytest.approx(total))

    r = await client.post(f"/{kind}/{entity_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    issued = await _get(client, auth, kind, entity_id)
    assert issued["status"] != "draft"
    assert (issued["subtotal"], _tax_of(kind, issued), issued["total"]) == (
        pytest.approx(subtotal), pytest.approx(tax), pytest.approx(total))
    roles, stock, _ = await _books(session, auth)
    assert _moved(before[0], roles) == pytest.approx(_expected_roles(doc_type, total, tax))
    assert stock == before[1]


# ---------------------------------------------------------------------------
# Steps that move goods or money, on demo items, with stock and the ledger checked after each.
# ---------------------------------------------------------------------------

def _usd(value) -> float:
    return round(float(value) + 0.0, 2)


def _vat(subtotal: float) -> float:
    return float(round_money(to_decimal(subtotal) * 7 / 100, "USD"))


async def _stocked_item(client, auth, minimum: float = 3) -> dict:
    items = (await client.get("/items", params={"limit": 50}, headers=auth["headers"])).json()["items"]
    return next(i for i in items if float(i.get("quantity") or 0) >= minimum and i.get("sell_by") == "piece")


async def _patch_lines(client, auth, kind, entity_id, lines, version):
    return await client.patch(f"/{kind}/{entity_id}", headers=auth["headers"], json={
        "expected_version": version, "fields_changed": {"line_items": {"new": lines}}})


async def _post(client, auth, path: str, body: dict | None = None):
    return await client.post(path, headers=auth["headers"], json=body or {})


async def test_an_invoice_edited_in_two_tabs_issued_fulfilled_and_voided_keeps_its_books(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    item = await _stocked_item(client, auth)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": _contact(contacts, "invoice"), "line_items": [
            {"item_id": item["id"], "sku": item["sku"], "name": item["name"], "quantity": 3, "taxes": VAT}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    tab_a = tab_b = await _get(client, auth, "docs", inv)
    price = float(tab_a["line_items"][0]["unit_price"])

    # Tab A saves two; tab B, still on the version it opened, is refused and changes nothing.
    before = await _books(session, auth)
    # The editor sends a line's total beside its quantity.
    line = {**tab_a["line_items"][0], "quantity": 2, "line_total": round(2 * price, 2)}
    r = await _patch_lines(client, auth, "docs", inv, [line], tab_a["version"])
    assert r.status_code == 200, r.text
    after_a = await _books(session, auth)
    r = await _patch_lines(client, auth, "docs", inv, [{**tab_b["line_items"][0], "quantity": 1}], tab_b["version"])
    assert r.status_code == 409, r.text
    assert await _books(session, auth) == after_a
    assert after_a[:2] == before[:2]
    doc = await _get(client, auth, "docs", inv)
    subtotal = _usd(2 * price)
    tax = _vat(subtotal)
    assert doc["line_items"][0]["quantity"] == 2
    assert (doc["subtotal"], doc["tax"], doc["total"]) == (
        pytest.approx(subtotal), pytest.approx(tax), pytest.approx(subtotal + tax))

    # Issue books receivable, revenue and output tax, plus the goods' cost out of inventory;
    # stock leaves on fulfilment only.
    before = await _books(session, auth)
    assert (await _post(client, auth, f"/docs/{inv}/finalize")).status_code == 200
    issued = await _books(session, auth)
    moved = _moved(before[0], issued[0])
    sale = {k: v for k, v in moved.items() if k in ("receivable", "sales_revenue", "tax_output")}
    assert sale == pytest.approx(_expected_roles("invoice", subtotal + tax, tax))
    cost = {k: v for k, v in moved.items() if k not in sale}
    inventory = [k for k in cost if k != "cogs"]
    assert len(inventory) == 1 and cost["cogs"] == pytest.approx(-cost[inventory[0]])
    assert issued[1] == before[1]
    r = await _post(client, auth, f"/docs/{inv}/fulfill-lines", {"line_entity_ids": [item["id"]]})
    assert r.status_code == 200, r.text
    fulfilled = await _books(session, auth)
    assert fulfilled[1][item["id"]] == pytest.approx(float(item["quantity"]) - 2)
    moved = _moved(issued[0], fulfilled[0])
    assert {"receivable", "tax_output", "sales_revenue"}.isdisjoint(moved)

    # Voiding takes the sale and its tax back off the books.
    r = await _post(client, auth, f"/docs/{inv}/void", {"reason": "test"})
    voided = await _books(session, auth)
    if r.status_code == 200:
        assert {k: v for k, v in _moved(before[0], voided[0]).items()
                if k in ("receivable", "tax_output", "sales_revenue")} == {}
    else:
        assert r.status_code == 409, r.text
        assert voided == fulfilled


async def test_an_invoice_reverted_to_draft_and_issued_again_books_its_tax_once(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    kind, inv = await _create(client, auth, contacts, "invoice", "USD", "compound")
    before = await _books(session, auth)
    assert (await _post(client, auth, f"/docs/{inv}/finalize")).status_code == 200
    assert (await _post(client, auth, f"/docs/{inv}/revert-to-draft", {"reason": "test"})).status_code == 200
    reverted = await _books(session, auth)
    assert _moved(before[0], reverted[0]) == {}
    assert (await _get(client, auth, kind, inv))["status"] == "draft"
    assert (await _post(client, auth, f"/docs/{inv}/finalize")).status_code == 200
    again = await _books(session, auth)
    assert _moved(before[0], again[0]) == pytest.approx(_expected_roles("invoice", 37.02 + 5.74, 5.74))


async def test_a_purchase_order_received_issued_and_partly_returned_keeps_stock_and_payables(client, session):
    auth, contacts = await _demo_company(client, session, "USD")
    item = await _stocked_item(client, auth, minimum=0)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "purchase_order", "contact_id": _contact(contacts, "purchase_order"), "line_items": [
            {"item_id": item["id"], "sku": item["sku"], "name": item["name"], "quantity": 3,
             "unit_price": 10.0, "taxes": VAT}]})
    assert r.status_code == 200, r.text
    po = r.json()["id"]
    start = await _books(session, auth)
    r = await _post(client, auth, f"/docs/{po}/receive", {"location_id": "", "received_items": [
        {"item_id": item["id"], "sku": item["sku"], "name": item["name"], "quantity_received": 3,
         "po_line_index": 0, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    received = await _books(session, auth)
    # Receipt books the goods at cost to the item's inventory account against payables.
    goods = _moved(start[0], received[0])
    inventory = next(k for k in goods if k != "payable")
    assert goods == pytest.approx({"payable": -30.0, inventory: 30.0})

    assert (await _post(client, auth, f"/docs/{po}/finalize")).status_code == 200
    issued = await _books(session, auth)
    # Issue adds the tax, and only the tax.
    assert _moved(received[0], issued[0]) == pytest.approx({"payable": -2.1, "tax_input": 2.1})
    assert issued[1] == received[1]

    r = await _post(client, auth, f"/docs/{po}/return-items", {"items": [{"item_id": item["id"], "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    returned = await _books(session, auth)
    assert returned[1][item["id"]] == pytest.approx(float(item["quantity"]) + 2)
    assert _moved(start[0], returned[0]) == pytest.approx(
        {"payable": -21.4, inventory: 20.0, "tax_input": 1.4})


@pytest.mark.parametrize("state", ["one", "two", "legacy_rate", "compound"])
async def test_a_quotation_converts_to_an_invoice_charging_the_same_tax(client, session, state):
    auth, contacts = await _demo_company(client, session, "USD")
    kind, lst = await _create(client, auth, contacts, "list", "USD", state)
    quotation = await _get(client, auth, kind, lst)
    assert (await _post(client, auth, f"/lists/{lst}/finalize")).status_code == 200
    before = await _books(session, auth)
    r = await _post(client, auth, f"/lists/{lst}/convert", {"target_type": "invoice"})
    assert r.status_code == 200, r.text
    inv = r.json()["target_doc_id"]
    invoice = await _get(client, auth, "docs", inv)
    assert (invoice["status"], invoice["subtotal"], invoice["tax"], invoice["total"]) == (
        "draft", quotation["subtotal"], quotation["tax_amount"], quotation["total"])
    assert (await _books(session, auth))[:2] == before[:2]
    assert (await _post(client, auth, f"/docs/{inv}/finalize")).status_code == 200
    assert _moved(before[0], (await _books(session, auth))[0]) == pytest.approx(
        _expected_roles("invoice", quotation["total"], quotation["tax_amount"]))


async def test_a_quotation_taxed_on_the_whole_list_converts_to_an_invoice_charging_that_tax(client, session):
    """A list's header ``tax`` is a rate; the invoice it converts to holds its tax as an amount."""
    auth, contacts = await _demo_company(client, session, "USD")
    kind, lst = await _create(client, auth, contacts, "list", "USD", "document")
    quotation = await _get(client, auth, kind, lst)
    assert (quotation["tax"], quotation["tax_amount"]) == (7, pytest.approx(2.59))
    assert (await _post(client, auth, f"/lists/{lst}/finalize")).status_code == 200
    before = await _books(session, auth)
    r = await _post(client, auth, f"/lists/{lst}/convert", {"target_type": "invoice"})
    assert r.status_code == 200, r.text
    inv = r.json()["target_doc_id"]
    invoice = await _get(client, auth, "docs", inv)
    assert (invoice["subtotal"], invoice["tax"], invoice["total"]) == (
        pytest.approx(37.02), pytest.approx(2.59), pytest.approx(39.61))
    assert (await _post(client, auth, f"/docs/{inv}/finalize")).status_code == 200
    assert _moved(before[0], (await _books(session, auth))[0]) == pytest.approx(
        _expected_roles("invoice", 39.61, 2.59))


_VAT10 = [_tax("VAT 10%", 10)]
# case -> currency, header discount, lines, (subtotal, discount, tax, total) worked by hand: the header
# discount comes off before tax and each line's tax is rounded on its own, as on every document.
QUOTE_CASES = {
    "usd_header_pct": ("USD", {"discount": 1, "discount_type": "percentage"},
                       [{"quantity": 1, "unit_price": 100, "taxes": _VAT10}], (100, 1, 9.90, 108.90)),
    "usd_header_flat_rate_only": ("USD", {"discount": 10, "discount_type": "flat"},
                                  [{"quantity": 1, "unit_price": 100, "tax_rate": 10}], (100, 10, 9, 99)),
    "jpy_header_pct": ("JPY", {"discount": 10, "discount_type": "percentage"},
                       [{"quantity": 3, "unit_price": 2345, "taxes": _VAT10}], (7035, 704, 633, 6964)),
    "kwd_header_flat": ("KWD", {"discount": 1.111, "discount_type": "flat"},
                        [{"quantity": 1, "unit_price": 98.765, "taxes": _VAT10}], (98.765, 1.111, 9.765, 107.419)),
    "jpy_rate_only_per_line": ("JPY", {}, [{"quantity": 1, "unit_price": 15, "tax_rate": 10}] * 3, (45, 0, 6, 51)),
    "usd_line_discount_pct": ("USD", {}, [{"quantity": 1, "unit_price": 100, "discount_pct": 10, "taxes": _VAT10}],
                              (90, 0, 9, 99)),
}


@pytest.mark.parametrize("case", sorted(QUOTE_CASES))
async def test_a_quotation_totals_as_the_invoice_it_converts_to(client, session, case):
    """A quotation's money follows the document rule: the invoice made from it carries the same
    subtotal, discount, tax and total, and books exactly that."""
    currency, header, lines, expected = QUOTE_CASES[case]
    auth, _ = await _demo_company(client, session, currency)
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": currency, **header,
        "line_items": [{"description": f"Service {i}", **line} for i, line in enumerate(lines)]})
    assert r.status_code == 200, r.text
    lst = r.json()["id"]
    quotation = await _get(client, auth, "lists", lst)
    assert (quotation["subtotal"], quotation["discount_amount"], quotation["tax_amount"], quotation["total"]) == (
        pytest.approx(expected[0]), pytest.approx(expected[1]), pytest.approx(expected[2]), pytest.approx(expected[3]))
    assert (await _post(client, auth, f"/lists/{lst}/finalize")).status_code == 200
    before = await _books(session, auth)
    r = await _post(client, auth, f"/lists/{lst}/convert", {"target_type": "invoice"})
    assert r.status_code == 200, r.text
    inv = r.json()["target_doc_id"]
    invoice = await _get(client, auth, "docs", inv)
    assert (invoice["subtotal"], invoice["discount_amount"], invoice["tax"], invoice["total"]) == (
        quotation["subtotal"], quotation["discount_amount"], quotation["tax_amount"], quotation["total"])
    assert (await _post(client, auth, f"/docs/{inv}/finalize")).status_code == 200
    moved = _moved(before[0], (await _books(session, auth))[0])
    assert {k: moved.get(k) for k in ("receivable", "tax_output")} == pytest.approx(
        {"receivable": expected[3], "tax_output": -expected[2]})


async def test_a_line_discount_is_charged_the_same_through_every_way_in(client, session):
    """Class guard: a line given a discount_pct and no line_total (as the API and a CSV import send it)
    is one amount everywhere: a list created and edited, a document created, edited and imported,
    and the invoice a quotation converts to. 3 x 12.34 less 10% is 33.32."""
    auth, contacts = await _demo_company(client, session, "USD")
    line = {"description": "Service", "quantity": 3, "unit_price": 12.34, "discount_pct": 10}
    amounts = {}
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "line_items": [line]})
    assert r.status_code == 200, r.text
    lst = r.json()["id"]
    created = await _get(client, auth, "lists", lst)
    amounts["list create"] = created["subtotal"]
    r = await client.patch(f"/lists/{lst}", headers=auth["headers"], json={
        "expected_version": created["version"],
        "fields_changed": {"line_items": {"new": [line, {**line, "description": "Second"}]}}})
    assert r.status_code == 200, r.text
    amounts["list edit"] = (await _get(client, auth, "lists", lst))["subtotal"] / 2
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "currency": "USD", "contact_id": _contact(contacts, "invoice"), "line_items": [line]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    created = await _get(client, auth, "docs", doc)
    amounts["document create"] = created["subtotal"]
    r = await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "expected_version": created["version"],
        "fields_changed": {"line_items": {"new": [line, {**line, "description": "Second"}]}}})
    assert r.status_code == 200, r.text
    amounts["document edit"] = (await _get(client, auth, "docs", doc))["subtotal"] / 2
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": "doc:IMP-disc", "event_type": "doc.created", "source": "csv", "idempotency_key": "doc:IMP-disc",
        "data": {"doc_type": "invoice", "status": "draft", "currency": "USD", "line_items": [line]}})
    assert r.status_code == 200, r.text
    amounts["document import"] = (await _get(client, auth, "docs", "doc:IMP-disc"))["subtotal"]
    assert (await _post(client, auth, f"/lists/{lst}/finalize")).status_code == 200
    r = await _post(client, auth, f"/lists/{lst}/convert", {"target_type": "invoice"})
    assert r.status_code == 200, r.text
    amounts["conversion"] = (await _get(client, auth, "docs", r.json()["target_doc_id"]))["subtotal"] / 2
    assert amounts == {way: pytest.approx(33.32) for way in amounts}


@pytest.mark.parametrize("state", ["one", "two", "legacy_rate", "compound"])
async def test_an_imported_draft_takes_its_money_from_its_lines(client, session, state):
    auth, _ = await _demo_company(client, session, "USD")
    eid = f"doc:IMP-{state}"
    line = {"description": "Service", "quantity": 3, "unit_price": 12.34, **STATES[state]}
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": eid,
        "data": {"doc_type": "invoice", "status": "draft", "currency": "USD", "line_items": [line]}})
    assert r.status_code == 200, r.text
    doc = await _get(client, auth, "docs", eid)
    tax = ORACLE["USD"][2][state]
    assert (doc["subtotal"], doc["tax"], doc["total"]) == (
        pytest.approx(37.02), pytest.approx(tax), pytest.approx(37.02 + tax))
