# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One set of document figures: header discount before tax, shipping in the total.

The document calculation (celerp_docs.doc_money.document_money) computes tax on the
discounted taxable base and adds shipping to the total. Every write stores its result,
and every surface (detail page, print, PDF) lays out those stored figures without
recomputing them. Example: $100 subtotal, 10% header discount, 10% tax gives tax $9.00
and total $99.00; with $5.00 shipping the total is $104.00.
"""
from __future__ import annotations

import copy
import os
import re

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from celerp.models.projections import Projection
from test_helpers import make_test_token, doc_version
from test_money_stock_and_contact_invariants import _auth_company
from test_ui import _mock_get_company  # noqa: F401  (autouse fixture: the company settings)


def _doc(shipping: float = 0.0) -> dict:
    """A draft invoice whose money is what the document calculation produces."""
    from celerp_docs.doc_money import document_money
    lines = [{"description": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0,
              "tax_rate": 10, "taxes": [{"code": "VAT", "rate": 10, "amount": 0, "order": 0,
                                         "is_compound": False, "label": ""}]}]
    state = {"entity_id": "d:disc", "doc_type": "invoice", "status": "draft", "currency": "USD",
             "contact_name": "Acme", "issue_date": "2026-01-01",
             "discount": 10, "discount_type": "percentage", "shipping": shipping}
    money_lines = copy.deepcopy(lines)
    state.update(document_money(state, money_lines, "USD", keep_unrated_tax=True))
    state["line_items"] = money_lines
    return state


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


async def _render(ui_client, doc: dict) -> str:
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
        ui_client.cookies.update({"celerp_token": make_test_token()})
        r = await ui_client.get(f"/docs/{doc['entity_id']}")
    assert r.status_code == 200, r.text
    return r.text


def _tax_rows(html: str) -> list[str]:
    block = html[html.index('id="doc-tax-rows"'):html.index('id="doc-total"')]
    return re.findall(r'class="total-value">([^<]+)<', block)


def _total(html: str) -> str:
    return re.search(r'id="doc-total"[^>]*>([^<]+)<', html).group(1)


@pytest.mark.asyncio
async def test_detail_page_shows_discounted_tax_once(ui_client):
    doc = _doc()
    assert doc["tax"] == 9.0 and doc["total"] == 99.0
    html = await _render(ui_client, doc)
    assert _tax_rows(html)[0] == "$9.00"
    assert _total(html) == "$99.00"
    assert "$8.10" not in html and "$98.10" not in html


@pytest.mark.asyncio
async def test_detail_page_total_includes_shipping_with_a_discount(ui_client):
    doc = _doc(shipping=5.0)
    assert doc["total"] == 104.0
    html = await _render(ui_client, doc)
    assert _tax_rows(html)[0] == "$9.00"
    assert re.search(r'id="doc-shipping"[^>]*>\$5\.00<', html), "shipping row missing"
    assert _total(html) == "$104.00"


def test_print_shows_the_stored_figures_with_shipping():
    from celerp.output.doc_print import render_doc_print_html
    html = render_doc_print_html(_doc(shipping=5.0))
    totals = html[html.index("dp-totals"):]
    assert "-$10.00" in totals and "$9.00" in totals and "Shipping" in totals and "$104.00" in totals


def _print_totals(doc: dict) -> dict[str, str]:
    """The print view's totals rows, label to amount."""
    from celerp.output.doc_print import render_doc_print_html
    html = render_doc_print_html(doc)
    block = html[html.index("dp-totals"):]
    block = block[:block.index("</table>")]
    return dict(re.findall(r'<td class="label">([^<]+)</td>\s*<td class="amount">([^<]+)</td>', block))


def test_print_reads_the_total_and_tax_the_detail_page_and_pdf_read():
    base = {"doc_type": "invoice", "currency": "USD", "subtotal": 100.0, "line_items": []}
    # The current keys win over an older one.
    rows = _print_totals(base | {"total": 107.0, "total_amount": 500.0, "tax_amount": 7.0, "tax": 3.0})
    assert rows["Total"] == "$107.00" and rows["Tax"] == "$7.00", rows
    # An imported document carries its total under the older key only.
    rows = _print_totals(base | {"total_amount": 110.0, "tax": 10.0})
    assert rows["Total"] == "$110.00" and rows["Tax"] == "$10.00", rows


