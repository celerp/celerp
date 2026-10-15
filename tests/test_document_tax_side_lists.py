# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A supplier document starts from and offers the company's purchasing taxes; a sale its
sales taxes. Scanning onto a document and opening it in the editor read the same list."""
from __future__ import annotations

import json
import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

_SALES = [{"name": "Output VAT", "rate": 7.0, "tax_type": "both", "is_default": True}]
_PURCHASING = [{"name": "Input VAT", "rate": 10.0, "tax_type": "both", "is_default": True}]


async def _company(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Two Lists Co", "email": f"tl-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": _SALES})).status_code == 200
    r = await client.patch("/companies/me/purchasing-taxes", headers=h, json={"taxes": _PURCHASING})
    assert r.status_code == 200, r.text
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": "TAX-SKU", "name": "Taxed", "sell_by": "piece", "quantity": 5,
        "retail_price": 20, "cost_price": 8})
    assert r.status_code == 200, r.text
    return h


async def _scanned_taxes(client, h, doc_type: str) -> list[dict]:
    r = await client.post("/docs", headers=h, json={"doc_type": doc_type, "line_items": []})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/scan", headers=h,
                          json={"barcode": "TAX-SKU", "run_key": f"run-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 1, r.text
    lines = (await client.get(f"/docs/{doc_id}", headers=h)).json()["line_items"]
    assert len(lines) == 1
    return [{"code": x["code"], "rate": x["rate"]} for x in lines[0].get("taxes") or []]


@pytest.mark.asyncio
async def test_a_scan_onto_a_purchase_order_takes_the_purchasing_default(client):
    h = await _company(client)
    assert await _scanned_taxes(client, h, "purchase_order") == [{"code": "Input VAT", "rate": 10.0}]


@pytest.mark.asyncio
async def test_a_scan_onto_an_invoice_keeps_the_sales_default(client):
    h = await _company(client)
    assert await _scanned_taxes(client, h, "invoice") == [{"code": "Output VAT", "rate": 7.0}]


@pytest.mark.asyncio
async def test_purchasing_taxes_never_saved_read_as_the_sales_list(client):
    r = await client.post("/auth/register", json={
        "company_name": "One List Co", "email": f"ol-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": _SALES})).status_code == 200
    assert [x["name"] for x in (await client.get("/companies/me/purchasing-taxes", headers=h)).json()] == ["Output VAT"]


def _offered(html: str) -> list[dict]:
    return json.loads(re.search(r"window\._CELERP_TAXES = (\[.*?\]);", html).group(1))


async def _editor(ui_client, doc_type: str) -> str:
    doc = {"id": "d:1", "entity_id": "d:1", "doc_type": doc_type, "status": "draft", "ref_id": "D-1",
           "line_items": [{"line_id": "l1", "description": "Widget", "quantity": 1, "unit_price": 5}]}
    with (
        patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
        patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_SALES)),
        patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(return_value=_PURCHASING)),
    ):
        r = await ui_client.get("/docs/d:1", cookies=_authed())
    assert r.status_code == 200, r.text
    return r.text


@pytest.mark.asyncio
async def test_the_purchase_order_editor_offers_purchasing_taxes(ui_client):
    html = await _editor(ui_client, "purchase_order")
    offered = _offered(html)
    assert [x["name"] for x in offered] == ["Input VAT"]
    assert "Input VAT|10.0" in html and "Output VAT" not in html


@pytest.mark.asyncio
async def test_the_invoice_editor_offers_sales_taxes(ui_client):
    html = await _editor(ui_client, "invoice")
    offered = _offered(html)
    assert [x["name"] for x in offered] == ["Output VAT"]
    assert "Output VAT|7.0" in html and "Input VAT" not in html


@pytest.mark.asyncio
async def test_a_tax_both_lists_name_takes_its_side_from_the_documents_own_list(client):
    """GST is sales-only on the sales list and both-sided on the purchasing list: a bill
    may carry it, an invoice may too, and a purchase-only tax stays off the invoice."""
    r = await client.post("/auth/register", json={
        "company_name": "Shared Name Co", "email": f"sn-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    sales = [{"name": "GST", "rate": 5.0, "tax_type": "sales"}]
    purchasing = [{"name": "GST", "rate": 5.0, "tax_type": "both"}, {"name": "WHT", "rate": 3.0, "tax_type": "purchase"}]
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": sales})).status_code == 200
    assert (await client.patch("/companies/me/purchasing-taxes", headers=h, json={"taxes": purchasing})).status_code == 200

    def body(doc_type: str, code: str, rate: float) -> dict:
        return {"doc_type": doc_type, "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100,
                                                       "taxes": [{"code": code, "rate": rate}]}]}

    assert (await client.post("/docs", headers=h, json=body("bill", "GST", 5.0))).status_code == 200
    assert (await client.post("/docs", headers=h, json=body("invoice", "GST", 5.0))).status_code == 200
    r = await client.post("/docs", headers=h, json=body("invoice", "WHT", 3.0))
    assert r.status_code == 422 and "WHT" in r.text, r.text
