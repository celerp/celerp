# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for invoice line-total → unit_price back-calculation.

When a user edits the "Total" column on an invoice line item:
  - qty must stay unchanged
  - unit_price must be back-calculated as line_total / (qty * (1 - discPct/100))
  - celerpUpdateTotals() must run even when factor == 0 (qty=0 or 100% discount)
  - _celerpCollectLines must read the actual .line-total DOM value, not recompute it

Covers:
  1. HTML rendering — correct oninput handler on the line_total input
  2. Rendered JS — celerpLineTotalInput writes unit_price, not quantity
  3. Rendered JS — celerpLineTotalInput calls celerpUpdateTotals() on factor=0 path
  4. Rendered JS — _celerpCollectLines reads from .line-total field (not qty*price)
  5. API integration — POST /docs/{id}/lines preserves explicit line_total as-sent
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token


def _authed() -> dict:
    return {"celerp_token": make_test_token()}


def _extract_function(html: str, name: str) -> str:
    """Return a JS function's full body by brace-matching (robust to length/formatting changes)."""
    start = html.find(f"function {name}")
    assert start >= 0, f"{name} not found in rendered JS"
    brace = html.find("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    return html[start:]


_DRAFT_DOC = {
    "entity_id": "d:lt1",
    "doc_number": "INV-LT-001",
    "doc_type": "invoice",
    "status": "draft",
    "contact_name": "Acme",
    "issue_date": "2026-01-01",
    "due_date": "2026-02-01",
    "total_amount": 100,
    "outstanding_balance": 100,
    "line_items": [
        {
            "description": "Widget",
            "quantity": 5,
            "unit_price": 10.0,
            "discount_pct": 0,
            "line_total": 50.0,
        }
    ],
    "tax_amount": 0,
    "payment_terms": "Net 30",
}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


@pytest_asyncio.fixture
async def draft_html(ui_client):
    """Rendered HTML of the draft invoice detail page."""
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_DRAFT_DOC)):
        r = await ui_client.get("/docs/d:lt1", cookies=_authed())
    assert r.status_code == 200
    return r.text


# ── 1. HTML rendering ─────────────────────────────────────────────────────────

class TestLineTotalInputWiring:

    @pytest.mark.asyncio
    async def test_line_total_input_uses_celerpLineTotalInput_handler(self, draft_html):
        """The line_total input must use celerpLineTotalInput, not celerpUpdateTotals."""
        assert 'oninput="celerpLineTotalInput(this)"' in draft_html, (
            "line_total input must fire celerpLineTotalInput on input, not celerpUpdateTotals"
        )

    @pytest.mark.asyncio
    async def test_quantity_input_uses_celerpUpdateTotals_handler(self, draft_html):
        """The quantity input must use celerpUpdateTotals (forward calc)."""
        assert 'data-name="quantity"' in draft_html
        # quantity fires forward calculation, not backward
        assert 'celerpUpdateTotals()' in draft_html

    @pytest.mark.asyncio
    async def test_line_total_input_has_data_name(self, draft_html):
        """line_total input must have data-name='line_total' for DOM selectors."""
        assert 'data-name="line_total"' in draft_html


# ── 2. JS: celerpLineTotalInput writes unit_price, not quantity ───────────────

