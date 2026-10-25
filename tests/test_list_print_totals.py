# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A printed quotation shows the totals the list stores (LIST_TOTAL_FIELDS), never a recomputed
or differently named figure, so Subtotal - Discount + Tax = Total reads off the paper."""

from __future__ import annotations

import re
from html import unescape

import pytest
from celerp_docs.doc_money import LIST_TOTAL_FIELDS

from celerp.output.doc_print import render_doc_print_html

# The printed label of each stored list total.
_PRINT_LABEL = {"subtotal": "Subtotal", "discount_amount": "Discount", "tax_amount": "Tax", "total": "Total"}


def _printed(html: str, label: str) -> str | None:
    """The amount printed beside a totals-row label, or None when the row is absent."""
    m = re.search(rf'<td class="label">{label}[^<]*</td>\s*<td class="amount">([^<]*)</td>', html)
    return unescape(m.group(1)) if m else None


def _num(s: str) -> float:
    return float(re.sub(r"[^0-9.\-]", "", s))


async def _printed_quotation(client, session) -> tuple[dict, str]:
    from test_tax_acceptance_matrix import _demo_company

    auth, contacts = await _demo_company(client, session, "USD")
    customer = next(c for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "contact_id": customer["id"],
        "discount": 10, "discount_type": "percentage", "tax": 7,
        "line_items": [{"description": "Service", "quantity": 2, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    r = await client.get(f"/lists/{r.json()['id']}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    state = {"doc_type": "list", **r.json()}
    return state, render_doc_print_html(state)


@pytest.mark.asyncio
async def test_a_printed_quotation_shows_its_stored_totals_and_they_add_up(client, session):
    state, html = await _printed_quotation(client, session)
    shown = {k: _printed(html, _PRINT_LABEL[k]) for k in LIST_TOTAL_FIELDS}
    assert all(shown.values()), f"missing print rows: {shown}"
    sub, disc, tax, total = (_num(shown[k]) for k in LIST_TOTAL_FIELDS)
    assert (sub, abs(disc), tax, total) == tuple(float(state[k]) for k in LIST_TOTAL_FIELDS)
    assert round(sub - abs(disc) + tax, 2) == total


@pytest.mark.asyncio
async def test_every_stored_list_total_has_a_print_row(client, session):
    state, html = await _printed_quotation(client, session)
    for field in LIST_TOTAL_FIELDS:
        assert field in _PRINT_LABEL, f"no print row for list total {field}"
        assert float(state[field]) > 0
        assert _printed(html, _PRINT_LABEL[field]) is not None, field
