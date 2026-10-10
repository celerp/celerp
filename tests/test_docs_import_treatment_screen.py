# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The document import screen asks, per document that posts, how it enters the books.

Initial conditions: the review step of /docs/import with a spreadsheet of issued bills, an
issued invoice and a draft quotation. Each document that posts (a bill, invoice or credit
note) gets a choice on the confirm panel, newest first: Already in my opening balances,
Record it now, or ``--`` (no choice). The company's opening balance date pre-fills it: a
document dated on or before that date is offered as already in the opening balances, a
later one as recorded now. With no date, a bill is left at ``--`` (the import refuses it
until it is chosen) and an invoice or credit note is recorded now, as the import does when
it is not told. The chosen treatment travels with the document to the import.
"""
from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from test_import_upsert import _Routes, _numbered_via, _staged_form

pytestmark = pytest.mark.asyncio

_CSV = (
    "doc_type,doc_number,date,status,total\n"
    "bill,B-OLD,2026-01-15,awaiting_payment,100\n"
    "bill,B-NEW,2026-02-10,awaiting_payment,100\n"
    "invoice,I-1,2026-02-01,sent,50\n"
    "quotation,Q-1,2026-02-20,draft,10\n"
)


def _screen(monkeypatch, company: dict):
    from ui.routes import docs_import

    monkeypatch.setattr(docs_import, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "get_company", AsyncMock(return_value=company))
    routes = _Routes()
    docs_import.setup_routes(routes)
    return routes.post_routes


def _choice(html: str, doc_type: str, number: str) -> str | None:
    """The selected value of a document's treatment select, or None with no select."""
    m = re.search(rf'<select[^>]*name="treatment:{doc_type}:{number}"[^>]*>(.*?)</select>', html, re.S)
    if m is None:
        return None
    sel = re.search(r'<option value="([^"]*)" selected', m.group(1))
    return sel.group(1) if sel else ""


async def _review(monkeypatch, company) -> str:
    routes = _screen(monkeypatch, {"id": str(uuid.uuid4()), **company})
    form = await _staged_form("tok", _CSV)
    return to_xml(await routes["/docs/import/revalidate"](form))


async def test_the_opening_balance_date_prefills_each_document_newest_first(monkeypatch):
    html = await _review(monkeypatch, {"opening_balance_date": "2026-01-31"})
    assert _choice(html, "bill", "B-OLD") == "opening_balances"
    assert _choice(html, "bill", "B-NEW") == "record_now"
    assert _choice(html, "invoice", "I-1") == "record_now"
    assert _choice(html, "quotation", "Q-1") is None
    assert html.index("treatment:bill:B-NEW") < html.index("treatment:invoice:I-1") < html.index(
        "treatment:bill:B-OLD")
    assert "2026-01-31" in html
    # The choice is inside the form the import button submits.
    form = re.search(r"<form.*?</form>", html, re.S).group(0)
    assert "treatment:bill:B-OLD" in form


async def test_with_no_opening_balance_date_a_bill_waits_for_a_choice(monkeypatch):
    html = await _review(monkeypatch, {"opening_balance_date": None})
    assert _choice(html, "bill", "B-OLD") == ""
    assert _choice(html, "bill", "B-NEW") == ""
    assert _choice(html, "invoice", "I-1") == "record_now"
    m = re.search(r'name="treatment:bill:B-OLD"[^>]*>(.*?)</select>', html, re.S)
    assert '<option value="" selected>--</option>' in m.group(1)


async def test_the_chosen_treatment_travels_with_the_document(client, session, auth, monkeypatch):
    from ui.routes import docs_import

    async def _batch(_tok, path, records, upsert=False):
        return (await client.post(path, headers=auth["headers"], json={"records": records,
                                                                       "upsert": upsert})).json()

    routes = _screen(monkeypatch, {"id": str(auth["company_id"])})
    monkeypatch.setattr(docs_import.api, "batch_import", _batch)
    monkeypatch.setattr(docs_import.api, "numbered_ids", _numbered_via(client, auth["headers"]))
    confirm = routes["/docs/import/confirm"]
    csv_text = "doc_type,doc_number,date,status,total\nbill,TB-1,2026-01-15,awaiting_payment,100\n"

    left = to_xml(await confirm(await _staged_form("tok", csv_text, **{"treatment:bill:TB-1": ""})))
    assert "Already in my opening balances" in left, left

    chosen = await _staged_form("tok", csv_text, **{"treatment:bill:TB-1": "opening_balances"})
    to_xml(await confirm(chosen))
    rows = (await client.get("/docs", headers=auth["headers"], params={"q": "TB-1"})).json()["items"]
    assert len(rows) == 1, rows
    doc = (await client.get(f"/docs/{rows[0]['id']}", headers=auth["headers"])).json()
    assert doc.get("import_treatment") == "opening_balances", doc


async def test_revalidating_keeps_the_treatment_the_user_chose(monkeypatch):
    """B-OLD is switched to Record it now and B-NEW left at --, a cell fixed and the file
    revalidated: both choices survive the re-render instead of going back to the prefill."""
    routes = _screen(monkeypatch, {"id": str(uuid.uuid4()), "opening_balance_date": "2026-01-31"})
    form = await _staged_form("tok", _CSV, **{"treatment:bill:B-OLD": "record_now",
                                              "treatment:bill:B-NEW": ""})
    html = to_xml(await routes["/docs/import/revalidate"](form))
    assert _choice(html, "bill", "B-OLD") == "record_now"
    assert _choice(html, "bill", "B-NEW") == ""
    assert _choice(html, "invoice", "I-1") == "record_now"


async def test_a_date_that_cannot_be_read_leaves_every_document_to_be_chosen(monkeypatch):
    """Opening date 2026-01-31 and documents dated 15/01/2026, a form the import does not
    read as a date: neither the invoice nor the bill is filled in, so a document already in
    the opening receivables or payables is never booked again by default."""
    csv_text = ("doc_type,doc_number,date,status,total\n"
                "invoice,I-OLD,15/01/2026,sent,50\n"
                "credit_note,C-OLD,15/01/2026,sent,5\n"
                "bill,B-OLD,15/01/2026,awaiting_payment,100\n"
                "invoice,I-NEW,2026-02-15,sent,50\n")
    routes = _screen(monkeypatch, {"id": str(uuid.uuid4()), "opening_balance_date": "2026-01-31"})
    html = to_xml(await routes["/docs/import/revalidate"](await _staged_form("tok", csv_text)))
    assert _choice(html, "invoice", "I-OLD") == ""
    assert _choice(html, "credit_note", "C-OLD") == ""
    assert _choice(html, "bill", "B-OLD") == ""
    assert _choice(html, "invoice", "I-NEW") == "record_now"
