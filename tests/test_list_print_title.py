# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A printed list carries its own type's name, never "LIST" (SAL-006): the title comes from the
list type while doc_type stays "list" for the layout branches. A quotation's customer is "Quote
to", as on its page; a list type without a customer prints no customer block."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from celerp.output.doc_print import LIST_PARTY_LABELS, LIST_PRINT_TITLES, render_doc_print_html
from celerp.services.list_behavior import LIST_BEHAVIOR
from test_helpers import make_test_token

ROOT = Path(__file__).resolve().parents[1]
_CUSTOMER = {"contact_name": "Acme Trading", "contact_address": "1 Main Road", "contact_email": "buyer@acme.test"}
_TITLES = {"quotation": "Quotation", "transfer": "Transfer", "audit": "Audit", "writeoff": "Write-off"}


def _list(list_type: str | None, **extra) -> dict:
    doc = {"doc_type": "list", "entity_id": "list:L-001", "ref_id": "L-001", "issue_date": "2026-10-29",
           "currency": "USD", "line_items": [{"description": "Ruby", "quantity": 1, "unit_price": 10.0}],
           **_CUSTOMER, **extra}
    if list_type is not None:
        doc["list_type"] = list_type
    return doc


def _title(html: str) -> str:
    return re.search(r'<p class="dp-doc-title">([^<]*)</p>', html).group(1)


def _party_labels(html: str) -> list[str]:
    return re.findall(r'<p class="dp-party-label">([^<]*)</p>', html)


@pytest.mark.parametrize("list_type, title", sorted(_TITLES.items()))
def test_each_list_type_prints_its_own_title(list_type, title):
    html = render_doc_print_html(_list(list_type))
    assert _title(html) == title
    assert f"<title>{title} L-001</title>" in html
    assert "<title>List " not in html and ">List<" not in html


def test_every_list_type_has_a_print_title():
    assert LIST_PRINT_TITLES == {lt: b.label for lt, b in LIST_BEHAVIOR.items()}


def test_a_list_without_a_type_prints_as_a_quotation():
    html = render_doc_print_html(_list(None, valid_until="2026-11-30"))
    assert _title(html) == "Quotation"
    assert _party_labels(html)[0] == "Quote to"
    assert "Valid until: </strong>2026-11-30" in html


def test_a_quotation_names_its_customer_quote_to():
    html = render_doc_print_html(_list("quotation"))
    assert _party_labels(html) == ["Quote to"]
    assert "Acme Trading" in html and "Bill To" not in html


def test_the_print_party_label_is_the_detail_pages_english_label():
    english = json.loads((ROOT / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert LIST_PARTY_LABELS["quotation"] == english["documents.party_quote_to"]


@pytest.mark.parametrize("list_type", ["transfer", "audit", "writeoff"])
def test_a_list_type_without_a_customer_prints_no_customer_block(list_type):
    html = render_doc_print_html(_list(list_type))
    assert _party_labels(html) == []
    assert "Acme Trading" not in html and "Bill To" not in html


def test_the_old_quote_type_prints_as_a_quotation():
    html = render_doc_print_html(_list("quote"))
    assert _title(html) == "Quotation" and _party_labels(html) == ["Quote to"]
    assert "Acme Trading" in html


def test_an_unknown_list_type_keeps_bill_to():
    html = render_doc_print_html(_list("sales_order"))
    assert _party_labels(html) == ["Bill To"] and "Acme Trading" in html


def test_a_shipping_document_keeps_its_paper_names():
    html = render_doc_print_html(_list("shipping_doc"))
    assert _title(html) == "Delivery Note"
    html = render_doc_print_html(_list("shipping_doc"), layout="commercial_invoice")
    assert _title(html) == "Commercial Invoice"


def test_documents_still_print_their_doc_type_and_bill_to():
    html = render_doc_print_html({**_list(None), "doc_type": "invoice", "list_type": "quotation"})
    assert _title(html) == "Invoice"
    assert _party_labels(html)[0] == "Bill To"


# ── The UI print route ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("list_type, title", sorted(_TITLES.items()))
async def test_list_print_route_prints_the_list_type(ui_client, list_type, title):
    stored = {k: v for k, v in _list(list_type).items() if k != "doc_type"}  # the API sends no doc_type
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.get_list", new=AsyncMock(return_value=stored)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "Test Corp", "settings": {}})):
        r = await ui_client.get("/lists/list:L-001/print")
    assert r.status_code == 200, r.text
    assert _title(r.text) == title
    assert ("Quote to" in _party_labels(r.text)) is (list_type == "quotation")
