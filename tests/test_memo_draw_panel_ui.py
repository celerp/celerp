# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The memo page around invoicing part of a memo: the panel says which fixed amounts the next
invoice takes and what stays on the memo, memo lines say where their goods went, the Rate cell
previews a tax rate before saving it, and Back to memo names a memo it reopens."""

from __future__ import annotations

import os
import re
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.i18n import t

MEMO = "doc:memo-1"
LINE = "11111111-1111-4111-8111-111111111111"


def _memo(**extra) -> dict:
    return {"id": MEMO, "doc_type": "memo", "status": "final", "ref_id": "M-12", "version": 3,
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


@pytest.mark.parametrize(("status", "still", "shown"), [("final", 120.0, "120.00"), ("closed", None, None)])
def test_memo_totals_say_what_is_still_on_an_open_memo_only(status, still, shown):
    """The totals name what is still on the memo while a draw can come from it; a closed memo
    has nothing left to bill, so the row is left out rather than shown as zero."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _memo_take_rows

    take = {"invoice_id": "doc:inv-1", "doc_number": "INV-1", "shipping": 40.0, "discount": 0, "tax": 0}
    memo = _memo(status=status, memo_fixed_amounts={"shipping": 0, "discount": 0, "tax": 0, "takes": [take],
                                                    "still_on_memo": still})
    rows = to_xml(_memo_take_rows(memo, "USD"))
    assert "INV-1" in rows
    assert (t("documents.memo_still_on_memo") in rows) == (shown is not None)
    if shown:
        assert shown in rows


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
    preview = {"problems": [], "memo_version": 3, "fixed_taken": takes, "totals_after": {"total": 300.0},
               "left_on_memo": {"fixed": {"still_on_memo": 50.0}}}
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
    answer = {"removed": [LINE], "reopened_memos": [{"id": MEMO, "doc_number": "M-12"}]}
    with patch("ui.api_client.back_to_memo", new=AsyncMock(return_value=answer)):
        r = await ui_client.post("/docs/doc:pf-1/back-to-memo", data={"line_id": LINE, "idempotency_key": "k-1"})
    assert r.status_code == 204, r.text
    toast = r.headers.get("HX-Trigger", "")
    assert "M-12" in toast and f"/docs/{MEMO}" in toast and '"persist": true' in toast.replace("True", "true")


@pytest.mark.asyncio
async def test_memo_rate_cell_ui(ui_client):
    issued = _memo(memo_fixed_amounts={"shipping": 0, "discount": 0, "tax": 35.0, "takes": [], "still_on_memo": 385.0})
    html = _html(issued)
    assert "/docs/doc:memo-1/field/tax_rate/edit" in html and t("documents.click_to_edit") in html
    assert "--" in html

    get_doc = AsyncMock(return_value=issued)
    answer = {"tax": 28.0, "total": 378.0, "tax_rate": 8, "previous_tax": 35.0, "version": 4}
    rate, preview_rate = AsyncMock(return_value=answer), AsyncMock(return_value=answer)
    with patch("ui.api_client.get_doc", new=get_doc), patch("ui.api_client.get_company", new=AsyncMock(return_value={})), \
            patch("ui.api_client.set_memo_tax_rate", new=rate), patch("ui.api_client.memo_tax_rate_preview", new=preview_rate):
        editor = await ui_client.get(f"/docs/{MEMO}/field/tax_rate/edit")
        assert editor.status_code == 200
        assert 'type="number"' in editor.text and t("btn.save") in editor.text
        assert "hx-trigger=\"blur" not in editor.text

        preview = await ui_client.post(f"/docs/{MEMO}/tax-rate/preview", data={"value": "8"})
        assert "28.00" in preview.text
        assert preview_rate.await_count == 1 and rate.await_count == 0

        display = await ui_client.get(f"/docs/{MEMO}/field/tax_rate/display")
        assert display.status_code == 200
        assert preview_rate.await_count == 1 and rate.await_count == 0

        saved = await ui_client.patch(f"/docs/{MEMO}/field/tax_rate", data={"value": "8"})
        assert saved.status_code == 200 and "28.00" in saved.text
        assert rate.await_count == 1
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


def test_a_drawn_invoice_line_badge_names_the_memo_its_goods_are_out_on():
    """A lot invoiced from a memo stays out on that memo while the invoice holds it, so the
    invoice line's status badge names the memo, never the invoice the page already is."""
    from ui.routes.documents import _line_status_doc

    held_by = {"item:1": ("doc:inv-1", "PF-1")}
    drawn = {"item_id": "item:1", "drawn_from": {"memo_id": MEMO, "memo_number": "M-12"}}
    assert _line_status_doc(drawn, "item:1", held_by) == (MEMO, "M-12")
    assert _line_status_doc({"item_id": "item:1"}, "item:1", held_by) == ("doc:inv-1", "PF-1")


_PICKS = {"line_id_0": LINE, "qty_0": "1", "currency": "USD"}
_HELD = {"expected_memo": "3", "expected_target": "5", "expected_target_id": "doc:pf-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", "doc:pf-2"])
async def test_a_draw_preview_reads_fresh_whatever_versions_the_panel_holds(ui_client, target):
    """Choosing another invoice re-previews; the versions an earlier preview read are never
    sent with it, so they cannot make the new preview stale."""
    preview = AsyncMock(return_value={"problems": [], "memo_version": 3, "target": {"id": target or None},
                                      "target_version": 7 if target else None, "totals_after": {"total": 10.0}})
    with patch("ui.api_client.invoice_lines_preview", new=preview):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines/preview", data={**_PICKS, **_HELD, "target_doc_id": target})
    assert r.status_code == 200, r.text
    body = preview.await_args.args[2]
    assert "expected_versions" not in body and body["target_doc_id"] == (target or None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("held", "sent"), [
    (_HELD, {"memo": 3, "target": 5, "target_id": "doc:pf-1"}),
    ({"expected_memo": "3"}, {"memo": 3, "target": None, "target_id": None}),
])
async def test_a_draw_submit_sends_the_versions_bound_to_the_invoice_they_were_read_for(ui_client, held, sent):
    draw = AsyncMock(return_value={"target_doc_id": "doc:pf-1"})
    with patch("ui.api_client.invoice_lines", new=draw):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines",
                                 data={**_PICKS, **held, "target_doc_id": "doc:pf-1", "idempotency_key": "k-1"})
    assert r.status_code == 204, r.text
    assert draw.await_args.args[2]["expected_versions"] == sent


@pytest.mark.parametrize(("target", "hidden"), [
    ({"id": "doc:pf-1"}, {"expected_memo": "3", "expected_target": "5", "expected_target_id": "doc:pf-1"}),
    ({"id": None}, {"expected_memo": "3"}),
])
def test_a_draw_preview_carries_the_invoice_its_versions_were_read_for(target, hidden):
    from fasthtml.common import to_xml
    from ui.routes.documents import _draw_panel_preview

    preview = {"problems": [], "memo_version": 3, "target": target,
               "target_version": 5 if target["id"] else None, "totals_after": {"total": 10.0}}
    panel = to_xml(_draw_panel_preview(_memo(), preview))
    found = dict(re.findall(r'name="(expected_\w+)" value="([^"]*)"', panel))
    assert found == hidden