class TestCelerpLineTotalInputFunction:

    @pytest.mark.asyncio
    async def test_function_is_present_in_rendered_js(self, draft_html):
        assert "celerpLineTotalInput" in draft_html

    @pytest.mark.asyncio
    async def test_function_updates_unit_price_not_quantity(self, draft_html):
        """celerpLineTotalInput must set unit_price.value, never quantity.value."""
        func_body = _extract_function(draft_html, "celerpLineTotalInput")
        assert "unitPriceEl.value" in func_body, (
            "celerpLineTotalInput must update unit_price"
        )
        # Must NOT set quantity — qty is read, never written
        assert "qtyEl.value" not in func_body, (
            "celerpLineTotalInput must not write to quantity field"
        )
        # Reads qty as a const (unchanged), does NOT assign to any qty field
        assert '[data-name="quantity"]' in func_body, (
            "celerpLineTotalInput must read quantity to back-calculate unit_price"
        )

    @pytest.mark.asyncio
    async def test_function_calls_celerpUpdateTotals_on_factor_zero(self, draft_html):
        """When qty=0 or 100% discount (factor==0), celerpUpdateTotals must still run."""
        func_body = _extract_function(draft_html, "celerpLineTotalInput")
        # factor === 0 branch must still call celerpUpdateTotals() before the unit-price write.
        assert "celerpUpdateTotals" in func_body[: func_body.find("if (unitPriceEl)")]

    @pytest.mark.asyncio
    async def test_function_does_not_call_celerpUpdateTotals_in_a_loop(self, draft_html):
        """celerpLineTotalInput calls celerpUpdateTotals() only at terminal points (one per code
        path: cleared field, factor=0, normal) - never inside a loop, so it can't recompute-storm."""
        func_body = _extract_function(draft_html, "celerpLineTotalInput")
        assert "celerpUpdateTotals()" in func_body, "must recompute document totals"
        # The real invariant: no loop construct wraps the recompute call.
        for loop_kw in ("for (", "while (", ".forEach", "for(", "while("):
            assert loop_kw not in func_body, (
                f"celerpLineTotalInput must contain no loop ({loop_kw!r}) around celerpUpdateTotals"
            )


# ── 3. JS: _celerpCollectLines reads DOM line_total value ─────────────────────

class TestCelerpCollectLinesFunction:

    @pytest.mark.asyncio
    async def test_reads_line_total_from_dom_field(self, draft_html):
        """_celerpCollectLines must read from the .line-total DOM element."""
        start = draft_html.find("function _celerpCollectLines")
        assert start >= 0, "_celerpCollectLines not found in rendered JS"
        # Use 2500 chars — the long category/receiveAs lines push the new code past 1200
        func_body = draft_html[start: start + 2500]
        assert "querySelector('.line-total')" in func_body, (
            "_celerpCollectLines must read line_total from .line-total DOM field, not recompute"
        )

    @pytest.mark.asyncio
    async def test_has_fallback_when_field_missing(self, draft_html):
        """_celerpCollectLines must fall back to qty*price when .line-total is absent."""
        start = draft_html.find("function _celerpCollectLines")
        assert start >= 0
        func_body = draft_html[start: start + 2500]
        # Ternary fallback: lineTotalEl ? ... : qty * price * (1 - discPct / 100)
        assert "qty * price * (1 - discPct / 100)" in func_body, (
            "_celerpCollectLines must fall back to qty*price when .line-total element is absent"
        )


# ── 4c. Invoice-level (header) discount ──────────────────────────────────────

