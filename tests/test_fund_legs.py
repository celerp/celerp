# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A document's lines carry their fund into the entry it posts (attribution rule LG).

Initial conditions per test: a fresh company with the seeded chart, funds turned on, a
restricted fund and two board-designated (unrestricted) funds (test_fund_movements._setup),
service items made through POST /items. Documents are created with POST /docs and finalized
with POST /docs/{id}/finalize; the posted entry is read from its projection.
"""
from __future__ import annotations

import pytest
from celerp.models.projections import Projection
from test_fund_movements import _setup
from test_income_accounts import _finalize_entry, _issued, _item, _line

pytestmark = pytest.mark.asyncio


async def _bill_entry(session, auth, doc: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc}:bill"})
    assert row is not None and row.state.get("status") == "posted"
    return row.state


def _by_fund(entries: list[dict], roles: str) -> dict:
    """Net debit per fund of the entry's lines serving ``roles``."""
    out: dict = {}
    for e in entries:
        if roles in (e.get("account_roles") or []):
            out[e.get("fund_id")] = round(out.get(e.get("fund_id"), 0) + (e.get("debit") or 0) - (e.get("credit") or 0), 2)
    return out


def _vat(rate: float, amount: float) -> list[dict]:
    return [{"code": "VAT", "rate": rate, "amount": amount}]


async def test_receivable_split_by_gross_line_contribution(client, session, auth):
    """The receivable of each fund is that fund's lines' net plus their own tax; revenue goes to
    each line's fund; every fund balances on its own."""
    f = await _setup(client, auth)
    gift, ticket = await _item(client, auth, "Gift"), await _item(client, auth, "Ticket")
    doc = await _issued(client, auth, "invoice", [
        _line(gift, "Gift", 100, fund_id=f["R"]), _line(ticket, "Ticket", 50, taxes=_vat(10, 5))],
        subtotal=150, tax=5, total=155)
    entries = (await _finalize_entry(session, auth, doc))["entries"]
    assert _by_fund(entries, "receivable") == {f["R"]: 100.0, None: 55.0}
    assert _by_fund(entries, "sales_revenue") == {f["R"]: -100.0, None: -50.0}
    for fund in (f["R"], None):
        assert round(sum((e.get("debit") or 0) - (e.get("credit") or 0) for e in entries if e.get("fund_id") == fund), 2) == 0
    assert sorted(n for e in entries if e.get("fund_id") == f["R"] for n in e.get("source_lines") or []) == [1, 1]


async def test_mixed_invoice_tax_follows_taxable_line_fund(client, session, auth):
    """An exempt restricted donation and a taxable ticket: all the tax is the ticket's fund's."""
    f = await _setup(client, auth)
    gift, ticket = await _item(client, auth, "Donation"), await _item(client, auth, "Gala ticket")
    doc = await _issued(client, auth, "invoice", [
        _line(gift, "Donation", 200, fund_id=f["R"]), _line(ticket, "Gala ticket", 80, fund_id=f["U1"], taxes=_vat(10, 8))],
        subtotal=280, tax=8, total=288)
    entries = (await _finalize_entry(session, auth, doc))["entries"]
    assert _by_fund(entries, "tax_output") == {f["U1"]: -8.0}
    assert _by_fund(entries, "receivable") == {f["R"]: 200.0, f["U1"]: 88.0}


async def test_expense_bill_fund_from_lines_unfunded_unrestricted(client, session, auth):
    """A bill's expense lines keep their fund and the payable splits with them; a line with no
    fund is the unrestricted group's. A bill split between a restricted fund and the
    unrestricted group posts (it moves nothing between them)."""
    f = await _setup(client, auth)
    doc = await _issued(client, auth, "bill", [
        {**_line(None, "Program supplies", 30), "receive_as": "expense", "fund_id": f["R"]},
        {**_line(None, "Office rent", 70), "receive_as": "expense"}])
    entries = (await _bill_entry(session, auth, doc))["entries"]
    assert _by_fund(entries, "payable") == {f["R"]: -30.0, None: -70.0}
    assert _by_fund(entries, "general_expense") == {f["R"]: 30.0, None: 70.0}

