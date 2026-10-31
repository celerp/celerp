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
    preview = {"problems": [], "fixed_taken": takes, "totals_after": {"total": 300.0},
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
    rate, preview_rate = AsyncMock(return_value=answer), AsyncMock(return_value={**answer, "binding": "b-8"})
    with patch("ui.api_client.get_doc", new=get_doc), patch("ui.api_client.get_company", new=AsyncMock(return_value={})), \
            patch("ui.api_client.set_memo_tax_rate", new=rate), patch("ui.api_client.memo_tax_rate_preview", new=preview_rate):
        editor = await ui_client.get(f"/docs/{MEMO}/field/tax_rate/edit")
        assert editor.status_code == 200
        assert 'type="number"' in editor.text and t("btn.save") in editor.text
        assert "hx-trigger=\"blur" not in editor.text
        # Save waits for the preview of the rate typed: the form holds it, the preview span asks
        # with a newer request replacing an older one still on the way.
        assert 'data-confirms-preview="true"' in editor.text
        assert re.search(r'<button[^>]*type="submit"[^>]*disabled', editor.text)
        span = re.search(r'<span[^>]*memo-rate-preview[^>]*>', editor.text).group(0)
        assert 'data-preview="true"' in span and 'hx-sync="this:replace"' in span
        assert 'name="expected_version"' not in editor.text

        preview = await ui_client.post(f"/docs/{MEMO}/tax-rate/preview", data={"value": "8", "binding": "b-old"})
        assert "28.00" in preview.text
        assert re.findall(r'name="binding" value="([^"]*)"', preview.text) == ["b-8"]
        assert preview_rate.await_args.args[1:] == (MEMO, 8.0)
        assert preview_rate.await_count == 1 and rate.await_count == 0

        display = await ui_client.get(f"/docs/{MEMO}/field/tax_rate/display")
        assert display.status_code == 200
        assert preview_rate.await_count == 1 and rate.await_count == 0

        # A save sent before its preview answered carries no binding and is never written.
        early = await ui_client.patch(f"/docs/{MEMO}/field/tax_rate", data={"value": "8"})
        assert t("documents.memo_rate_wait_for_preview") in early.text and rate.await_count == 0

        saved = await ui_client.patch(f"/docs/{MEMO}/field/tax_rate", data={"value": "8", "binding": "b-8"})
        assert saved.status_code == 200 and "28.00" in saved.text
        assert rate.await_count == 1 and rate.await_args.args[1:] == (MEMO, 8.0, "b-8")
        assert t("documents.memo_tax_rate_set").split("{")[0].strip() in saved.headers.get("HX-Trigger", "")

    from ui.api_client import APIError
    refused = AsyncMock(side_effect=APIError(409, "This memo has no tax amount without a rate.",
                                                   {"message_key": "docs.memo_rate_no_unrated_tax"}))
    with patch("ui.api_client.get_doc", new=get_doc), patch("ui.api_client.get_company", new=AsyncMock(return_value={})), \
            patch("ui.api_client.set_memo_tax_rate", new=refused):
        r = await ui_client.patch(f"/docs/{MEMO}/field/tax_rate", data={"value": "9", "binding": "b-9"})
    assert 'cell-error' in r.text and refused.await_count == 1

    for doc in ({**issued, "doc_type": "invoice"}, _memo(tax_rate=7, tax=24.5)):
        assert "/tax-rate/preview" not in _html(doc)


def test_every_preview_then_confirm_form_shares_the_one_hold():
    """Every form that confirms a preview (Invoice selected, the memo's Rate cell) carries the
    shared hold's markers, and every page loads the one hold script once."""
    from fasthtml.common import Div, to_xml
    from ui.components.preview_confirm import PREVIEW_CONFIRM_JS
    from ui.components.shell import _shell_document

    panel = re.search(r'<form[^>]*id="draw-panel"[^>]*>', _html(_memo())).group(0)
    assert 'data-confirms-preview="true"' in panel
    preview = re.search(r'<div[^>]*id="draw-preview"[^>]*>', _html(_memo())).group(0)
    assert 'data-preview="true"' in preview and 'hx-sync="this:replace"' in preview
    page = to_xml(_shell_document(Div(), nav=Div()))
    assert page.count("form[data-confirms-preview]") == 1 and PREVIEW_CONFIRM_JS.strip()[:40] in page


def test_a_drawn_invoice_line_badge_names_the_memo_its_goods_are_out_on():
    """A lot invoiced from a memo stays out on that memo while the invoice holds it, so the
    invoice line's status badge names the memo, never the invoice the page already is."""
    from ui.routes.documents import _line_status_doc

    held_by = {"item:1": ("doc:inv-1", "PF-1")}
    drawn = {"item_id": "item:1", "drawn_from": {"memo_id": MEMO, "memo_number": "M-12"}}
    assert _line_status_doc(drawn, "item:1", held_by) == (MEMO, "M-12")
    assert _line_status_doc({"item_id": "item:1"}, "item:1", held_by) == ("doc:inv-1", "PF-1")


_PICKS = {"line_id_0": LINE, "qty_0": "1", "currency": "USD"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", "doc:pf-2"])
@pytest.mark.parametrize("problems", [[], [{"message_key": "docs.draw_target_not_draft", "message": "Not a draft."}]])
async def test_a_draw_preview_reads_fresh_and_carries_its_binding(ui_client, target, problems):
    """A preview never sends the binding the panel holds, and its answer, refused or not,
    carries the binding of this preview alone: the one a confirm sends back."""
    preview = AsyncMock(return_value={"problems": problems, "binding": "b-new", "totals_after": {"total": 10.0}})
    with patch("ui.api_client.invoice_lines_preview", new=preview):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines/preview",
                                 data={**_PICKS, "binding": "b-old", "target_doc_id": target})
    assert r.status_code == 200, r.text
    body = preview.await_args.args[2]
    assert "binding" not in body and body["target_doc_id"] == (target or None)
    assert re.findall(r'name="binding" value="([^"]*)"', r.text) == ["b-new"]


@pytest.mark.asyncio
async def test_a_failed_draw_preview_carries_no_binding(ui_client):
    from ui.api_client import APIError
    failed = AsyncMock(side_effect=APIError(404, "Not found.", {}))
    with patch("ui.api_client.invoice_lines_preview", new=failed):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines/preview", data={**_PICKS, "binding": "b-old"})
    assert 'name="binding"' not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", "doc:pf-1"])
async def test_a_draw_submit_sends_the_binding_of_the_preview_on_screen(ui_client, target):
    draw = AsyncMock(return_value={"target_doc_id": target or "doc:inv-9"})
    with patch("ui.api_client.invoice_lines", new=draw):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines", data={
            **_PICKS, "binding": "b-shown", "target_doc_id": target, "idempotency_key": "k-1"})
    assert r.status_code == 204, r.text
    sent = draw.await_args.args[2]
    assert sent["binding"] == "b-shown" and sent["target_doc_id"] == (target or None)
    assert sent["quantities"] == {LINE: 1.0}


@pytest.mark.asyncio
async def test_a_draw_submit_without_a_preview_is_refused_and_sends_nothing(ui_client):
    draw = AsyncMock(return_value={"target_doc_id": "doc:inv-9"})
    with patch("ui.api_client.invoice_lines", new=draw):
        r = await ui_client.post(f"/docs/{MEMO}/invoice-lines", data={**_PICKS, "idempotency_key": "k-1"})
    assert t("documents.draw_wait_for_preview") in r.headers.get("HX-Trigger", "")
    draw.assert_not_awaited()


def test_the_draw_confirm_waits_for_the_preview_of_the_current_selection():
    """Confirm starts disabled and only a preview answer carrying a binding enables it; a newer
    preview request replaces an older one in flight, so an older answer never lands last."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _draw_panel

    panel = to_xml(_draw_panel(MEMO, _memo(), [], {}))
    submit = re.search(r"<button[^>]*type=\"submit\"[^>]*>", panel).group(0)
    assert "disabled" in submit
    preview = re.search(r'<div[^>]*id="draw-preview"[^>]*>', panel).group(0)
    assert 'hx-sync="this:replace"' in preview
