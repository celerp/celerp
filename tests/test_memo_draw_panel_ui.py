# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The memo page around invoicing part of a memo: the panel says which fixed amounts the next
invoice takes and what stays on the memo, memo lines say where their goods went, the Rate cell
previews a tax rate before saving it, and Back to memo names a memo it reopens."""

from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.i18n import t

MEMO = "doc:memo-1"
LINE = "11111111-1111-4111-8111-111111111111"


def _memo(**extra) -> dict:
    return {"entity_id": MEMO, "id": MEMO, "doc_type": "memo", "status": "final", "ref_id": "M-12", "version": 3,
            "currency": "USD", "amount_paid": 0, "subtotal": 350.0, "tax": 35.0, "total": 385.0,
            "line_items": [{"line_id": LINE, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1",
                            "quantity": 2, "sell_by": "piece", "unit_price": 175.0}], **extra}


def _html(doc: dict, **kw) -> str:
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_detail
    return to_xml(_doc_detail(doc, item_status_map={"item:1": "memo_out"}, **kw))


@pytest.fixture
def ui_client():
    from ui.app import app as ui_app
    client = AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False)
    client.cookies.update({"celerp_token": make_test_token(role="owner")})
    return client


@pytest.mark.parametrize(("left", "takes", "names"), [
    ({"shipping": 50.0, "discount": 100.0, "tax": 0}, {"shipping": 50.0, "discount": 100.0, "tax": 0},
     ("50.00", "100.00")),
    ({"shipping": 0, "discount": 40.0, "tax": 0}, {"shipping": 0, "discount": 40.0, "tax": 0}, ("40.00",)),
    ({"shipping": 0, "discount": 0, "tax": 0}, None, ()),
])
def test_memo_draw_panel_names_fixed_amounts_left(left, takes, names):
    from ui.routes.documents import _draw_panel_preview

    memo = _memo(memo_fixed_amounts={**left, "takes": [], "still_on_memo": 350.0})
    html = _html(memo)
    assert 'id="draw-panel"' in html and "invoice-lines" in html

    from fasthtml.common import to_xml
    preview = {"fixed_taken": takes, "total": 300.0, "memo_fixed_left": {"shipping": 0, "discount": 0, "tax": 0}}
    panel = to_xml(_draw_panel_preview(memo, preview))
    for amount in names:
        assert amount in panel
    if takes is None:
        assert t("documents.draw_takes_fixed").split("{")[0] not in panel
    else:
        assert "300.00" in panel


def test_memo_line_labels_show_invoiced_and_sold():
    memo = _memo(line_items=[
        {"line_id": LINE, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1", "quantity": 3, "sell_by": "piece",
         "unit_price": 10.0, "shipped_label": "Invoiced (draft) PF-0042", "invoiced_on": [
             {"invoice_id": "doc:pf-1", "doc_number": "PF-0042", "quantity": 1, "status": "draft"},
             {"invoice_id": "doc:inv-1", "doc_number": "INV-0007", "quantity": 1, "status": "final"}]}])
    html = _html(memo)
    assert t("documents.line_invoiced_draft").split("{")[0].strip() in html
    assert t("documents.line_sold_on").split("{")[0].strip() in html
    assert 'href="/docs/doc:pf-1"' in html and 'href="/docs/doc:inv-1"' in html
    assert "On Memo" in html or t("doc.on_memo") in html


@pytest.mark.asyncio
async def test_back_to_memo_response_names_reopened_memo(ui_client):
    answer = {"returned": 1, "reopened_memos": [{"memo_id": MEMO, "doc_number": "M-12"}]}
    with patch("ui.api_client.back_to_memo", new=AsyncMock(return_value=answer)):
        r = await ui_client.post("/docs/doc:pf-1/back-to-memo", data={"line_ids": LINE})
    assert r.status_code == 204, r.text
    toast = r.headers.get("HX-Trigger", "")
    assert "M-12" in toast and f"/docs/{MEMO}" in toast and '"persist": true' in toast.replace("True", "true")


@pytest.mark.asyncio
async def test_memo_rate_cell_ui(ui_client):
    issued = _memo(memo_fixed_amounts={"shipping": 0, "discount": 0, "tax": 35.0, "takes": [], "still_on_memo": 385.0})
    html = _html(issued)
    assert "/docs/doc:memo-1/field/tax_rate/edit" in html and t("click_to_edit") in html
    assert "--" in html

    get_doc = AsyncMock(return_value=issued)
    rate = AsyncMock(return_value={"tax": 28.0, "total": 378.0, "tax_rate": 8})
    with patch("ui.api_client.get_doc", new=get_doc), patch("ui.api_client.get_company", new=AsyncMock(return_value={})), \
            patch("ui.api_client.set_memo_tax_rate", new=rate):
        editor = await ui_client.get(f"/docs/{MEMO}/field/tax_rate/edit")
        assert editor.status_code == 200
        assert 'type="number"' in editor.text and t("btn.save") in editor.text
        assert "hx-trigger=\"blur" not in editor.text

        preview = await ui_client.post(f"/docs/{MEMO}/tax-rate/preview", data={"value": "8"})
        assert "28.00" in preview.text
        assert rate.await_args.kwargs.get("dry_run") is True

        display = await ui_client.get(f"/docs/{MEMO}/field/tax_rate/display")
        assert display.status_code == 200
        assert rate.await_count == 1

        saved = await ui_client.patch(f"/docs/{MEMO}/field/tax_rate", data={"value": "8"})
        assert saved.status_code == 200 and "28.00" in saved.text
        assert rate.await_args.kwargs.get("dry_run") in (None, False)
        assert t("documents.memo_tax_rate_set").split("{")[0].strip() in saved.headers.get("HX-Trigger", "")

    from ui.api_client import APIError
    refused = AsyncMock(side_effect=APIError(409, "This memo has no tax amount without a rate.",
                                                   {"message_key": "docs.memo_rate_no_unrated_tax"}))
    with patch("ui.api_client.get_doc", new=get_doc), patch("ui.api_client.get_company", new=AsyncMock(return_value={})), \
            patch("ui.api_client.set_memo_tax_rate", new=refused):
        r = await ui_client.patch(f"/docs/{MEMO}/field/tax_rate", data={"value": "9"})
    assert 'cell-error' in r.text

    for doc in ({**issued, "doc_type": "invoice"}, _memo(tax_rate=7, tax=24.5)):
        assert "/tax-rate/preview" not in _html(doc)