class TestHeaderDiscount:
    """The whole-document discount: a pencil by the Total opens a %/$ popover; the discount reduces
    the taxable base and tax scales by the same ratio so rows/discount/total reconcile."""

    @pytest.mark.asyncio
    async def test_pencil_and_popover_on_draft_invoice(self, ui_client):
        doc = {**_DRAFT_DOC, "entity_id": "d:hd", "doc_type": "invoice", "status": "draft"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:hd", cookies=_authed())
        assert r.status_code == 200
        assert 'class="btn-disc-edit"' in r.text, "discount pencil missing on a draft invoice"
        assert 'id="discount-popover"' in r.text, "discount popover missing"
        assert 'id="doc-discount-value"' in r.text, "discount state input missing"
        assert "<dialog" not in r.text.lower(), "discount editor must be an inline popover, not a modal dialog"

    @pytest.mark.asyncio
    async def test_pencil_left_of_right_aligned_total(self, ui_client):
        # The pencil sits LEFT of the total amount (grouped in .total-final-right); the amount keeps
        # its right-aligned accounting position.
        doc = {**_DRAFT_DOC, "entity_id": "d:hdL", "doc_type": "invoice", "status": "draft"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:hdL", cookies=_authed())
        assert r.status_code == 200
        assert "total-final-right" in r.text
        assert r.text.index('class="btn-disc-edit"') < r.text.index('id="doc-total"'), \
            "pencil must render before (left of) the total amount"

    @pytest.mark.asyncio
    async def test_secondary_button_is_cancel_until_discount_exists(self, ui_client):
        no_disc = {**_DRAFT_DOC, "entity_id": "d:hdC", "doc_type": "invoice", "status": "draft"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=no_disc)):
            r = await ui_client.get("/docs/d:hdC", cookies=_authed())
        assert ">Cancel<" in r.text and ">Remove<" not in r.text, "new discount editor shows Cancel, not Remove"

        with_disc = {**_DRAFT_DOC, "entity_id": "d:hdR", "doc_type": "invoice", "status": "draft",
                     "subtotal": 100.0, "total": 90.0, "total_amount": 90.0,
                     "discount": 10, "discount_type": "percentage", "discount_amount": 10.0}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=with_disc)):
            r2 = await ui_client.get("/docs/d:hdR", cookies=_authed())
        assert ">Remove<" in r2.text, "an existing discount shows Remove"

    @pytest.mark.asyncio
    async def test_no_pencil_on_credit_note(self, ui_client):
        # A credit note is a reversal - a header discount is incoherent there, so no affordance.
        doc = {**_DRAFT_DOC, "entity_id": "d:cn", "doc_type": "credit_note", "status": "draft"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:cn", cookies=_authed())
        assert r.status_code == 200
        # The pencil button + popover markup are gated; only the JS handler mentions the class name.
        assert 'class="btn-disc-edit"' not in r.text, "credit note should not show the discount pencil"
        assert 'id="discount-popover"' not in r.text, "credit note should not render the discount popover"

    @pytest.mark.asyncio
    async def test_pencil_on_supplier_bill(self, ui_client):
        doc = {**_DRAFT_DOC, "entity_id": "d:bill", "doc_type": "bill", "status": "draft"}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:bill", cookies=_authed())
        assert r.status_code == 200
        assert 'class="btn-disc-edit"' in r.text, "supplier bill should offer a header discount"
        assert 'id="discount-popover"' in r.text

    @pytest.mark.asyncio
    async def test_percentage_discount_scales_tax_and_total(self, ui_client):
        # subtotal 1000, 5% line tax; a 10% header discount -> taxable 900, tax 45 (was 50), total 945.
        line = {"description": "Widget", "quantity": 10, "unit_price": 100.0,
                "discount_pct": 0, "line_total": 1000.0, "tax_rate": 5}
        doc = {**_DRAFT_DOC, "entity_id": "d:hd2", "doc_type": "invoice", "status": "final",
               "line_items": [line], "subtotal": 1000.0, "tax": 45.0, "total": 945.0,
               "total_amount": 945.0, "discount": 10, "discount_type": "percentage",
               "discount_amount": 100.0}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:hd2", cookies=_authed())
        assert r.status_code == 200
        assert "Discount (10%)" in r.text, "discount row should name the percentage"
        assert "-$100.00" in r.text, "discount amount row"
        assert "$45.00" in r.text, "tax must scale to the discounted base (50 -> 45)"
        assert "$945.00" in r.text, "total must be taxable + scaled tax"

    @pytest.mark.asyncio
    async def test_flat_discount_row_no_percent_label(self, ui_client):
        line = {"description": "Widget", "quantity": 10, "unit_price": 100.0,
                "discount_pct": 0, "line_total": 1000.0, "tax_rate": 0}
        doc = {**_DRAFT_DOC, "entity_id": "d:hd3", "doc_type": "invoice", "status": "final",
               "line_items": [line], "subtotal": 1000.0, "tax": 0.0, "total": 850.0,
               "total_amount": 850.0, "discount": 150, "discount_type": "flat",
               "discount_amount": 150.0}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
            r = await ui_client.get("/docs/d:hd3", cookies=_authed())
        assert r.status_code == 200
        assert "-$150.00" in r.text
        assert "Discount (" not in r.text, "a flat discount must not show a percentage"
        assert "$850.00" in r.text, "total = subtotal - flat discount (no tax)"

    def test_print_view_shows_discount(self):
        """The printable invoice shows the header discount between Subtotal and Total."""
        from fasthtml.common import to_xml
        from ui.routes.documents import _doc_print_view
        doc = {"doc_type": "invoice", "subtotal": 1000.0, "tax_total": 45.0, "total": 945.0,
               "discount": 10, "discount_type": "percentage", "discount_amount": 100.0,
               "line_items": [{"name": "Widget", "quantity": 10, "unit_price": 100.0, "line_total": 1000.0}]}
        html = to_xml(_doc_print_view(doc))
        assert "Discount (10%)" in html
        assert "-$100.00" in html


# ── 4b. Unit-price precision: no rounding-into-discount ──────────────────────

def _doc_with(lines, **over):
    """A draft invoice detail doc carrying the given line items (sums computed from them)."""
    sub = round(sum(float(li["line_total"]) for li in lines), 2)
    d = {**_DRAFT_DOC, "entity_id": "d:hp", "doc_number": "INV-HP-001",
         "line_items": lines, "subtotal": sub, "total": sub, "total_amount": sub}
    d.update(over)
    return d


# qty * unit_price = 38.6 * 15.285 = 590.001 -> line_total 590.00 at currency precision.
# Rounding the unit price to 2 dp (15.29) would make 38.6*15.29 = 590.19 - a gap the doc used
# to surface as a phantom "discount". The unit price must keep its real 3 decimals.
_HIPREC_LINE = {"description": "Gold", "quantity": 38.6, "unit_price": 15.285,
                "discount_pct": 0, "line_total": 590.0}


class TestUnitPricePrecision:

    @pytest.mark.asyncio
    async def test_back_calc_uses_precision_helper_not_toFixed(self, draft_html):
        """celerpLineTotalInput must derive the unit price via _celerpUnitFromTotal (keeps real
        precision), never .toFixed(2) which truncated it to 2 dp and created the discount gap."""
        assert "function _celerpUnitFromTotal" in draft_html
        body = _extract_function(draft_html, "celerpLineTotalInput")
        assert "_celerpUnitFromTotal(" in body, "must use the precision helper to back-calc unit price"
        assert "toFixed(2)" not in body, "must not truncate the back-calculated unit price to 2 dp"

    @pytest.mark.asyncio
    async def test_unit_from_total_searches_currency_to_rate_decimals(self, draft_html):
        """The helper searches from currency precision up to rate precision for a unit price that
        reconciles (round(unit*qty) == target) - mirroring the backend money.unit_price_from_total."""
        body = _extract_function(draft_html, "_celerpUnitFromTotal")
        assert "_CELERP_CDP" in body and "_CELERP_RDP" in body

    @pytest.mark.asyncio
    async def test_draft_hiprec_unit_price_keeps_decimals(self, ui_client):
        """A draft line's editable unit-price input shows the stored 3-decimal price, not 2 dp."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_doc_with([_HIPREC_LINE]))):
            r = await ui_client.get("/docs/d:hp", cookies=_authed())
        assert r.status_code == 200
        assert "15.285" in r.text

    @pytest.mark.asyncio
    async def test_finalized_hiprec_unit_price_full_precision(self, ui_client):
        """The finalized read-only view renders the unit price via fmt_rate (real decimals),
        never rounded to 2 dp."""
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_doc_with([_HIPREC_LINE], status="final"))):
            r = await ui_client.get("/docs/d:hp", cookies=_authed())
        assert r.status_code == 200
        assert "15.285" in r.text, "finalized view must show the real unit-price decimals"
        assert "15.29" not in r.text, "unit price must not be rounded up to 2 dp"
        assert 'id="doc-line-discount"' not in r.text, "no phantom discount on a non-discounted line"

    @pytest.mark.asyncio
    async def test_no_phantom_discount_from_rounding_residue(self, ui_client):
        """Multiple high-precision lines (no discount_pct) must NOT produce a discount row from
        accumulated rounding residue. Old formula (sum(qty*price) - subtotal) showed a phantom
        $0.01: 3 x (1 * 10.333) = 30.999 vs stored subtotal 30.99. Rendered finalized so the
        only `doc-line-discount` would be a real server-side row (the editable JS, which also
        names that id, is not emitted on a finalized doc)."""
        lines = [{"description": f"Item {i}", "quantity": 1, "unit_price": 10.333,
                  "discount_pct": 0, "line_total": 10.33} for i in range(3)]
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_doc_with(lines, status="final"))):
            r = await ui_client.get("/docs/d:hp", cookies=_authed())
        assert r.status_code == 200
        assert 'id="doc-line-discount"' not in r.text, "rounding residue must not appear as a discount"

    @pytest.mark.asyncio
    async def test_real_line_discount_still_shown(self, ui_client):
        """A genuine line-level discount_pct still renders a discount row (10% of 100 = 10).
        Finalized so the id reflects a real server row, not the editable JS template."""
        line = {"description": "Widget", "quantity": 10, "unit_price": 10.0,
                "discount_pct": 10, "line_total": 90.0}
        with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_doc_with([line], status="final"))):
            r = await ui_client.get("/docs/d:hp", cookies=_authed())
        assert r.status_code == 200
        assert 'id="doc-line-discount"' in r.text, "a real line discount must still be shown"

    def test_print_view_unit_price_full_precision(self):
        """The printable view renders the unit price via fmt_rate (real decimals)."""
        from fasthtml.common import to_xml
        from ui.routes.documents import _doc_print_view
        html = to_xml(_doc_print_view(_doc_with([_HIPREC_LINE])))
        assert "15.285" in html
        assert "15.29" not in html


# ── 4. API integration: explicit line_total is preserved ─────────────────────

class TestLinesTotalAPIPreservation:
    """The backend API must preserve line_total, quantity, and unit_price as sent.

    This simulates the round-trip after the JS back-calculation:
      original: qty=5, unit_price=10, line_total=50
      user edits line_total → 100
      JS computes unit_price = 100 / 5 = 20
      saved payload: qty=5, unit_price=20, line_total=100
    """

    @pytest.mark.asyncio
    async def test_back_calculated_line_preserved_on_patch(self, client):
        """PATCH /docs/{id} with back-calculated unit_price preserves qty, unit_price, line_total."""
        addr = f"lt-{uuid.uuid4().hex[:8]}@test.com"
        reg = await client.post(
            "/auth/register",
            json={"company_name": "LT Corp", "email": addr, "name": "Admin", "password": "pw"},
        )
        assert reg.status_code == 200
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Create an initial doc
        initial_line = {"description": "Widget", "quantity": 5, "unit_price": 10.0, "line_total": 50.0}
        doc_r = await client.post(
            "/docs",
            headers=headers,
            json={
                "doc_type": "invoice",
                "contact_id": "contact:test",
                "line_items": [initial_line],
                "subtotal": 50.0,
                "tax": 0.0,
                "total": 50.0,
            },
        )
        assert doc_r.status_code == 200, doc_r.text
        entity_id = doc_r.json()["id"]

        # Simulate: user edits line_total to 100 → JS back-calculates unit_price=20
        updated_line = {"description": "Widget", "quantity": 5, "unit_price": 20.0, "line_total": 100.0}
        patch_r = await client.patch(
            f"/docs/{entity_id}",
            headers=headers,
            json={
                "fields_changed": {
                    "line_items": {"old": None, "new": [updated_line]},
                    "subtotal": {"old": None, "new": 100.0},
                    "total": {"old": None, "new": 100.0},
                }
            },
        )
        assert patch_r.status_code == 200, patch_r.text

        # Retrieve and verify all three values are preserved
        get_r = await client.get(f"/docs/{entity_id}", headers=headers)
        assert get_r.status_code == 200
        saved_lines = get_r.json().get("line_items", [])
        assert len(saved_lines) == 1
        li = saved_lines[0]
        assert float(li["quantity"]) == 5.0, "quantity must not change when unit_price is back-calculated"
        assert float(li["unit_price"]) == 20.0, "unit_price must be the back-calculated value"
        assert float(li["line_total"]) == 100.0, "line_total must be the user's entered value"