# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End to end: a line added to a document in a new company with demo items is taxed once.

Real API and UI servers on Postgres: the company takes its tax regime from its default
location's country, the demo items are seeded the way setup seeds them, and lines are added
through every path a user has: "Send to" from inventory, "Add to document", a scan and the
document editor's item picker.

- A line takes every default tax of the company, never only the first: a company set up before
  IGST became the India default holds CGST 9% and SGST 9% both as defaults, 18% in all.
- A draft taxed as a whole (document taxes) gives a new line no tax of its own, so the
  document tax is never charged twice.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager

import httpx
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
