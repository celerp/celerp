# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The document summary counts what the list shows. For every filter the list takes, the
summary's figures (the chips above the list) count and total exactly the rows the list returns
for the same query, and its ``cards`` count the rows the list returns once the filters the
status cards split by are left out. The test walks every list filter, so a filter added to the
list without reaching the summary fails here."""
from __future__ import annotations

import dataclasses
import re
from collections import Counter, defaultdict
from urllib.parse import urlencode

import pytest

from celerp_docs.routes import _DOC_SPLIT_FILTERS, DocListFilters
from test_contact_money_cards import _day, _demo_contact, _invoice, _ok
from test_helpers import mark_running
from ui.routes.documents import _DOC_PAGE_KEYS, _DOC_STATE_KEYS

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
    return {"contact": contact, "ids": ids, "bill": bill["id"], "credit_note": cn["id"]}


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
        "awaiting_only": {"awaiting_only": "1"},
        "paid_only": {"paid_only": "1"},
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
    """What the summary must say about ``rows``: its row counts, the issued invoices' total and
    every row's total by status."""
    statuses = Counter(r["status"] for r in rows)
    by_status: dict[str, float] = defaultdict(float)
    for r in rows:
        by_status[r["status"]] += float(r["total"] or 0)
    issued = [r for r in rows if r["doc_type"] == "invoice" and r["status"] not in ("draft", "void")]
    return {
        "count_by_status": dict(statuses),
        "total_count": len(rows) - statuses["draft"],
        "draft_count": statuses["draft"],
        "invoice_count": len(issued),
        "all_issued_total": round(sum(float(r["total"]) for r in issued), 2),
        "total_by_status": {k: round(v, 2) for k, v in by_status.items()},
    }


def _figures(summary: dict) -> dict:
    return {k: summary[k] for k in ("count_by_status", "total_count", "draft_count", "invoice_count",
                                    "all_issued_total", "total_by_status")}


def _cases(made: dict) -> list[tuple[str, dict]]:
    """Every list filter alone, and the combinations the pages put together."""
    return list(_filters(made).items()) + [
        ("contact_id+overdue_only", {"contact_id": made["contact"]["id"], "overdue_only": "1"}),
        ("doc_type+overdue_only", {"doc_type": "invoice", "all_issued": "1", "overdue_only": "1"}),
        ("doc_type+unfulfilled_only", {"doc_type": "invoice", "exclude_status": "draft", "unfulfilled_only": "1"}),
        ("doc_type+status", {"doc_type": "invoice", "status": "paid", "q": made["contact"]["name"]}),
    ]


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
    for name, params in _cases(made):
        rows = await _listed(api, params)
        assert 0 < len(rows) < len(everything), (name, len(rows), len(everything))
        summary = await _ok(await api.get("/docs/summary", params=params))
        assert _figures(summary) == _figures_of(rows), name
        split = {k: v for k, v in params.items() if k not in _DOC_SPLIT_FILTERS}
        assert _figures(summary["cards"]) == _figures_of(await _listed(api, split)), name


# The page parameter for each list filter. The rest are API-only: no page sets them, so no card
# link carries them.
_PAGE_KEYS = {"doc_type": "type", "date_from": "from", "date_to": "to"}
_API_ONLY = {"exclude_status", "due_from", "due_to"}
_TYPES = ("invoice", "quotation", "credit_note", "bill", "memo")


def _row_ids(html: str) -> set[str]:
    return set(re.findall(r'href="/docs/(doc:[^"/?#]+)"', html))


def _status_cards(html: str) -> list[tuple[str, int, str]]:
    """(label, count, link) for each status card on a document list."""
    return [(label, int(count), href.replace("&amp;", "&")) for href, label, count in re.findall(
        r'<a href="(/docs\?[^"]*)" class="status-card[^"]*">\s*<span class="status-card-label">([^<]+)</span>\s*'
        r'<span class="status-card-count">(\d+)</span>', html)]


async def test_every_status_card_counts_the_rows_its_link_opens(owner_ui, monkeypatch):
    """For every filter combination above that a page can set, and the type filter on each
    document type: each status card's count is the number of rows on the page its link opens."""
    made = await _documents(owner_ui, monkeypatch)
    assert {_PAGE_KEYS.get(k, k) for k in _FILTER_KEYS if k not in _API_ONLY} <= set(_DOC_STATE_KEYS)
    cases = [(n, p) for n, p in _cases(made) if not set(p) & _API_ONLY]
    cases += [(f"doc_type={dt}", {"doc_type": dt}) for dt in _TYPES]
    checked = 0
    for name, params in cases:
        query = {_PAGE_KEYS.get(k, k): v for k, v in params.items()}
        if not {"from", "to"} & set(query):
            query["preset"] = "all"
        r = await owner_ui.get("/docs?" + urlencode(query))
        assert r.status_code == 200, (name, r.status_code)
        cards = _status_cards(r.text)
        assert cards, name
        for label, count, href in cards:
            landing = await owner_ui.get(href + "&per_page=500", follow_redirects=True)
            assert landing.status_code == 200, (name, href)
            assert count == len(_row_ids(landing.text)), (name, label, count, href)
            checked += 1
    assert checked > len(cases)