def test_a_document_s_tax_is_its_stored_amount_a_zero_included_never_a_rate():
    from celerp.services.doc_balance import doc_tax
    # A list stores its tax amount, and its header tax as a rate: a list with a 7% rate on
    # lines that bear no tax has no tax, not 7.
    assert doc_tax({"doc_type": "list", "entity_type": "list", "tax": 7.0, "tax_amount": 0.0}) == 0
    assert doc_tax({"doc_type": "list", "entity_type": "list", "tax": 7.0, "tax_amount": 14.0}) == 14.0
    # A list that has no stored amount shows no tax rather than its rate as money.
    assert doc_tax({"doc_type": "list", "tax": 7.0}) == 0
    # Every other document stores its tax amount as tax, a zero included.
    assert doc_tax({"doc_type": "invoice", "tax": 0.0}) == 0
    assert doc_tax({"doc_type": "invoice", "tax": 9.0}) == 9.0
    assert doc_tax({"doc_type": "invoice"}) == 0


def test_print_and_pdf_show_a_list_s_tax_amount_not_its_rate():
    from celerp.output import pdf as pdf_mod
    base = {"doc_type": "list", "entity_type": "list", "currency": "USD", "subtotal": 100.0, "line_items": []}
    # A 7% rate on a list whose amount came to zero: no tax row, a total without tax.
    rows = _print_totals(base | {"tax": 7.0, "tax_amount": 0.0, "total": 100.0})
    assert "Tax" not in rows and rows["Total"] == "$100.00", rows
    # The same rate on taxed lines: the amount, never the 7.
    rows = _print_totals(base | {"tax": 7.0, "tax_amount": 7.0, "total": 107.0})
    assert rows["Tax"] == "$7.00", rows
    rows = _print_totals(base | {"tax": 10.0, "tax_amount": 7.0, "total": 107.0})
    assert rows["Tax"] == "$7.00", rows

    seen: list[str] = []
    real = pdf_mod.Paragraph

    def _spy(text, *a, **k):
        seen.append(str(text))
        return real(text, *a, **k)

    with patch.object(pdf_mod, "Paragraph", side_effect=_spy):
        pdf_mod.generate_document_pdf(base | {"tax": 7.0, "tax_amount": 0.0, "total": 100.0}, {"name": "Co"})
    assert pdf_mod._fmt_money(7, "USD") not in seen, seen


def test_print_shows_an_imported_document_s_tax_and_a_zero_tax():
    base = {"doc_type": "invoice", "currency": "USD", "subtotal": 100.0, "line_items": []}
    # An imported invoice stores its tax amount as tax, its total under the older key.
    rows = _print_totals(base | {"total_amount": 107.0, "tax": 7.0})
    assert rows["Tax"] == "$7.00" and rows["Total"] == "$107.00", rows
    # An invoice with no tax.
    rows = _print_totals(base | {"total": 100.0, "tax": 0.0})
    assert "Tax" not in rows and rows["Total"] == "$100.00", rows


@pytest.mark.asyncio
async def test_detail_page_shows_a_list_s_tax_amount_not_its_rate(ui_client):
    doc = {"entity_id": "l:rate", "entity_type": "list", "doc_type": "list", "list_type": "quotation",
           "status": "draft", "currency": "USD", "contact_name": "Acme", "issue_date": "2026-01-01",
           "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
           "subtotal": 100.0, "discount": 0, "discount_type": "flat", "discount_amount": 0.0,
           "tax": 7.0, "tax_amount": 0.0, "total": 100.0}
    html = await _render(ui_client, doc)
    assert _total(html) == "$100.00"
    assert "$7.00" not in _tax_rows(html), _tax_rows(html)


def test_pdf_shows_the_discount_and_shipping_rows():
    from celerp.output import pdf as pdf_mod
    rows: list[str] = []
    real = pdf_mod.Paragraph

    def _spy(text, *a, **k):
        rows.append(str(text))
        return real(text, *a, **k)

    with patch.object(pdf_mod, "Paragraph", side_effect=_spy):
        pdf_mod.generate_document_pdf(_doc(shipping=5.0), {"name": "Co"})
    money = pdf_mod._fmt_money
    assert "Discount" in rows and "-" + money(10, "USD") in rows
    assert "Shipping" in rows and money(5, "USD") in rows and money(104, "USD") in rows


