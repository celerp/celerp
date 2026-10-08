# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line's taxes follow its amount, and the tax select names the tax it carries.

A line edit recomputes each tax amount on the line from its rate, so the tax rows of
the summary agree with the subtotal and total."""
from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from line_actions_support import h, state  # noqa: F401

_L0 = "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_a_quantity_edit_recomputes_the_line_tax_amounts(client, h):
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"description": "Serum", "quantity": 5, "unit_price": 29, "line_total": 145,
         "taxes": [{"code": "VAT", "rate": 10}]}]})
    assert r.status_code == 200, r.text
    d = r.json()["id"]
    lines = [dict(li) for li in (await state(client, h, d))["line_items"]]
    assert lines[0]["taxes"][0]["amount"] == 14.5
    # The editor sends the stored taxes back as they are, old amount included.
    lines[0].update(quantity=6, line_total=174)
    r = await client.patch(f"/docs/{d}", headers=h, json={"fields_changed": {
        "line_items": {"new": lines}, "subtotal": {"new": 174}, "tax": {"new": 17.4}, "total": {"new": 191.4}}})
    assert r.status_code == 200, r.text
    assert (await state(client, h, d))["line_items"][0]["taxes"][0]["amount"] == 17.4


def _render(status: str, li: dict, company_taxes: list[dict] | None = None) -> str:
    from ui.routes.documents import _doc_detail
    d = {"entity_id": "doc:inv-1", "doc_type": "invoice", "status": status, "ref_id": "I-1",
         "line_items": [{"line_id": _L0, "sku": "S-1", "quantity": 6, "unit_price": 29,
                         "line_total": 174, **li}]}
    return to_xml(_doc_detail(d, company_taxes=company_taxes or []))


def _selected_tax(html: str) -> str:
    # The last select is the line's; the first belongs to the blank row template.
    select = re.findall(r'<select[^>]*data-name="tax_select".*?</select>', html, re.S)[-1]
    return re.search(r"<option[^>]*selected[^>]*>([^<]*)</option>", select).group(1)


def test_the_tax_select_names_a_configured_tax_the_line_carries():
    html = _render("draft", {"taxes": [{"code": "VAT 10%", "rate": 10, "amount": 17.4}]},
                   [{"name": "VAT 10%", "rate": 10}])
    assert _selected_tax(html) == "VAT 10% (10.0%)"


def test_the_tax_select_names_a_line_tax_the_company_has_not_configured():
    html = _render("draft", {"taxes": [{"code": "VAT", "rate": 10, "amount": 17.4}]},
                   [{"name": "Standard Tax", "rate": 0}])
    assert _selected_tax(html) == "VAT (10.0%)"
