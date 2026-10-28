# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The chips above a document list are fixed by the view, never by the rows it happens to hold.

The walk adds documents of every kind one step at a time (a quotation, a vendor bill, an
invoice, a credit note, drafts of three types, a void invoice, a converted quotation) and opens
the all-types list and each typed list in every view after each step. For one view the chip
labels never change while the rows do, and every money chip totals documents of one type on one
side: the receivable chips total issued invoices only, a Total only the rows of a typed list. The
all-types status cards count documents and never total them across types."""
from __future__ import annotations

import re
from collections import defaultdict

import pytest

from test_contact_money_cards import _day, _demo_contact, _invoice, _ok, _summary_chips
from ui.components.table import fmt_money

pytestmark = pytest.mark.asyncio

_TERM = "Chipwalk"
_TYPES = ("", "invoice", "quotation", "bill", "credit_note")
# Views that admit only drafts or voids: a typed list totals its rows there, whatever mix of the
# two it admits.
_UNISSUED_VIEWS = ("view=drafts", "status=void", "status_in=draft,void")
_VIEWS = ("", "all_issued=1", *_UNISSUED_VIEWS, "status_in=draft,sent", "converted_to_type=invoice")
# Chips that state an amount, and the documents each one may total.
_RECEIVABLE = {"AR": "amount_outstanding", "Billed": "total", "Paid": "amount_paid"}


async def _doc(api, doc_type: str, contact: dict, total: float, steps=("finalize",), **extra) -> str:
    doc = await _ok(await api.post("/docs", json={
        "doc_type": doc_type, "contact_id": contact["id"], "contact_name": contact["name"],
        "issue_date": _day(), "due_date": _day(),
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total, **extra}))
    for step in steps:
        await _ok(await api.post(f"/docs/{doc['id']}/{step}", json={}))
    return doc["id"]


async def _check_view(owner_ui, url: str, doc_type: str, view: str) -> tuple[str, ...]:
    """The chip labels of ``url``, after checking each money chip against the rows it lists."""
    r = await owner_ui.get(url)
    assert r.status_code == 200, (url, r.text[:300])
    chips = _summary_chips(r.text)
    if doc_type and view in _UNISSUED_VIEWS:
        assert "Total" in chips and not set(chips) & set(_RECEIVABLE), (url, chips)
    rows = []
    for doc_id in dict.fromkeys(re.findall(r'href="/docs/(doc:[^"/?#]+)"', r.text)):
        rows.append(await _ok(await owner_ui.api.get(f"/docs/{doc_id}")))
    issued = [d for d in rows if d.get("doc_type") == "invoice" and d.get("status") not in ("draft", "void")]
    for label, value in chips.items():
        if label in _RECEIVABLE:
            field = _RECEIVABLE[label]
            assert value == fmt_money(sum(float(d.get(field) or 0) for d in issued), None), (url, label, chips)
        elif label == "Total":
            # A Total sums one type's rows: never on the all-types list, never across types.
            assert doc_type, (url, chips)
            assert {d.get("doc_type") for d in rows} <= {doc_type}, (url, chips)
            assert value == fmt_money(sum(float(d.get("total") or 0) for d in rows), None), (url, chips)
        elif label == "Issued invoices":
            assert value == str(len(issued)), (url, chips)
        else:
            assert value == str(len(rows)), (url, label, chips)
    if not doc_type:
        cards = r.text.split('class="status-cards"', 1)[-1].split("<table", 1)[0]
        assert "status-card-total" not in cards, (url, "all-types status cards total documents of every type")
    return tuple(chips)


@pytest.mark.timeout(180)  # renders every type and view of both lists after each of seven steps
async def test_the_chips_of_a_view_never_change_with_its_rows(owner_ui, monkeypatch):
    await _demo_contact(owner_ui, monkeypatch)
    api = owner_ui.api
    customer = await _ok(await api.post("/crm/contacts", json={"name": f"{_TERM} Customer", "contact_type": "customer"}))
    customer = {"id": customer["id"], "name": f"{_TERM} Customer"}
    vendor = await _ok(await api.post("/crm/contacts", json={"name": f"{_TERM} Supplier", "contact_type": "vendor"}))
    vendor = {"id": vendor["id"], "name": f"{_TERM} Supplier"}

    async def credit_note():
        cn = await _ok(await api.post("/docs", json={
            "doc_type": "credit_note", "original_doc_id": invoices[0], "contact_id": customer["id"],
            "line_items": [{"name": "Credit", "quantity": 1, "unit_price": 30.0, "line_total": 30.0}], "total": 30.0}))
        await _ok(await api.post(f"/docs/{cn['id']}/finalize", json={}))

    async def drafts():
        await _doc(api, "quotation", customer, 40.0, steps=())
        await _doc(api, "bill", vendor, 20.0, steps=())
        await _invoice(api, customer, 10.0, steps=())

    invoices: list[str] = []

    async def invoice():
        invoices.append(await _invoice(api, customer, 100.0))

    steps = (
        lambda: _doc(api, "quotation", customer, 1000.0, steps=("finalize", "send")),
        lambda: _doc(api, "bill", vendor, 250.0),
        invoice,
        credit_note,
        drafts,
        lambda: _invoice(api, customer, 5.0, steps=("finalize", "void")),
        lambda: _doc(api, "quotation", customer, 70.0, steps=("convert",)),
    )
    labels: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for step in steps:
        await step()
        for doc_type in _TYPES:
            for view in _VIEWS:
                query = "&".join(p for p in (f"type={doc_type}" if doc_type else "", view,
                                             f"q={_TERM}", "preset=all", "per_page=500") if p)
                for path in ("/docs/search", "/docs"):
                    labels[f"{path}?{query}"].add(await _check_view(owner_ui, f"{path}?{query}", doc_type, view))
    changed = {url: seen for url, seen in labels.items() if len(seen) != 1}
    assert not changed, changed