@pytest.mark.asyncio
async def test_line_save_stores_computed_money_not_client_figures(client, session):
    """The editor's line save is an input; the API computes the money (and the GL follows it)."""
    auth = await _auth_company(session)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "shipping": 5.0,
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "tax_rate": 10}],
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    # What the editor sends: the lines, the header discount, and stale client totals.
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={"expected_version": await doc_version(client, auth["headers"], doc_id), "fields_changed": {
        "line_items": {"new": [{"description": "Service", "quantity": 1, "unit_price": 100.0,
                                "line_total": 100.0, "tax_rate": 10,
                                "taxes": [{"code": "", "rate": 10, "amount": 0, "order": 0,
                                           "is_compound": False, "label": ""}]}]},
        "discount": {"new": 10}, "discount_type": {"new": "percentage"},
        "subtotal": {"new": 100.0}, "tax": {"new": 8.1}, "total": {"new": 98.1},
    }})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc["discount_amount"] == 10.0
    assert doc["line_items"][0]["taxes"][0]["amount"] == 9.0
    assert doc["tax"] == 9.0
    assert doc["total"] == 104.0

    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.like(f"je:auto:%{doc_id}%")))).scalars().all()
    debit: dict[str, float] = {}
    credit: dict[str, float] = {}
    for row in rows:
        for e in row.state.get("entries") or []:
            debit[e["account"]] = round(debit.get(e["account"], 0) + float(e["debit"] or 0), 2)
            credit[e["account"]] = round(credit.get(e["account"], 0) + float(e["credit"] or 0), 2)
    assert debit.get("1120") == 104.0, (debit, credit)
    assert credit.get("2120") == 9.0, (debit, credit)


# A document's tax amount computed from rates is derived: removing the last rate on a draft
# leaves no tax, never the amount the old rates gave. Only a tax amount the document never
# had a rate for (an imported total) stands on its own.
_LINE = {"description": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}
_LEVY = {"code": "Levy 5%", "rate": 5.0, "order": 0, "is_compound": False, "label": ""}
_RATE_SOURCES = {
    "doc_taxes": ({"doc_taxes": [_LEVY]}, {"doc_taxes": []}),
    "header_tax_rate": ({"tax_rate": 5}, {"tax_rate": 0}),
    "line_taxes": ({"line_items": [{**_LINE, "taxes": [_LEVY]}]}, {"line_items": [dict(_LINE)]}),
    "line_tax_rate": ({"line_items": [{**_LINE, "tax_rate": 5}]}, {"line_items": [dict(_LINE)]}),
}


@pytest.mark.parametrize("source", sorted(_RATE_SOURCES))
def test_removing_the_last_rate_leaves_no_tax(source):
    from celerp_docs.doc_money import document_money
    rated, cleared = _RATE_SOURCES[source]
    stored = {"currency": "USD", "line_items": [dict(_LINE)], **copy.deepcopy(rated)}
    stored.update(document_money(stored, stored["line_items"], "USD", keep_unrated_tax=True))
    assert stored["tax"] == 5.0
    merged = {**stored, **copy.deepcopy(cleared)}
    money = document_money(merged, merged["line_items"], "USD", keep_unrated_tax=True, stored=stored)
    assert (money["tax"], money["total"]) == (0.0, 100.0)


def test_an_unrated_tax_amount_stands_through_an_edit():
    from celerp_docs.doc_money import document_money
    stored = {"currency": "USD", "line_items": [dict(_LINE)], "tax": 7.0}
    merged = {**stored, "line_items": [{**_LINE, "quantity": 2, "line_total": 200.0}]}
    money = document_money(merged, merged["line_items"], "USD", keep_unrated_tax=True, stored=stored)
    assert (money["tax"], money["total"]) == (7.0, 207.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(_RATE_SOURCES))
async def test_a_draft_edit_removing_the_last_rate_stores_no_tax(client, session, source):
    auth = await _auth_company(session)
    rated, cleared = _RATE_SOURCES[source]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [dict(_LINE)], **copy.deepcopy(rated)})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()["tax"] == 5.0
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={"fields_changed": {
        k: {"new": v} for k, v in copy.deepcopy(cleared).items()},
        "expected_version": await doc_version(client, auth["headers"], doc_id)})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert (doc["tax"], doc["total"]) == (0.0, 100.0), doc
