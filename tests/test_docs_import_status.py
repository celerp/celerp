# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document's status comes in from the spreadsheet, and only as a status the books can
honour.

Initial conditions: the documents import screen on a company with no opening balance date,
importing through the real batch import. The column mapper suggests the file's ``status``
column for documents and lists, and skips it for inventory, whose status the app manages.

Oracle for every documents row: under "Already in my opening balances" nothing posts and the
document reads the status, paid amount and amount outstanding its file gives; under "Record
it now" the receivable (1120) or payable (2110) equals the document's amount outstanding, and
a document the file says was paid, which names no payment, is refused rather than booked
without one. A status the file's columns cannot back, or one its amount outstanding
contradicts, refuses that row with its reason in the reader's language and writes nothing.
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml
from sqlalchemy import select
from starlette.requests import Request

import ui.api_client as api_client
from celerp.models.projections import Projection
from test_import_invariants import stage_dir  # noqa: F401
from test_import_navigation import _ui_request
from test_import_upsert import _Form, _numbered_via, _staged_form
from test_set_aside_older_paths import _net
from ui.i18n import t

pytestmark = pytest.mark.asyncio

LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


class _Routes:
    def __init__(self):
        self.get_routes: dict = {}
        self.post_routes: dict = {}

    def get(self, path):
        def deco(fn):
            self.get_routes[path] = fn
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.post_routes[path] = fn
            return fn
        return deco


def _screen(monkeypatch, client, auth) -> _Routes:
    from ui.routes import docs_import

    async def _batch(_tok, path, records, upsert=False):
        return (await client.post(path, headers=auth["headers"], json={"records": records,
                                                                       "upsert": upsert})).json()

    monkeypatch.setattr(docs_import, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "get_company", AsyncMock(return_value={"id": str(auth["company_id"])}))
    monkeypatch.setattr(docs_import.api, "batch_import", _batch)
    monkeypatch.setattr(docs_import.api, "numbered_ids", _numbered_via(client, auth["headers"]))
    routes = _Routes()
    docs_import.setup_routes(routes)
    return routes


class _Page(Request):
    """A form post on the import page, drawn by the real shell."""

    def __init__(self, form: dict):
        super().__init__({"type": "http", "method": "POST", "path": "/docs/import", "headers": [],
                          "query_string": b""})
        self._posted = form

    async def form(self):
        return self._posted


async def _page_form(csv_text: str, **extra) -> _Page:
    return _Page((await _staged_form("tok", csv_text, **extra))._form)


def _hidden(html: str, prefix: str) -> dict[str, str]:
    return {name: value for name, value in re.findall(
        rf'<input type="hidden" name="({re.escape(prefix)}[^"]*)"[^>]*value="([^"]*)"', html)}


async def _doc(client, auth, number: str) -> dict:
    rows = (await client.get("/docs", headers=auth["headers"], params={"q": number})).json()["items"]
    rows = [r for r in rows if r.get("doc_number") == number or r.get("ref_id") == number]
    if not rows:
        return {}
    return (await client.get(f"/docs/{rows[0]['id']}", headers=auth["headers"])).json()


async def _posted_for(session, auth, doc_id: str) -> int:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"je:auto:{doc_id}%")))).scalars().all()
    return sum(1 for p in rows if (p.state or {}).get("status") == "posted")


async def _confirm(routes, csv_text: str, **form) -> str:
    return to_xml(await routes.post_routes["/docs/import/confirm"](await _staged_form("tok", csv_text, **form)))


# N1: the app's own documents template comes back with its status


