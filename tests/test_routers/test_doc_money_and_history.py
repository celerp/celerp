# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Money precision in the doc ledger + semantic history for header-discount edits.

Covers two reported defects:
  1. Tax/totals were recorded with raw IEEE-754 floats (e.g. ``346.50000000000006``) because the
     browser computes them as JS floats and the patch endpoint stored them verbatim.
  2. A header-discount edit logged "Subtotal/Tax/Total ... Lines edited" noise instead of a clean
     "Discount Applied: $X, Total: a -> b" (the lines are re-sent unchanged on a discount save).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp_docs.doc_projections import _recalc_list_totals
from ui.components.activity import _fields_changed_summary


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@money.test"
    r = await client.post("/auth/register", json={"company_name": "Money Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Issue 1: money precision ──────────────────────────────────────────────────

class TestRecalcListTotalsRounds:
    """_recalc_list_totals must round every monetary result to currency precision."""

    def test_usd_tax_has_no_float_tail(self):
        state = _recalc_list_totals({"currency": "USD", "line_items": [{"line_total": 4950, "tax_rate": 7}]})
        assert state["tax_amount"] == 346.5
        assert state["total"] == 5296.5
        # No IEEE-754 residue once stored.
        assert "0000000" not in repr(state["tax_amount"])
        assert "0000000" not in repr(state["total"])

    def test_zero_decimal_currency_rounds_to_whole_units(self):
        state = _recalc_list_totals({"currency": "JPY", "line_items": [{"line_total": 4950, "tax_rate": 7}]})
        assert state["tax_amount"] == 347.0  # 346.5 -> 347 at 0 dp (HALF_UP)
        assert state["total"] == 5297.0

    def test_percentage_discount_reduces_taxable_base(self):
        # Header tax rate (no per-line tax_rate) applies to the post-discount base.
        state = _recalc_list_totals({
            "currency": "USD",
            "line_items": [{"line_total": 1000}],
            "tax": 10,  # header rate 10%
            "discount": 10, "discount_type": "percentage",
        })
        assert state["discount_amount"] == 100.0       # 10% of 1000
        assert state["tax_amount"] == 90.0             # 10% of (1000 - 100)
        assert state["total"] == 990.0


@pytest.mark.asyncio
async def test_patch_doc_rounds_money_in_ledger_and_projection(client, session):
    """A discount save sends raw JS floats; the patch endpoint must round them so neither the
    immutable event record nor the projection carries a float tail."""
    token = await _register(client)
    eid = (await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 5000, "line_total": 5000}],
        "subtotal": 5000, "tax": 350, "total": 5350,
        "currency": "USD",
    })).json()["id"]

    # Simulate the browser: unrounded tax/total floats and a $500 header discount.
    fields_changed = {
        "discount": {"old": 0, "new": 500},
        "discount_type": {"old": "flat", "new": "flat"},
        "discount_amount": {"old": 0.0, "new": 500.0},
        "subtotal": {"old": 5000.0, "new": 5000.0},
        "tax": {"old": 350.0, "new": 315.00000000000006},
        "total": {"old": 5350.0, "new": 4815.00000000000006},
    }
    r = await client.patch(f"/docs/{eid}", headers=_h(token), json={"fields_changed": fields_changed})
    assert r.status_code == 200

    # Projection: stored values are clean.
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["tax"] == 315.0, doc
    assert doc["total"] == 4815.0, doc
    assert "0000000" not in repr(doc["tax"]) and "0000000" not in repr(doc["total"])

    # Event record: the immutable ledger entry is clean too (auditors never see the tail).
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id == eid, LedgerEntry.event_type == "doc.updated")
    )).scalars().all()
    assert rows, "expected a doc.updated ledger entry"
    fc = rows[-1].data["fields_changed"]
    assert fc["tax"]["new"] == 315.0
    assert fc["total"]["new"] == 4815.0
    assert "0000000" not in repr(fc["tax"]["new"])


# ── Issue 2: semantic history for header-discount edits ───────────────────────

