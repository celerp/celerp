# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A list charges the taxes on its lines.

A line on a list holds its tax as a legacy ``tax_rate`` or as a ``taxes`` list (what a new line
takes from the company's default taxes). The list's tax is the sum of its lines' taxes: a
``tax_rate`` as it always was, and ``taxes`` by the one tax rule documents use (in order,
compound on the base plus the taxes before it), at the list currency's precision. A line holding
both is charged its taxes, and a header rate applies only when no line has a tax of its own. Example: three
at 12.34 with VAT 7% is tax 2.59 and total 39.61 in USD.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from test_money_stock_and_contact_invariants import _auth_company

_SEED = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo", _SEED)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


def _tax(code: str, rate: float, order: int = 0, compound: bool = False) -> dict:
    return {"code": code, "rate": rate, "order": order, "is_compound": compound, "label": ""}


VAT = [_tax("VAT 7%", 7)]
GST = [_tax("CGST 9%", 9), _tax("SGST 9%", 9, order=1)]
COMPOUND = [_tax("T1", 10), _tax("T2", 5, order=1, compound=True)]

# currency -> (unit price, subtotal, {tax state: tax}) for three on one line.
ORACLE = {
    "JPY": (1234, 3702, {"vat": 259, "gst": 666, "compound": 574, "legacy": 259}),
    "USD": (12.34, 37.02, {"vat": 2.59, "gst": 6.66, "compound": 5.74, "legacy": 2.59}),
    "KWD": (12.341, 37.023, {"vat": 2.592, "gst": 6.664, "compound": 5.738, "legacy": 2.592}),
}


def _line(state: str, price) -> dict:
    line = {"description": "Service", "quantity": 3, "unit_price": price}
    if state == "legacy":
        line["tax_rate"] = 7
    else:
        line["taxes"] = {"vat": VAT, "gst": GST, "compound": COMPOUND}[state]
    return line


async def _demo_company(client, session, currency: str) -> dict:
    auth = await _auth_company(session, currency)
    token = auth["headers"]["Authorization"].split()[1]
    await seed_demo.seed_items(client, token)
    return auth


async def _list(client, auth, lid: str) -> dict:
    r = await client.get(f"/lists/{lid}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("currency", list(ORACLE))
@pytest.mark.parametrize("state", ["vat", "gst", "compound", "legacy"])
async def test_a_list_charges_its_line_taxes(client, session, currency, state):
    auth = await _demo_company(client, session, currency)
    price, subtotal, taxes = ORACLE[currency]
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": currency, "line_items": [_line(state, price)]})
    assert r.status_code == 200, r.text
    lst = await _list(client, auth, r.json()["id"])
    assert lst["subtotal"] == pytest.approx(subtotal)
    assert lst["tax_amount"] == pytest.approx(taxes[state])
    assert lst["total"] == pytest.approx(subtotal + taxes[state])


async def test_a_line_with_a_rate_and_taxes_is_charged_once(client, session):
    """The editor saves a line's rate beside its taxes; the line is taxed once, by its taxes."""
    auth = await _demo_company(client, session, "USD")
    line = {**_line("vat", 12.34), "tax_rate": 7}
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "line_items": [line]})
    lst = await _list(client, auth, r.json()["id"])
    assert (lst["tax_amount"], lst["total"]) == (pytest.approx(2.59), pytest.approx(39.61))


async def test_line_taxes_count_after_an_edit_and_beside_a_header_discount(client, session):
    """A line given taxes by an edit is charged them, on its amount less the header discount, as a
    document charges it: 37.02 less 10 is 27.02, taxed 7%."""
    auth = await _demo_company(client, session, "USD")
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "discount": 10, "discount_type": "flat",
        "line_items": [{"description": "Service", "quantity": 3, "unit_price": 12.34}]})
    lid = r.json()["id"]
    lst = await _list(client, auth, lid)
    assert lst["tax_amount"] == 0
    lines = [{**lst["line_items"][0], "taxes": VAT}]
    r = await client.patch(f"/lists/{lid}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"old": lst["line_items"], "new": lines}},
                                 "expected_version": lst["version"]})
    assert r.status_code == 200, r.text
    lst = await _list(client, auth, lid)
    assert lst["tax_amount"] == pytest.approx(1.89)
    assert lst["total"] == pytest.approx(37.02 - 10 + 1.89)
    assert lst["line_items"][0]["taxes"][0]["amount"] == pytest.approx(1.89)


