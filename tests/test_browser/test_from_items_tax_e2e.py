# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End to end: a line added to a document in a new company with demo items is taxed once.

Real API and UI servers on Postgres: the company takes its tax regime from its default
location's country, the demo items are seeded the way setup seeds them, and lines are added
through every path a user has: "Send to" from inventory, "Add to document", a scan and the
document editor's item picker.

- A line takes every default tax of the company, never only the first: a company set up before
  IGST became the India default holds CGST 9% and SGST 9% both as defaults, 18% in all.
- A draft taxed as a whole (document taxes) leaves off a new line only the defaults the
  document already charges, so no tax is charged twice and none is left out.
- A stored tax entry the editor cannot read is kept on every save: an edit is refused naming
  it, and removing it on the page lets the next save through.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
import psycopg2
import pytest

from .conftest import _API_LIMITS
from .test_demo_items_link import _seed_demo_items
from .test_reserved_conflict_modal import _picker_add

pytestmark = pytest.mark.browser

# The India rows a company set up before IGST 18% became the default still holds.
_INDIA_BEFORE = [
    {"name": "CGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True},
    {"name": "SGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True},
    {"name": "IGST 18%", "rate": 18.0, "tax_type": "both", "is_default": False},
    {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False},
]
_VAT_WHOLE = {"code": "VAT 7%", "rate": 7.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}


@contextmanager
def _demo_company(api_server, ui_server, api, country: str, taxes: list[dict] | None = None):
    """A new company in `country` holding the demo items: (API client, UI client, token, items)."""
    r = api.post("/companies", json={"name": f"Tax {country} {uuid.uuid4().hex[:6]}"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    with httpx.Client(base_url=api_server, headers={"Authorization": f"Bearer {token}"}, timeout=10,
                      limits=_API_LIMITS) as company, \
            httpx.Client(base_url=ui_server, cookies={"celerp_token": token}, timeout=30) as ui:
        default = next(loc for loc in company.get("/companies/me/locations").json()["items"] if loc["is_default"])
        r = company.patch(f"/companies/me/locations/{default['id']}",
                          json={"address": {**(default.get("address") or {}), "country": country}})
        assert r.status_code == 200, r.text
        if taxes is not None:
            r = company.patch("/companies/me/taxes", json={"taxes": taxes})
            assert r.status_code == 200, r.text
        _seed_demo_items(company)
        items = company.get("/items", params={"filter": "demo", "limit": 500}).json()["items"]
        assert items, "no demo items were seeded"
        yield company, ui, token, items


def _taxed_once(doc: dict, factor: float) -> None:
    subtotal, total = float(doc["subtotal"]), float(doc["total"])
    assert subtotal > 0, doc
    assert abs(total - round(subtotal * factor, 2)) < 0.01 * len(doc["line_items"]), (
        subtotal, total, doc.get("doc_taxes"), [line.get("taxes") for line in doc["line_items"]])


def _codes(line: dict) -> list[str]:
    return [tax["code"] for tax in line.get("taxes") or []]


@pytest.mark.parametrize(("country", "taxes", "factor"),
                         [("GB", None, 1.20), ("IN", None, 1.18), ("IN", _INDIA_BEFORE, 1.18)])
def test_from_items_invoice_total_is_subtotal_plus_default_rate(api_server, ui_server, api, country, taxes, factor):
    with _demo_company(api_server, ui_server, api, country, taxes) as (company, ui, _, items):
        r = ui.post("/docs/from-items/new", data={"selected": [i["id"] for i in items]})
        assert r.status_code == 204, r.text
        doc = company.get(f"/docs/{r.headers['HX-Redirect'].rsplit('/', 1)[-1]}").json()
    _taxed_once(doc, factor)
    if taxes is not None:
        assert {tuple(_codes(line)) for line in doc["line_items"]} == {("CGST 9%", "SGST 9%")}


@pytest.mark.parametrize(("country", "shown"), [("IN", True), ("TH", False)])
def test_the_editor_of_a_company_offering_igst_explains_it(api_server, ui_server, api, country, shown):
    from ui.i18n import t

    with _demo_company(api_server, ui_server, api, country) as (company, ui, _, items):
        r = ui.post("/docs/from-items/new", data={"selected": [items[0]["id"]]})
        assert r.status_code == 204, r.text
        html = ui.get(r.headers["HX-Redirect"]).text
    assert (t("documents.igst_note") in html) is shown


def _whole_draft(company, item: dict) -> str:
    r = company.post("/docs", json={
        "doc_type": "invoice", "status": "draft", "doc_taxes": [_VAT_WHOLE],
        "line_items": [{"description": item["name"], "entity_id": item["id"], "quantity": 1,
                        "unit_price": 100.0}]})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_every_way_to_add_a_line_to_a_draft_taxed_as_a_whole_adds_no_line_tax(api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "TH") as (company, ui, _, items):
        added = items[1:4]
        doc_id = _whole_draft(company, items[0])
        r = ui.post("/docs/from-items/add", data={"selected": [added[0]["id"]], "target_id": doc_id})
        assert r.status_code == 204, r.text
        r = ui.post("/api/items/send-to", data={"selected": [added[1]["id"]], "send_to_doc_type": "invoice",
                                                "send_to_target": doc_id})
        assert r.status_code == 204, r.text
        r = company.post(f"/docs/{doc_id}/scan", json={"barcode": added[2]["sku"]})
        assert r.status_code == 200 and r.json()["scanned"] == 1, r.text
        doc = company.get(f"/docs/{doc_id}").json()

        memo = company.post("/docs", json={"doc_type": "memo", "status": "draft", "doc_taxes": [_VAT_WHOLE],
                                           "line_items": []}).json()["id"]
        r = ui.post("/api/items/send-to", data={"selected": [added[0]["id"]], "send_to_doc_type": "memo",
                                                "send_to_target": memo})
        assert r.status_code == 204, r.text
        memo_doc = company.get(f"/docs/{memo}").json()

    assert len(doc["line_items"]) == 4, doc["line_items"]
    assert [_codes(line) for line in doc["line_items"]] == [[]] * 4
    assert [tax["code"] for tax in doc["doc_taxes"]] == ["VAT 7%"]
    _taxed_once(doc, 1.07)
    assert [_codes(line) for line in memo_doc["line_items"]] == [[]]


def _saved(company, doc_id: str, lines: int) -> dict:
    for _ in range(120):
        doc = company.get(f"/docs/{doc_id}").json()
        if len(doc.get("line_items") or []) == lines:
            return doc
        time.sleep(0.25)
    raise AssertionError(f"the editor did not save {lines} lines: {doc.get('line_items')}")


@pytest.mark.parametrize(("country", "taxes", "doc_taxes", "line_codes", "factor"), [
    ("TH", None, [_VAT_WHOLE], [], 1.07),
    ("TH", None, [], ["VAT 7%"], 1.07),
    ("IN", _INDIA_BEFORE, [], ["CGST 9%", "SGST 9%"], 1.18),
])
def test_a_line_picked_in_the_editor_is_taxed_once(page, api_server, ui_server, api, country, taxes, doc_taxes,
                                                   line_codes, factor):
    with _demo_company(api_server, ui_server, api, country, taxes) as (company, _, token, items):
        r = company.post("/docs", json={"doc_type": "invoice", "status": "draft", "doc_taxes": doc_taxes,
                                        "line_items": []})
        assert r.status_code in (200, 201), r.text
        doc_id = r.json()["id"]
        ctx = page.context.browser.new_context(base_url=ui_server)
        try:
            ctx.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
            ctx.route("**/health/system", lambda route: route.fulfill(json={"overall": "ok"}))
            tab = ctx.new_page()
            tab.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
            _picker_add(tab, items[0]["sku"])
            doc = _saved(company, doc_id, 1)
        finally:
            ctx.close()
    assert _codes(doc["line_items"][0]) == line_codes
    _taxed_once(doc, factor)


_LEVY = {"code": "Levy 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}
# Listed levy first: the company's saved calculation order, not the list, puts VAT first.
_COMPOUND_AFTER_VAT = [
    {"name": "Levy 10%", "rate": 10.0, "tax_type": "both", "is_default": True, "is_compound": True,
     "default_order": 1},
    {"name": "VAT 5%", "rate": 5.0, "tax_type": "both", "is_default": True, "is_compound": False,
     "default_order": 0},
]


def _add_line(path: str, page, ui_server, company, ui, token: str, doc_id: str, item: dict) -> None:
    """Add `item` to the draft by `path`: from items, send to, scan, or the editor's line picker."""
    if path == "from_items":
        r = ui.post("/docs/from-items/add", data={"selected": [item["id"]], "target_id": doc_id})
        assert r.status_code == 204, r.text
    elif path == "send_to":
        r = ui.post("/api/items/send-to", data={"selected": [item["id"]], "send_to_doc_type": "invoice",
                                                "send_to_target": doc_id})
        assert r.status_code == 204, r.text
    elif path == "scan":
        r = company.post(f"/docs/{doc_id}/scan", json={"barcode": item["sku"]})
        assert r.status_code == 200 and r.json()["scanned"] == 1, r.text
    else:
        lines = len(company.get(f"/docs/{doc_id}").json().get("line_items") or [])
        ctx = page.context.browser.new_context(base_url=ui_server)
        try:
            ctx.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
            ctx.route("**/health/system", lambda route: route.fulfill(json={"overall": "ok"}))
            tab = ctx.new_page()
            tab.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
            _picker_add(tab, item["sku"])
            _saved(company, doc_id, lines + 1)
        finally:
            ctx.close()


def _each_line_at_100(company, doc_id: str) -> dict:
    """The draft with every line set to one unit at $100, its taxes kept as they are."""
    lines = company.get(f"/docs/{doc_id}").json()["line_items"]
    lines = [{**line, "quantity": 1, "unit_price": 100.0, "line_total": 100.0} for line in lines]
    r = company.patch(f"/docs/{doc_id}", json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text
    return company.get(f"/docs/{doc_id}").json()


@pytest.mark.parametrize("path", ["from_items", "send_to", "scan", "picker"])
def test_a_document_wide_levy_leaves_vat_on_a_line_added_next_to_it(page, api_server, ui_server, api, path):
    """A draft with a $100 line taxed VAT 7% and a document-wide 5% levy: a $100 item added to it
    still takes VAT 7%, so VAT is $14, the levy $10 and the total $224 (not $217)."""
    with _demo_company(api_server, ui_server, api, "TH") as (company, ui, token, items):
        r = company.post("/docs", json={
            "doc_type": "invoice", "status": "draft", "doc_taxes": [_LEVY],
            "line_items": [{"description": items[0]["name"], "entity_id": items[0]["id"], "quantity": 1,
                            "unit_price": 100.0, "taxes": [{**_VAT_WHOLE, "amount": 7.0}]}]})
        assert r.status_code in (200, 201), r.text
        doc_id = r.json()["id"]
        _add_line(path, page, ui_server, company, ui, token, doc_id, items[1])
        doc = _each_line_at_100(company, doc_id)
    assert [_codes(line) for line in doc["line_items"]] == [["VAT 7%"], ["VAT 7%"]]
    assert [tax["code"] for tax in doc["doc_taxes"]] == ["Levy 5%"]
    assert (float(doc["subtotal"]), float(doc["total"])) == (200.0, 224.0)


@pytest.mark.parametrize("path", ["from_items", "picker"])
def test_a_new_line_is_taxed_in_the_companys_calculation_order(page, api_server, ui_server, api, path):
    """VAT 5% (order 0) and a compound 10% levy (order 1), listed levy first: a $100 line is taxed
    VAT $5 then levy $10.50, total $115.50 (not $115)."""
    with _demo_company(api_server, ui_server, api, "TH", _COMPOUND_AFTER_VAT) as (company, ui, token, items):
        r = company.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": []})
        assert r.status_code in (200, 201), r.text
        doc_id = r.json()["id"]
        _add_line(path, page, ui_server, company, ui, token, doc_id, items[0])
        doc = _each_line_at_100(company, doc_id)
    assert sorted(_codes(doc["line_items"][0])) == ["Levy 10%", "VAT 5%"]
    assert (float(doc["subtotal"]), float(doc["total"])) == (100.0, 115.5)


@pytest.mark.parametrize(("removed", "line_codes"), [(False, ["SGST 9%"]), (True, ["CGST 9%", "SGST 9%"])])
def test_the_editor_leaves_off_only_a_default_the_document_charges(page, api_server, ui_server, api, removed,
                                                                   line_codes):
    """A company with CGST 9% and SGST 9% as defaults, on a draft charging CGST 9% as a whole: a
    line picked in the editor takes SGST 9% alone, and both once CGST is removed on the page."""
    cgst = {"code": "CGST 9%", "rate": 9.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}
    with _demo_company(api_server, ui_server, api, "IN", _INDIA_BEFORE) as (company, _, token, items):
        r = company.post("/docs", json={"doc_type": "invoice", "status": "draft", "doc_taxes": [cgst],
                                        "line_items": []})
        assert r.status_code in (200, 201), r.text
        doc_id = r.json()["id"]
        ctx = page.context.browser.new_context(base_url=ui_server)
        try:
            ctx.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
            ctx.route("**/health/system", lambda route: route.fulfill(json={"overall": "ok"}))
            tab = ctx.new_page()
            tab.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
            if removed:
                tab.wait_for_selector("#doc-taxes-state", state="attached", timeout=8000)
                tab.evaluate("celerpRemoveDocTax(0)")
            _picker_add(tab, items[0]["sku"])
            for _ in range(120):
                doc = _saved(company, doc_id, 1)
                if float(doc["subtotal"]) > 0:
                    break
                time.sleep(0.25)
        finally:
            ctx.close()
    assert sorted(_codes(doc["line_items"][0])) == line_codes
    assert [tax["code"] for tax in doc.get("doc_taxes") or []] == ([] if removed else ["CGST 9%"])
    _taxed_once(doc, 1.18)


def _store_doc_taxes(company_id: str, doc_id: str, doc_taxes: list, tax: float, total: float) -> None:
    """Write `doc_taxes` straight into the company's draft projection (a document id is unique only
    within its company): no write takes an entry of another shape any more, so this is how a value
    an older version stored reaches the editor."""
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            key = (company_id, doc_id)
            cur.execute("SELECT state FROM projections WHERE company_id = %s AND entity_id = %s", key)
            state = cur.fetchone()[0]
            state.update(doc_taxes=doc_taxes, tax=tax, total=total)
            cur.execute("UPDATE projections SET state = %s WHERE company_id = %s AND entity_id = %s",
                        (json.dumps(state), *key))
    finally:
        conn.close()


def test_the_editor_keeps_a_tax_it_cannot_read_until_it_is_removed(page, api_server, ui_server, api):
    """A $100 draft charging VAT 7% and a "Levy 5%" stored as plain text (tax 12, total 112): a
    quantity change is refused naming the levy and leaves the totals as stored; removing the levy
    on the page saves, and the draft totals 107."""
    with _demo_company(api_server, ui_server, api, "TH") as (company, _, token, _items):
        r = company.post("/docs", json={"doc_type": "invoice", "status": "draft", "doc_taxes": [_VAT_WHOLE],
                                        "line_items": [{"description": "Service", "quantity": 1,
                                                        "unit_price": 100.0}]})
        assert r.status_code in (200, 201), r.text
        doc_id = r.json()["id"]
        _store_doc_taxes(company.get("/companies/me").json()["id"], doc_id,
                         [{**_VAT_WHOLE, "amount": 7.0}, "Levy 5%"], 12.0, 112.0)
        ctx = page.context.browser.new_context(base_url=ui_server)
        try:
            ctx.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
            ctx.route("**/health/system", lambda route: route.fulfill(json={"overall": "ok"}))
            tab = ctx.new_page()
            tab.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
            levy = tab.locator('#doc-stored-tax-rows [data-doc-tax-index="1"]')
            assert "Levy 5%" in levy.text_content()
            qty = tab.locator('#line-body tr').first.locator('[data-name="quantity"]')
            qty.fill("2")
            qty.blur()
            tab.locator(".toast-container .toast--error", has_text="Levy 5%").wait_for(timeout=8000)
            doc = company.get(f"/docs/{doc_id}").json()
            assert (doc["line_items"][0]["quantity"], doc["tax"], doc["total"]) == (1, 12.0, 112.0), doc
            levy.locator("button").click()
            for _ in range(120):
                doc = company.get(f"/docs/{doc_id}").json()
                if len(doc.get("doc_taxes") or []) == 1:
                    break
                time.sleep(0.25)
        finally:
            ctx.close()
    assert [tax["code"] for tax in doc["doc_taxes"]] == ["VAT 7%"]
    assert (float(doc["tax"]), float(doc["total"])) == (7.0, 107.0)