class TestDiscountHistorySummary:
    """A header-discount save renders semantically, not as a dump of recomputed totals."""

    def test_discount_applied_shows_amount_and_total_effect(self):
        # The discount editor re-sends the (unchanged) lines plus every recomputed total.
        summary = _fields_changed_summary({
            "discount": {"old": 0, "new": 500},
            "discount_type": {"old": "flat", "new": "flat"},
            "discount_amount": {"old": 0.0, "new": 500.0},
            "subtotal": {"old": 5500.0, "new": 5500.0},
            "tax": {"old": 385.0, "new": 350.0},
            "total": {"old": 5885.0, "new": 5350.0},
            "line_items": {
                "old": [{"sku": "A", "quantity": 1, "unit_price": 5500, "line_total": 5500}],
                "new": [{"sku": "A", "quantity": 1, "unit_price": 5500, "line_total": 5500}],
            },
        }, "USD")
        assert summary == "Discount Applied: $500.00, Total: $5,885.00 → $5,350.00"
        # None of the noise the user complained about.
        assert "Subtotal" not in summary
        assert "Tax" not in summary
        assert "Lines edited" not in summary

    def test_changing_an_existing_discount_shows_old_to_new(self):
        # Updating 275 -> 550 must show the prior amount too, matching every other field.
        summary = _fields_changed_summary({
            "discount": {"old": 275, "new": 550},
            "discount_type": {"old": "flat", "new": "flat"},
            "discount_amount": {"old": 275.0, "new": 550.0},
            "tax": {"old": 367.06, "new": 346.5},
            "total": {"old": 5590.75, "new": 5296.50},
        }, "USD")
        assert summary == "Discount Applied: $275.00 → $550.00, Total: $5,590.75 → $5,296.50"

    def test_first_discount_application_omits_zero_from_value(self):
        # A brand-new discount (no prior amount) reads as a single figure, not "$0.00 -> $X".
        summary = _fields_changed_summary({
            "discount": {"old": 0, "new": 275},
            "discount_amount": {"old": 0.0, "new": 275.0},
            "total": {"old": 5885.0, "new": 5590.75},
        }, "USD")
        assert summary == "Discount Applied: $275.00, Total: $5,885.00 → $5,590.75"

    def test_discount_removed(self):
        summary = _fields_changed_summary({
            "discount": {"old": 500, "new": 0},
            "discount_amount": {"old": 500.0, "new": 0.0},
            "total": {"old": 5350.0, "new": 5885.0},
        }, "USD")
        assert summary == "Discount Removed, Total: $5,350.00 → $5,885.00"

    def test_unchanged_lines_resend_does_not_say_lines_edited(self):
        # A save that re-sends identical line_items must report nothing for them.
        summary = _fields_changed_summary({
            "subtotal": {"old": 5500.0, "new": 5500.0},
            "tax": {"old": 385.0, "new": 385.0},
            "total": {"old": 5885.0, "new": 5885.0},
            "line_items": {
                "old": [{"sku": "A", "quantity": 1, "line_total": 5500}],
                "new": [{"sku": "A", "quantity": 1, "line_total": 5500}],
            },
        }, "USD")
        assert summary == ""

    def test_line_edit_with_existing_percentage_discount_is_not_a_discount_event(self):
        # Editing a line shifts a percentage discount's *amount* but is not a discount action;
        # it must not be mislabelled "Discount Applied".
        summary = _fields_changed_summary({
            "discount": {"old": 10, "new": 10},
            "discount_type": {"old": "percentage", "new": "percentage"},
            "discount_amount": {"old": 495.0, "new": 550.0},
            "subtotal": {"old": 4950.0, "new": 5500.0},
            "tax": {"old": 346.5, "new": 385.0},
            "total": {"old": 5296.5, "new": 5885.0},
            "line_items": {
                "old": [{"sku": "A", "quantity": 1, "unit_price": 4950, "line_total": 4950}],
                "new": [{"sku": "A", "quantity": 2, "unit_price": 2750, "line_total": 5500}],
            },
        }, "USD")
        assert "Discount Applied" not in summary
        assert "A ×1→2" in summary  # the genuine line change is what is reported

    def test_tax_float_tail_is_formatted_clean(self):
        # Even when tax is shown (non-discount context), the IEEE-754 tail never reaches the user.
        summary = _fields_changed_summary({"tax": {"old": 346.50000000000006, "new": 385.00000000000006}}, "USD")
        assert summary == "Tax: $346.50 → $385.00"