# The line the list editor saves (_celerpCollectLines): its taxes, and beside them the sum of
# their rates as ``tax_rate`` (and, once saved, its ``line_id``). 10% then 5% compound on 100, at
# each currency's precision.
EDITOR_ORACLE = {"USD": (100, 15.5), "JPY": (100, 16), "KWD": (100, 15.5)}


def _editor_line(price) -> dict:
    return {"description": "Service", "sku": "", "quantity": 1, "unit": "",
            "unit_price": price, "discount_pct": 0, "tax_rate": 15, "taxes": COMPOUND, "line_total": price}


async def _create(client, auth, currency: str, lines: list[dict], **header) -> str:
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": currency, "line_items": lines, **header})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice_from(client, auth, lid: str) -> dict:
    r = await client.post(f"/lists/{lid}/finalize", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{lid}/convert", headers=auth["headers"], json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    r = await client.get(f"/docs/{r.json()['target_doc_id']}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("currency", list(EDITOR_ORACLE))
async def test_the_editor_line_is_charged_its_taxes_not_their_summed_rate(client, session, currency):
    """A line holding a taxes list is charged by it, compound included, whatever ``tax_rate`` it
    holds beside it; the invoice converted from the list charges the same."""
    auth = await _demo_company(client, session, currency)
    price, tax = EDITOR_ORACLE[currency]
    lid = await _create(client, auth, currency, [_editor_line(price)])
    lst = await _list(client, auth, lid)
    assert (lst["tax_amount"], lst["total"]) == (pytest.approx(tax), pytest.approx(price + tax))
    assert [t["amount"] for t in lst["line_items"][0]["taxes"]] == pytest.approx([10, tax - 10])
    doc = await _invoice_from(client, auth, lid)
    assert (doc["tax"], doc["total"]) == (lst["tax_amount"], lst["total"])


async def test_an_edit_from_the_editor_recharges_the_taxes_on_the_new_amount(client, session):
    """Quantity 1 to 2 on the editor's line: 20.00 and 11.00, total 231.00, not a flat 15%."""
    auth = await _demo_company(client, session, "USD")
    lid = await _create(client, auth, "USD", [_editor_line(100)])
    lst = await _list(client, auth, lid)
    lines = [{**_editor_line(100), "line_id": lst["line_items"][0]["line_id"], "quantity": 2, "line_total": 200}]
    r = await client.patch(f"/lists/{lid}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"old": lst["line_items"], "new": lines}},
                                 "expected_version": lst["version"]})
    assert r.status_code == 200, r.text
    lst = await _list(client, auth, lid)
    assert (lst["tax_amount"], lst["total"]) == (pytest.approx(31), pytest.approx(231))
    assert [t["amount"] for t in lst["line_items"][0]["taxes"]] == pytest.approx([20, 11])


async def test_rate_only_lines_are_rounded_per_line_as_on_the_invoice(client, session):
    """Lines with only a ``tax_rate`` are charged as a document charges them, each line's tax
    rounded on its own (0.0035 twice is 0.00, not 0.01), so the invoice converted from the list
    totals the same."""
    auth = await _demo_company(client, session, "USD")
    line = {"description": "Service", "quantity": 1, "unit_price": 0.05, "tax_rate": 7}
    lid = await _create(client, auth, "USD", [line, dict(line)])
    lst = await _list(client, auth, lid)
    assert (lst["tax_amount"], lst["total"]) == (pytest.approx(0.0), pytest.approx(0.10))
    doc = await _invoice_from(client, auth, lid)
    assert (doc["tax"], doc["total"]) == (lst["tax_amount"], lst["total"])


@pytest.mark.parametrize("taxes, tax", [([_tax("Exempt 0%", 0)], 0), ([], 7)])
async def test_a_header_rate_applies_only_where_no_line_has_a_tax(client, session, taxes, tax):
    """A line taxed at 0% has its tax: the list's header rate does not apply over it, as on the
    invoice converted from it. A line with no tax at all takes the header rate."""
    auth = await _demo_company(client, session, "USD")
    line = {"description": "Service", "quantity": 1, "unit_price": 100, "taxes": taxes}
    lid = await _create(client, auth, "USD", [line], tax=7)
    lst = await _list(client, auth, lid)
    assert (lst["tax_amount"], lst["total"]) == (pytest.approx(tax), pytest.approx(100 + tax))
    doc = await _invoice_from(client, auth, lid)
    assert (doc["tax"], doc["total"]) == (lst["tax_amount"], lst["total"])