async def test_reimporting_the_documents_template_keeps_its_status(client, session, auth, monkeypatch):
    """RED before the change: the mapper skipped every ``status`` column, so the template's
    invoice came back as a draft."""
    from ui.routes import docs_import

    routes = _screen(monkeypatch, client, auth)
    template = (await routes.get_routes["/docs/import/template"](None)).body.decode()
    number = next(csv_row for csv_row in template.splitlines()[1:]).split(",")[1]

    async def _staged(token, form, known=()):
        return [dict(r) for r in csv.DictReader(io.StringIO(template))], \
            await docs_import.stash_import_csv(token, template), None

    monkeypatch.setattr(docs_import, "stage_tabular_upload", _staged)
    mapping_page = to_xml(await routes.post_routes["/docs/import/preview"](await _page_form("")))
    mapping = _hidden(mapping_page, "map__")
    assert mapping["map__status"] == "status", mapping

    csv_ref = _hidden(mapping_page, "csv_ref")["csv_ref"]
    review = to_xml(await routes.post_routes["/docs/import/mapped"](
        _Page({**mapping, "csv_ref": csv_ref})))
    staged = _hidden(review, "csv_ref")["csv_ref"]
    result = to_xml(await routes.post_routes["/docs/import/confirm"](_Form({"csv_ref": staged})))
    assert "flash--error" not in result, result

    doc = await _doc(client, auth, number)
    assert doc.get("status") == "awaiting_payment", doc
    assert float(doc["amount_outstanding"]) == float(doc["total"]) > 0, doc
    assert await _net(session, auth, "1120", prefix="je:") == float(doc["total"])


# N2: inventory still skips and strips its status; lists map theirs


async def test_inventory_mapper_still_skips_and_strips_status(stage_dir):  # noqa: F811
    from celerp.importers.tabular import MAPPING_SKIP, apply_column_mapping

    with patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])), \
         patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})):
        r = await _ui_request("POST", "/inventory/import/preview",
                              files={"csv_file": ("stock.csv", io.BytesIO(b"Name,Status,Qty\nBasket,sold,4\n"),
                                                  "text/csv")})
    assert r.status_code == 200, r.text
    mapping = _hidden(r.text, "map__")
    assert mapping["map__Status"] == MAPPING_SKIP, mapping
    remapped, cols = apply_column_mapping(mapping, "Name,Status,Qty\nBasket,sold,4\n")
    assert "Status" not in cols and "status" not in cols and "sold" not in remapped, remapped


async def test_lists_mapper_maps_status(stage_dir):  # noqa: F811
    r = await _ui_request("POST", "/lists/import/preview",
                          files={"csv_file": ("lists.csv", io.BytesIO(b"ref_id,status,total\nL-1,sent,10\n"),
                                              "text/csv")})
    assert r.status_code == 200, r.text
    assert _hidden(r.text, "map__")["map__status"] == "status"


# N3: settled and finalized statuses keep the books consistent under both treatments


_HEAD = "doc_type,doc_number,date,contact_name,status,total,amount_outstanding,line_stone_type,line_qty,line_total_price\n"
_CONTROL = {"invoice": "1120", "bill": "2110"}


def _row(doc_type, number, status, outstanding, total=100):
    return f"{doc_type},{number},2026-03-01,Acme Corp,{status},{total},{outstanding},Service,1,{total}\n"


@pytest.mark.parametrize("doc_type", ["invoice", "bill"])
@pytest.mark.parametrize("status,outstanding,paid", [("paid", "0", 100.0), ("paid", "", 100.0),
                                                     ("partial", "40", 60.0)])
async def test_a_settled_document_already_in_the_opening_balances_posts_nothing(
        client, session, auth, monkeypatch, doc_type, status, outstanding, paid):
    routes = _screen(monkeypatch, client, auth)
    number = f"S-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, number, status, outstanding),
                          **{f"treatment:{doc_type}:{number}": "opening_balances"})
    assert "flash--error" not in html, html
    doc = await _doc(client, auth, number)
    assert doc["status"] == status, doc
    assert float(doc["amount_paid"]) == paid and float(doc["amount_outstanding"]) == 100.0 - paid, doc
    assert await _posted_for(session, auth, doc["id"]) == 0
    assert await _net(session, auth, _CONTROL[doc_type], prefix="je:") == 0.0


@pytest.mark.parametrize("doc_type", ["invoice", "bill"])
@pytest.mark.parametrize("status,outstanding", [("paid", "0"), ("partial", "40")])
async def test_a_settled_document_recorded_now_is_refused_for_naming_no_payment(
        client, session, auth, monkeypatch, doc_type, status, outstanding):
    """RED before the change: a paid invoice recorded now left its whole total open on 1120
    while the document read paid."""
    routes = _screen(monkeypatch, client, auth)
    number = f"R-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, number, status, outstanding),
                          **{f"treatment:{doc_type}:{number}": "record_now"})
    assert t("doc_import.settled_record_now", number=number, status=status) in html, html
    assert await _doc(client, auth, number) == {}
    assert await _net(session, auth, _CONTROL[doc_type], prefix="je:") == 0.0


