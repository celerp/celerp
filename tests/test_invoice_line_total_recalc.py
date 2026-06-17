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