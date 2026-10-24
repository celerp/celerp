# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A list charges the taxes on its lines.

A line on a list holds its tax as a legacy ``tax_rate`` or as a ``taxes`` list (what a new line
takes from the company's default taxes). The list's tax is the sum of its lines' taxes: a
``tax_rate`` as it always was, and ``taxes`` by the one tax rule documents use (in order,
compound on the base plus the taxes before it), at the list currency's precision. Example: three
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
    """The editor saves a line's rate beside its taxes; the line is taxed once, by its rate."""
    auth = await _demo_company(client, session, "USD")
    line = {**_line("vat", 12.34), "tax_rate": 7}
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "line_items": [line]})
    lst = await _list(client, auth, r.json()["id"])
    assert (lst["tax_amount"], lst["total"]) == (pytest.approx(2.59), pytest.approx(39.61))


async def test_line_taxes_count_after_an_edit_and_beside_a_header_discount(client, session):
    """A line given taxes by an edit is charged them, and a header discount comes off the total
    as it does for a line's rate (each line is taxed on its own amount)."""
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
    assert lst["tax_amount"] == pytest.approx(2.59)
    assert lst["total"] == pytest.approx(37.02 - 10 + 2.59)
    assert lst["line_items"][0]["taxes"][0]["amount"] == pytest.approx(2.59)