async def test_a_paid_invoice_with_no_treatment_is_refused_as_recorded_now(client, session, auth, monkeypatch):
    """An invoice left at ``--`` is recorded now, so the same refusal holds."""
    routes = _screen(monkeypatch, client, auth)
    number = f"N-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row("invoice", number, "paid", "0"))
    assert t("doc_import.settled_record_now", number=number, status="paid") in html, html
    assert await _doc(client, auth, number) == {}


@pytest.mark.parametrize("doc_type,treatment", [("invoice", "record_now"), ("invoice", "opening_balances"),
                                                ("bill", "opening_balances")])
@pytest.mark.parametrize("outstanding", ["100", ""])
async def test_an_open_document_leaves_exactly_its_outstanding(
        client, session, auth, monkeypatch, doc_type, treatment, outstanding):
    """RED before the change: a blank amount outstanding was read as 0, so an invoice booked
    now held 100 on 1120 while reading nothing owed."""
    routes = _screen(monkeypatch, client, auth)
    number = f"O-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, number, "awaiting_payment", outstanding),
                          **{f"treatment:{doc_type}:{number}": treatment})
    assert "flash--error" not in html, html
    doc = await _doc(client, auth, number)
    assert doc["status"] == "awaiting_payment" and float(doc["amount_outstanding"]) == 100.0, doc
    assert float(doc.get("amount_paid") or 0) == 0.0, doc
    held = await _net(session, auth, _CONTROL[doc_type], prefix="je:")
    if treatment == "opening_balances":
        assert held == 0.0 and await _posted_for(session, auth, doc["id"]) == 0
    else:
        assert held == 100.0


async def test_a_void_document_imports_void_and_posts_nothing(client, session, auth, monkeypatch):
    routes = _screen(monkeypatch, client, auth)
    number = f"V-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row("invoice", number, "void", ""))
    assert "flash--error" not in html, html
    doc = await _doc(client, auth, number)
    assert doc["status"] == "void", doc
    assert await _posted_for(session, auth, doc["id"]) == 0


@pytest.mark.parametrize("doc_type,status", [("invoice", "received"), ("invoice", "shipped"),
                                             ("bill", "received"), ("bill", "partially_received")])
async def test_a_status_the_file_cannot_back_is_refused(client, session, auth, monkeypatch, doc_type, status):
    """RED before the change: any status was written as it came, so an invoice "shipped" read as
    issued while nothing was booked for it, and a bill "received" held no goods."""
    routes = _screen(monkeypatch, client, auth)
    number = f"U-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, number, status, ""),
                          **{f"treatment:{doc_type}:{number}": "opening_balances"})
    assert f"{number} was not imported" in html and status in html, html
    assert "doc_import.status_not_importable" not in html, html
    assert await _doc(client, auth, number) == {}


# N4: other systems export an open document as "Unpaid" or "Overdue". Overdue is never a
# stored status (it is read from the due date), and both mean the whole total is still owed,
# so both import as awaiting_payment and are checked and booked exactly as it is.


@pytest.mark.parametrize("doc_type,treatment", [("invoice", "record_now"), ("invoice", "opening_balances"),
                                                ("bill", "record_now"), ("bill", "opening_balances")])
