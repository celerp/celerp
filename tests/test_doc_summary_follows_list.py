# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The document summary counts what the list shows. For every filter the list takes, the
summary's figures (the chips above the list) count and total exactly the rows the list returns
for the same query, and its ``cards`` count the rows the list returns once the filters the
status cards split by are left out. The test walks every list filter, so a filter added to the
list without reaching the summary fails here."""
from __future__ import annotations

import dataclasses
from collections import Counter

import pytest

from celerp_docs.routes import _DOC_SPLIT_FILTERS, DocListFilters
from test_contact_money_cards import _day, _demo_contact, _invoice, _ok
from ui.routes.documents import _DOC_PAGE_KEYS

pytestmark = pytest.mark.asyncio

_ORDER_KEYS = {"sort", "dir"}
_FILTER_KEYS = [f.name for f in dataclasses.fields(DocListFilters) if f.name not in _ORDER_KEYS]


async def _documents(owner_ui, monkeypatch) -> dict:
    """The demo company plus one customer's invoices in every state, a credit note and a
    quotation turned into an invoice, and a vendor's bill."""
    contact = await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
    ids = {
        "overdue": await _invoice(api, contact, 400.0, issued=40, due_date=_day(30)),
        "sent": await _invoice(api, contact, 250.0, issued=2),
        "paid": await _invoice(api, contact, 125.0, steps=("finalize", "send", "pay"), issued=3),
        "draft": await _invoice(api, contact, 90.0, steps=()),
        "void": await _invoice(api, contact, 60.0, steps=("finalize", "void")),
        "excluded": await _invoice(api, contact, 30.0, issued=5, accounting_sync_excluded=True),
    }
    cn = await _ok(await api.post("/docs", json={
        "doc_type": "credit_note", "original_doc_id": ids["sent"], "contact_id": contact["id"],
        "line_items": [{"name": "Credit", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}],
        "total": 50.0}))
    await _ok(await api.post(f"/docs/{cn['id']}/finalize", json={}))
    vendor = await _ok(await api.post("/crm/contacts", json={"name": "Bill Vendor", "contact_type": "vendor"}))
    bill = await _ok(await api.post("/docs", json={
        "doc_type": "bill", "contact_id": vendor["id"], "contact_name": "Bill Vendor",
        "issue_date": _day(4), "due_date": _day(4),
        "line_items": [{"name": "Supplies", "quantity": 1, "unit_price": 80.0, "line_total": 80.0}],
        "total": 80.0}))
    await _ok(await api.post(f"/docs/{bill['id']}/finalize", json={}))
    quote = await _ok(await api.post("/docs", json={
        "doc_type": "quotation", "contact_id": contact["id"], "contact_name": contact["name"],
        "issue_date": _day(6),
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": 70.0, "line_total": 70.0}],
        "total": 70.0}))
    await _ok(await api.post(f"/docs/{quote['id']}/convert", json={}))
    return {"contact": contact, "ids": ids, "bill": bill["id"]}


def _filters(made: dict) -> dict[str, dict]:
    """One query per list filter, each narrowing the set to something other than everything."""
    contact, ids = made["contact"], made["ids"]
    return {
        "doc_type": {"doc_type": "invoice"},
        "status": {"status": "sent"},
        "status_in": {"status_in": "sent,paid"},
        "exclude_status": {"exclude_status": "draft"},
        "date_from": {"date_from": _day(3)},
        "date_to": {"date_to": _day(3)},
        "due_from": {"due_from": _day(3)},
        "due_to": {"due_to": _day(3)},
        "q": {"q": contact["name"]},
        "contact_id": {"contact_id": contact["id"]},
        "overdue_only": {"overdue_only": "1"},
        "all_issued": {"all_issued": "1"},
        "unfulfilled_only": {"unfulfilled_only": "1"},
        "not_restocked": {"not_restocked": "1"},
        "not_stocked": {"not_stocked": "1"},
        "converted_to_type": {"converted_to_type": "invoice"},
        "accounting_sync": {"accounting_sync": "excluded"},
        "ids": {"ids": f"{ids['overdue']},{ids['draft']},{made['bill']}"},
    }


async def _listed(api, params: dict) -> list[dict]:
    body = await _ok(await api.get("/docs", params=params))
    assert body["total"] == len(body["items"]), body["total"]
    return body["items"]


def _figures_of(rows: list[dict]) -> dict:
    """What the summary must say about ``rows``: its row counts and the issued invoices' total."""
    statuses = Counter(r["status"] for r in rows)
    issued = [r for r in rows if r["doc_type"] == "invoice" and r["status"] not in ("draft", "void")]
    return {
        "count_by_status": dict(statuses),
        "total_count": len(rows) - statuses["draft"],
        "draft_count": statuses["draft"],
        "invoice_count": len(issued),
        "all_issued_total": round(sum(float(r["total"]) for r in issued), 2),
    }


def _figures(summary: dict) -> dict:
    return {k: summary[k] for k in ("count_by_status", "total_count", "draft_count", "invoice_count", "all_issued_total")}


async def test_every_list_filter_is_named_here():
    assert sorted(_filters({"contact": {"id": "c", "name": "n"}, "ids": dict.fromkeys(("overdue", "draft"), "d"),
                            "bill": "b"})) == sorted(_FILTER_KEYS)
    # The UI sends the summary every list parameter except the page and the order.
    assert set(_DOC_PAGE_KEYS) & set(_FILTER_KEYS) == set()
    assert set(_DOC_SPLIT_FILTERS) <= set(_FILTER_KEYS)


async def test_the_summary_counts_the_rows_the_list_shows_for_every_filter(owner_ui, monkeypatch):
    made = await _documents(owner_ui, monkeypatch)
    api = owner_ui.api
    everything = await _listed(api, {})
    cases = list(_filters(made).items()) + [
        ("contact_id+overdue_only", {"contact_id": made["contact"]["id"], "overdue_only": "1"}),
        ("doc_type+overdue_only", {"doc_type": "invoice", "all_issued": "1", "overdue_only": "1"}),
        ("doc_type+unfulfilled_only", {"doc_type": "invoice", "exclude_status": "draft", "unfulfilled_only": "1"}),
        ("doc_type+status", {"doc_type": "invoice", "status": "paid", "q": made["contact"]["name"]}),
    ]
    for name, params in cases:
        rows = await _listed(api, params)
        assert 0 < len(rows) < len(everything), (name, len(rows), len(everything))
        summary = await _ok(await api.get("/docs/summary", params=params))
        assert _figures(summary) == _figures_of(rows), name
        split = {k: v for k, v in params.items() if k not in _DOC_SPLIT_FILTERS}
        assert _figures(summary["cards"]) == _figures_of(await _listed(api, split)), name
