# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The opening balance tie-out compares the documents imported as "Already in my opening
balances" with what the ledger holds on the opening balance date, read only.

Initial conditions per test: a fresh company with the seeded chart, its opening balance
date set to 2026-06-30, and documents imported through /docs/import with a treatment.
Three checks: goods billed but not yet received (per goods account), payables, and
receivables (invoices less credit notes). Each reads Opening balances (the ledger),
Imported documents (expected), Difference (ledger - expected) and whether they match.
"""
from __future__ import annotations

import uuid

import pytest

from test_cost_restatement import _item
from test_import_treatment import _events, _post, _rec, _snapshot
from test_imported_document_cutover import AP, IN_TRANSIT, PRICE
from test_receipt_accounting import _OPENING

pytestmark = pytest.mark.asyncio

AS_OF = "2026-06-30"
AR, RE = "1120", "3200"
PATH = "/accounting/opening-tie-out"


async def _opening_date(client, auth, value: str | None = AS_OF) -> None:
    r = await client.patch("/companies/me/books", headers=auth["headers"], json={"opening_balance_date": value})
    assert r.status_code == 200, r.text


async def _je(client, auth, *entries: dict, ts: str = AS_OF) -> None:
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": ts, "memo": "Opening balances", "idempotency_token": uuid.uuid4().hex, "entries": list(entries)})
    assert r.status_code == 200, r.text


async def _import(client, auth, data: dict, treatment: str = "opening_balances") -> str:
    rec = _rec(data, treatment)
    r = await _post(client, auth, rec, False)
    assert r.status_code == 200, r.text
    return rec["entity_id"]


async def _tie_out(client, auth) -> dict:
    r = await client.get(PATH, headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _row(data: dict, key: str) -> dict:
    return next(c for c in data["checks"] if c["key"] == key)


def _sales(doc_type: str, total: float, outstanding: float | None = None, **extra) -> dict:
    owed = total if outstanding is None else outstanding
    return {"doc_type": doc_type, "contact_id": "customer:1", "status": "final" if owed == total else "partial",
            "doc_number": f"S-{uuid.uuid4().hex[:6]}", "issue_date": "2026-05-01", "total": total,
            "amount_outstanding": owed, "amount_paid": total - owed,
            "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}], **extra}


async def test_tie_out_matches_when_opening_journal_holds_the_bill(client, session, auth):
    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _import(client, auth, _snapshot(lot, "bill", 5, 0))
    await _je(client, auth, {"account": IN_TRANSIT, "debit": PRICE * 5}, {"account": AP, "credit": PRICE * 5})
    data = await _tie_out(client, auth)
    assert data["as_of"] == AS_OF
    assert [c["key"] for c in data["checks"]] == ["goods", "payables", "receivables"]
    goods, payables = _row(data, "goods"), _row(data, "payables")
    assert (goods["ledger"], goods["expected"], goods["difference"], goods["matches"]) == (70.0, 70.0, 0.0, True)
    assert goods["by_account"] == [{"account": IN_TRANSIT, "ledger": 70.0, "expected": 70.0, "difference": 0.0}]
    assert (payables["ledger"], payables["expected"], payables["matches"]) == (70.0, 70.0, True)
    assert [d["doc_id"] for d in payables["documents"]] == [doc]
    assert payables["documents"][0]["status"] == "awaiting_payment"
    assert _row(data, "receivables")["matches"] is True


async def test_tie_out_reports_payable_difference_without_fixing_it(client, session, auth):
    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    await _import(client, auth, _snapshot(lot, "bill", 5, 0))
    await _je(client, auth, {"account": IN_TRANSIT, "debit": 70.0}, {"account": AP, "credit": 63.0},
              {"account": RE, "credit": 7.0})
    n = await _events(session, auth)
    payables = _row(await _tie_out(client, auth), "payables")
    assert (payables["ledger"], payables["expected"], payables["difference"], payables["matches"]) == (
        63.0, 70.0, -7.0, False)
    assert await _events(session, auth) == n


async def test_tie_out_counts_only_lines_dated_by_the_opening_date(client, session, auth):
    """A payable posted after the opening date is period activity, not opening balance."""
    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    await _import(client, auth, _snapshot(lot, "bill", 5, 0))
    await _je(client, auth, {"account": RE, "debit": 70.0}, {"account": AP, "credit": 70.0})
    await _je(client, auth, {"account": RE, "debit": 5.0}, {"account": AP, "credit": 5.0}, ts="2026-07-01")
    assert _row(await _tie_out(client, auth), "payables")["ledger"] == 70.0


async def test_tie_out_goods_exclude_received_share(client, session, auth):
    from celerp.services.auto_je import _bill_carriage_parts

    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    data = _snapshot(lot, "bill", 10, 4)
    await _import(client, auth, data)
    goods = _row(await _tie_out(client, auth), "goods")
    assert goods["expected"] == PRICE * 6
    assert goods["by_account"][0]["account"] == IN_TRANSIT
    # The held parts keep their source line; the share received is held against retained earnings.
    parts = await _bill_carriage_parts(session, auth["company_id"], "doc:x", data)
    assert [(e["account"], s) for e, s in parts] == [(IN_TRANSIT, 0), (RE, None), (AP, None)]
    assert [e.get("debit") or e.get("credit") for e, _ in parts] == [PRICE * 6, PRICE * 4, PRICE * 10]


async def test_tie_out_receivables_net_credit_notes(client, session, auth):
    await _opening_date(client, auth)
    inv = await _import(client, auth, _sales("invoice", 200.0))
    cn = await _import(client, auth, _sales("credit_note", 50.0, issue_date="2026-06-01"))
    await _je(client, auth, {"account": AR, "debit": 150.0}, {"account": RE, "credit": 150.0})
    rec = _row(await _tie_out(client, auth), "receivables")
    assert (rec["ledger"], rec["expected"], rec["matches"]) == (150.0, 150.0, True)
    # Newest first; a credit note reads negative.
    assert [(d["doc_id"], d["base_amount"]) for d in rec["documents"]] == [(cn, -50.0), (inv, 200.0)]


async def test_tie_out_foreign_currency_tolerance(client, session, auth):
    """Three EUR bills of 1.00 at 1.005 each convert to 1.01 (one rounding each), 3.03 in all.
    Up to half a cent per foreign document may differ: 0.01 matches, 0.02 does not."""
    await _opening_date(client, auth)
    for _ in range(3):
        await _import(client, auth, {
            "doc_type": "bill", "contact_id": "supplier:1", "status": "awaiting_payment",
            "doc_number": f"F-{uuid.uuid4().hex[:6]}", "issue_date": "2026-05-01", "currency": "EUR",
            "conversion_rate": "1.005", "total": 1.0, "subtotal": 1.0, "amount_outstanding": 1.0,
            "line_items": [{"name": "Service", "quantity": 1, "unit_price": 1.0, "line_total": 1.0}]})
    await _je(client, auth, {"account": RE, "debit": 3.02}, {"account": AP, "credit": 3.02})
    payables = _row(await _tie_out(client, auth), "payables")
    assert (payables["expected"], payables["difference"], payables["tolerance"], payables["matches"]) == (
        3.03, -0.01, 0.015, True)
    assert payables["by_currency"] == {"EUR": 3.0}
    await _je(client, auth, {"account": AP, "debit": 0.01}, {"account": RE, "credit": 0.01})
    payables = _row(await _tie_out(client, auth), "payables")
    assert (payables["difference"], payables["matches"]) == (-0.02, False)


async def test_tie_out_ignores_record_now_documents(client, session, auth):
    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    await _import(client, auth, _snapshot(lot, "bill", 5, 0), "record_now")
    await _import(client, auth, _sales("invoice", 80.0), "record_now")
    data = await _tie_out(client, auth)
    for key in ("goods", "payables", "receivables"):
        assert (_row(data, key)["expected"], _row(data, key)["documents"]) == (0.0, []), key
    # The ledger side still reads what record_now posted: a difference to explain.
    assert _row(data, "payables")["ledger"] == 70.0 and _row(data, "payables")["matches"] is False


async def test_tie_out_po_counts_in_payables_only(client, session, auth):
    """An order's goods received at import are opening stock, so it is in payables only, at
    what it owes for those goods."""
    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _import(client, auth, _snapshot(lot, "purchase_order", 5, 2))
    data = await _tie_out(client, auth)
    assert [d["doc_id"] for d in _row(data, "payables")["documents"]] == [po]
    assert _row(data, "payables")["expected"] == PRICE * 2
    assert (_row(data, "goods")["expected"], _row(data, "goods")["documents"]) == (0.0, [])


async def test_tie_out_without_opening_date_shows_no_figures(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _import(client, auth, _snapshot(lot, "bill", 5, 0))
    data = await _tie_out(client, auth)
    assert (data["as_of"], data["checks"]) == (None, [])


async def test_tie_out_requires_view_financial_reports(client, session):
    from test_helpers import perm_setup

    ctx = await perm_setup(client, session)
    assert (await client.get(PATH, headers=ctx["operator_h"])).status_code == 403
    assert (await client.get(PATH, headers=ctx["manager_h"])).status_code == 200


async def test_tie_out_page_lists_documents_and_links_the_ledger(client, session, auth):
    """The page shows each check with its documents linked and the goods account linked to
    its ledger at the opening date, and offers nothing that edits the books."""
    from test_helpers import ui_get
    from ui.i18n import t

    await _opening_date(client, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _import(client, auth, _snapshot(lot, "bill", 5, 0))
    await _je(client, auth, {"account": IN_TRANSIT, "debit": 70.0}, {"account": AP, "credit": 63.0},
              {"account": RE, "credit": 7.0})
    html = (await ui_get(auth["headers"], "/reports/opening-tie-out")).text
    assert f'href="/docs/{doc}"' in html
    assert f'href="/reports/ledger/{IN_TRANSIT}?date_to={AS_OF}&amp;src=opening-tie-out"' in html
    assert t("opening_tie_out.matches") in html and t("opening_tie_out.differs") in html
    assert "<form" not in html.split('id="main-content"')[-1].split("</main>")[0]


async def test_tie_out_page_shows_error_without_figures(client, session):
    """A role the API refuses sees the refusal on the page, never a table of figures."""
    from html import escape

    from test_helpers import perm_setup, ui_get
    from ui.i18n import t

    ctx = await perm_setup(client, session)
    page = await ui_get(ctx["operator_h"], "/reports/opening-tie-out")
    assert page.status_code == 200
    assert t("acct.not_authorized") in page.text
    assert '<table class="data-table"' not in page.text
    manager = await ui_get(ctx["manager_h"], "/reports/opening-tie-out")
    assert escape(t("opening_tie_out.no_date")) in manager.text
    assert '<table class="data-table"' not in manager.text