async def test_the_summary_refuses_the_sort_the_list_refuses(owner_ui, monkeypatch):
    mark_running(monkeypatch, "celerp-docs")
    for params in ({"sort": "nonsense"}, {"dir": "sideways"}):
        for path in ("/docs", "/docs/summary", "/docs/export/csv"):
            r = await owner_ui.api.get(path, params=params)
            assert r.status_code == 422, (path, params, r.status_code)


# Every document list that has status cards: all types, then each type.
_CARD_LISTS = ("", "invoice", "bill", "credit_note", "debit_note", "quotation", "memo", "receipt",
               "purchase_order", "production_order", "consignment_in")
# The documents of _documents a card of each label must list, and those it must not.
_CARD_MEANING = {
    "Awaiting Payment": ({"sent", "partial", "bill"}, {"paid", "draft", "void", "credit_note"}),
    "Paid": ({"paid"}, {"sent", "partial", "bill", "credit_note"}),
    "Overdue": ({"overdue", "sent", "partial", "bill"}, {"paid", "draft", "void", "credit_note"}),
    "Draft": ({"draft"}, {"sent", "void"}),
    "Pro Forma": ({"draft"}, {"sent", "void"}),
    "Void": ({"void"}, {"sent", "draft"}),
    "All Issued": ({"sent", "partial", "paid", "overdue", "bill"}, {"draft", "void"}),
}


def _card_filter(href: str) -> tuple[tuple[str, str], ...]:
    """The filters a card link sets beyond the list it is on (type, search, dates, page)."""
    query = dict(p.split("=", 1) for p in href.split("?", 1)[1].split("&") if "=" in p)
    return tuple(sorted((k, v) for k, v in query.items() if k not in ("type", "preset", "q", "from", "to")))


async def test_a_card_label_means_one_rule_on_every_list(owner_ui, monkeypatch):
    """Each status card counts the rows its link opens, its label names one filter on every list,
    a typed list's card lists exactly the all-types card's documents of that type, and the card
    of each label lists the documents its label says: a sent invoice, a partly paid invoice and
    a supplier bill awaiting payment are all Awaiting Payment, wherever that card is shown."""
    made = await _documents(owner_ui, monkeypatch)
    api = owner_ui.api
    partial = await _invoice(api, made["contact"], 300.0, issued=1)
    await _ok(await api.post(f"/docs/{partial}/payment", json={
        "amount": 100.0, "method": "cash", "payment_date": _day(1), "bank_account": "1111"}))
    named = made["ids"] | {"partial": partial, "bill": made["bill"], "credit_note": made["credit_note"]}
    assert (await _ok(await api.get(f"/docs/{partial}")))["status"] == "partial"
    # The credit note was applied to its invoice when issued, so it reads "paid"; nobody paid it,
    # so it is never a Paid document.
    assert (await _ok(await api.get(f"/docs/{made['credit_note']}")))["status"] == "paid"
    type_of = {d["id"]: d["doc_type"] for d in await _listed(api, {})}

    listed: dict[tuple[str, str], set[str]] = {}
    filters: dict[str, set[tuple]] = defaultdict(set)
    for doc_type in _CARD_LISTS:
        r = await owner_ui.get("/docs?" + urlencode({"type": doc_type, "preset": "all"}))
        assert r.status_code == 200, (doc_type, r.status_code)
        cards = _status_cards(r.text)
        assert cards, doc_type
        for label, count, href in cards:
            landing = await owner_ui.get(href + "&per_page=500", follow_redirects=True)
            assert landing.status_code == 200, (doc_type, href)
            rows = _row_ids(landing.text)
            assert count == len(rows), (doc_type, label, count, href)
            listed[(doc_type, label)] = rows
            filters[label].add(_card_filter(href))
    assert {label: f for label, f in filters.items() if len(f) != 1} == {}
    for (doc_type, label), rows in listed.items():
        if doc_type and ("", label) in listed:
            assert rows == {i for i in listed[("", label)] if type_of.get(i) == doc_type}, (doc_type, label)
        must, never = _CARD_MEANING.get(label, (set(), set()))
        for name in must:
            if not doc_type or type_of[named[name]] == doc_type:
                assert named[name] in rows, (doc_type, label, name)
        for name in never & set(named):
            assert named[name] not in rows, (doc_type, label, name)
    assert ("", "Awaiting Payment") in listed and ("bill", "Awaiting Payment") in listed