@pytest.mark.parametrize("status", ["Unpaid", "Overdue"])
@pytest.mark.parametrize("outstanding", ["100", ""])
async def test_an_unpaid_or_overdue_document_imports_as_awaiting_payment(
        client, session, auth, monkeypatch, doc_type, treatment, status, outstanding):
    """RED before the change: "unpaid" and "overdue" were refused as statuses the file cannot
    back, so a file from another system could not bring its open documents in."""
    routes = _screen(monkeypatch, client, auth)
    number = f"A-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, number, status, outstanding),
                          **{f"treatment:{doc_type}:{number}": treatment})
    assert "flash--error" not in html, html
    doc = await _doc(client, auth, number)
    assert doc["status"] == "awaiting_payment" and float(doc["amount_outstanding"]) == 100.0, doc
    assert float(doc.get("amount_paid") or 0) == 0.0, doc
    held = await _net(session, auth, _CONTROL[doc_type], prefix="je:")
    # The same document imported as awaiting_payment books exactly as much again.
    control = f"C-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, control, "awaiting_payment", outstanding),
                          **{f"treatment:{doc_type}:{control}": treatment})
    assert "flash--error" not in html, html
    assert await _net(session, auth, _CONTROL[doc_type], prefix="je:") == 2 * held
    if treatment == "opening_balances":
        assert held == 0.0 and await _posted_for(session, auth, doc["id"]) == 0
    else:
        assert abs(held) == 100.0 and await _posted_for(session, auth, doc["id"]) > 0


@pytest.mark.parametrize("doc_type", ["invoice", "bill"])
async def test_an_overdue_document_owing_less_than_its_total_is_still_refused(
        client, session, auth, monkeypatch, doc_type):
    """An overdue document owes its whole total; one whose file says it owes part was part
    paid, which the figures check refuses as it does for awaiting_payment."""
    routes = _screen(monkeypatch, client, auth)
    number = f"P-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row(doc_type, number, "Overdue", "40"),
                          **{f"treatment:{doc_type}:{number}": "opening_balances"})
    assert f"{number} was not imported" in html and "amount outstanding" in html, html
    assert await _doc(client, auth, number) == {}


@pytest.mark.parametrize("status,outstanding", [("paid", "30"), ("partial", "0"), ("partial", "100"),
                                                ("partial", ""), ("awaiting_payment", "40")])
async def test_a_status_its_amount_outstanding_contradicts_is_refused(
        client, session, auth, monkeypatch, status, outstanding):
    routes = _screen(monkeypatch, client, auth)
    number = f"D-{uuid.uuid4().hex[:6]}"
    html = await _confirm(routes, _HEAD + _row("invoice", number, status, outstanding),
                          **{f"treatment:invoice:{number}": "opening_balances"})
    assert f"{number} was not imported" in html and "amount outstanding" in html, html
    assert await _doc(client, auth, number) == {}


async def test_every_refusal_reads_in_every_language():
    for key in ("doc_import.status_not_importable", "doc_import.status_figures_disagree",
                "doc_import.settled_record_now", "lists_import.status_not_importable"):
        for path in sorted(LOCALES.glob("*.json")):
            assert key in json.loads(path.read_text(encoding="utf-8")), f"{path.name} has no {key}"


@pytest.mark.parametrize("status,kept", [("finalized", True), ("void", True), ("sent", False), ("paid", False)])
async def test_a_list_keeps_a_list_status_and_refuses_any_other(client, auth, monkeypatch, status, kept):
    """RED before the change: with status mapped, a list took any status the file gave."""
    from ui.routes import lists_import

    async def _batch(_tok, path, records, upsert=False):
        return (await client.post(path, headers=auth["headers"], json={"records": records,
                                                                       "upsert": upsert})).json()

    monkeypatch.setattr(lists_import, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "get_company", AsyncMock(return_value={"id": str(auth["company_id"])}))
    monkeypatch.setattr(api_client, "batch_import", _batch)
    monkeypatch.setattr(api_client, "numbered_ids", _numbered_via(client, auth["headers"]))
    routes = _Routes()
    lists_import.setup_routes(routes)
    ref = f"L-{uuid.uuid4().hex[:6]}"
    html = to_xml(await routes.post_routes["/lists/import/confirm"](
        await _staged_form("tok", f"ref_id,status,total\n{ref},{status},10\n")))
    rows = (await client.get("/lists", headers=auth["headers"], params={"q": ref})).json()["items"]
    if kept:
        assert "flash--error" not in html and [r["status"] for r in rows] == [status], (html, rows)
    else:
        assert t("lists_import.status_not_importable", ref=ref, status=status,
                 allowed="closed, draft, finalized, void") in html, html
        assert rows == []
