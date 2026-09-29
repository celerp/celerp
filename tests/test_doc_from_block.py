# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The document From block shows the seller's website like its other contact rows: labelled,
click-to-edit, "--" when empty, on every document type that renders the shared block."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from celerp.output.document_context import prepare_document_output
from ui.i18n import t
from ui.routes.documents import _doc_detail

from test_helpers import make_test_token


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        yield c


def _doc(doc_type: str, **fields) -> dict:
    base = {"entity_id": "doc:w1", "id": "doc:w1", "doc_type": doc_type, "status": "draft", "line_items": [],
            "company_name": "Seller Co", "company_email": "sales@seller.example", "company_phone": "+66 2 555 0100"}
    if doc_type == "list":
        base |= {"entity_id": "list:w1", "id": "list:w1", "list_type": "quotation"}
    return base | fields


def _from_block(html: str) -> str:
    start = html.index(t("page.from"))
    return html[start:html.index(t("page.ship_to"), start)]


def _website_cell(block: str) -> re.Match | None:
    return re.search(r'<div[^>]*hx-get="/(?:docs|lists)/[^"]+/field/company_website/edit"[^>]*>([^<]*)</div>', block)


@pytest.mark.parametrize("doc_type", ["invoice", "quotation", "memo", "bill", "purchase_order", "list"])
def test_from_block_shows_the_website_on_every_doc_type(doc_type):
    block = _from_block(to_xml(_doc_detail(_doc(doc_type, company_website="https://seller.example"))))
    assert t("doc.website") in block
    cell = _website_cell(block)
    assert cell and cell.group(1).strip() == "https://seller.example", block
    assert 'class="editable-cell"' in cell.group(0)


def test_empty_website_shows_the_editable_placeholder():
    cell = _website_cell(_from_block(to_xml(_doc_detail(_doc("invoice")))))
    assert cell and cell.group(1).strip() == "--"


@pytest.mark.asyncio
async def test_website_edits_through_the_document_field_route(ui_client):
    doc = _doc("invoice", company_website="https://old.example")
    updated = doc | {"company_website": "https://new.example"}
    patch_doc = AsyncMock(return_value=updated)
    with patch("ui.api_client.patch_doc", new=patch_doc), \
         patch("ui.api_client.get_doc", new=AsyncMock(return_value=updated)):
        r = await ui_client.patch("/docs/doc:w1/field/company_website", data={"value": "https://new.example"}, cookies={"celerp_token": make_test_token(role="owner")})
    assert r.status_code == 200
    assert patch_doc.await_args.args[2] == {"company_website": "https://new.example"}
    assert b"https://new.example" in r.content


def test_document_identity_carries_all_five_contact_values():
    out = prepare_document_output({}, company={"name": "Seller Co"}, self_contact={
        "company_name": "Seller Co", "billing_address": "1 Road", "tax_id": "TX1",
        "phone": "+66 2 555 0100", "email": "sales@seller.example", "website": "https://seller.example",
    })
    assert (out["company_address"], out["company_tax_id"], out["company_phone"], out["company_email"], out["company_website"]) == (
        "1 Road", "TX1", "+66 2 555 0100", "sales@seller.example", "https://seller.example")
