# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The line table names every line by its line id and never disables a line's checkbox.

A line with no inventory item can be ticked and unticked like any other; the server skips
it in a stock action and the toast names it. Every line action sends line ids only, a draft
or List save hands the page the ids of the lines it saved, and the message catalogs carry
the new skip wording and none of the wording it replaced."""
from __future__ import annotations

import inspect
import json
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

_L0 = "11111111-1111-4111-8111-111111111111"
_L1 = "22222222-2222-4222-8222-222222222222"
_STOCK = {"line_id": _L0, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1", "quantity": 1, "unit_price": 10}
_GIA = {"line_id": _L1, "description": "GIA Certificate", "quantity": 1, "unit_price": 150}
_NOTICE = [{"message": 'Line 2 "GIA Certificate" was left as it is.', "message_key": "documents.lines_skipped_one",
            "params": {"lines": '2 "GIA Certificate"'}}]
_SKIPPED = [{"line": 2, "name": "GIA Certificate", "kind": "no_item", "line_id": _L1}]
_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _page(lines: list[dict], doc_type: str = "invoice", status: str = "final") -> str:
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:1", "doc_type": doc_type, "status": status, "ref_id": "I-1", "line_items": lines}
    return to_xml(_doc_detail(doc, item_status_map={"item:1": "available"}))


def _checkboxes(html: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"<input[^>]*class=\"[^\"]*li-select[^\"]*\"[^>]*>", html)]


def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


@pytest.mark.parametrize("doc_type,status", [("invoice", "final"), ("memo", "sent")])
def test_line_checkboxes_never_disabled(doc_type, status):
    boxes = _checkboxes(_page([dict(_STOCK), dict(_GIA)], doc_type, status))
    assert len(boxes) == 2, boxes
    assert not [b for b in boxes if re.search(r"\sdisabled\b", b)], boxes


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["reopened", "moved", "done"])
async def test_line_action_toast_names_skipped_lines_on_every_branch(ui_client, branch):
    from ui.i18n import refusal_text, t
    answer = {"skipped": _SKIPPED, "skipped_notice": _NOTICE}
    if branch == "reopened":
        answer["reopened_memos"] = [{"id": "doc:M-1", "doc_number": "M-1"}]
    elif branch == "moved":
        answer["taken_from"] = [{"sku": "S-1", "doc_number": "INV-9", "doc_id": "doc:INV-9"}]
    with patch("ui.api_client.fulfill_lines", new=AsyncMock(return_value=answer)):
        r = await ui_client.post("/docs/doc:1/fulfill-lines", data={"line_id": [_L0, _L1], "idempotency_key": "k"})
    assert r.status_code == 204, r.text
    toast = _toast(r)
    skipped = refusal_text(_NOTICE)
    assert toast["message"].count(skipped) == 1 and toast["message"].endswith(skipped), toast
    assert toast.get("persist") is True
    if branch == "done":
        assert toast["message"].startswith(t("documents.lines_shipped_one", n=1)), toast


# Each page proxy: (path, form, api_client function name).
_PROXIES = [
    ("/docs/doc:1/fulfill-lines", {}, "fulfill_lines"),
    ("/docs/doc:1/reserve-lines", {"new_status": "reserved"}, "reserve_lines"),
    ("/lists/list:1/reserve-lines", {"new_status": "reserved"}, "reserve_lines"),
    ("/docs/doc:1/set-available", {}, "set_lines_available"),
    ("/lists/list:1/set-available", {}, "set_lines_available"),
    ("/docs/doc:1/back-to-memo", {}, "back_to_memo"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,form,name", _PROXIES)
async def test_proxies_send_line_ids_only(ui_client, path, form, name):
    import ui.api_client as api
    assert "line_entity_ids" not in inspect.signature(getattr(api, name)).parameters
    sent = AsyncMock(return_value={})
    with patch(f"ui.api_client.{name}", new=sent):
        r = await ui_client.post(path, data={"line_id": [_L0, ""], "selected": ["item:1"],
                                             "idempotency_key": "k", **form})
    assert r.status_code == 204, r.text
    args, kwargs = sent.await_args
    assert kwargs["line_ids"] == [_L0, ""] and "line_entity_ids" not in kwargs
    inspect.signature(getattr(api, name)).bind(*args, **kwargs)


@pytest.mark.asyncio
async def test_draft_save_returns_line_ids(ui_client):
    doc = {"doc_type": "invoice", "status": "draft", "currency": "USD", "line_items": [dict(_STOCK), dict(_GIA)]}
    with patch("ui.api_client.patch_doc", new=AsyncMock(return_value={"version": 3})), \
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
        r = await ui_client.post("/docs/doc:1/lines", json={"line_items": [dict(_STOCK), dict(_GIA)], "expected_version": 2})
    assert r.status_code == 200, r.text
    assert r.json()["line_ids"] == [_L0, _L1]

    lst = {"list_type": "quotation", "currency": "USD", "line_items": [{"line_id": "a"}, dict(_STOCK), dict(_GIA)]}
    with patch("ui.api_client.patch_list_line_page", new=AsyncMock(return_value={"version": 5})), \
            patch("ui.api_client.get_list", new=AsyncMock(return_value=lst)):
        r = await ui_client.post("/lists/list:1/lines", json={
            "line_items": [dict(_STOCK), dict(_GIA)], "offset": 1, "original_count": 2, "expected_version": 4})
    assert r.status_code == 200, r.text
    assert r.json()["line_ids"] == [_L0, _L1]


@pytest.mark.asyncio
async def test_legacy_document_rows_carry_line_ids_and_ship_by_line_id(client, session):
    """An invoice stored before line ids existed, once backfilled, names every row by its
    derived id, and Set as shipped by those ids ships its stock lines."""
    from celerp.services.document_lines import derived_line_id
    from line_actions_support import lot
    from test_helpers import company_auth
    from test_line_ids_by_construction import _backfill, _make_legacy
    from ui.routes.documents import _doc_detail
    a = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    h = a["headers"]
    stone = await lot(client, h, "LEG-1", 1)
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"item_id": stone, "sku": "LEG-1", "description": "LEG-1", "quantity": 1, "unit_price": 10, "sell_by": "piece"},
        {"description": "GIA Certificate", "quantity": 1, "unit_price": 150, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    d = r.json()["id"]
    assert (await client.post(f"/docs/{d}/finalize", headers=h)).status_code == 200
    await _make_legacy(session, a["company_id"], d)
    await _backfill(session, a["company_id"], d)
    doc = (await client.get(f"/docs/{d}", headers=h)).json()
    ids = [derived_line_id(d, 0), derived_line_id(d, 1)]
    html = to_xml(_doc_detail(doc, item_status_map={stone: "available"}))
    assert [re.search(r'data-line-id="([^"]*)"', b).group(1) for b in _checkboxes(html)] == ids
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": ids, "idempotency_key": "legacy"})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{stone}", headers=h)).json()["status"] == "sold"


def test_removed_line_keys_absent_from_catalogs():
    removed = {"lines.no_item", "lines.not_stock", "documents.no_inventory_items_status"}
    added = {f"{base}_{n}" for base in ("lines.no_stock", "documents.lines_skipped", "lines.service_no_stock",
                                        "documents.service_lines_skipped", "documents.labels_skipped")
             for n in ("one", "many")}
    locales = sorted(_LOCALES.glob("*.json"))
    assert {p.stem for p in locales} == {"am", "ar", "de", "en", "es", "fr", "id", "it", "ja", "pt", "th", "vi"}
    for path in locales:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        assert not removed & set(catalog), (path.name, removed & set(catalog))
        assert added <= set(catalog), (path.name, added - set(catalog))
        for key in added:
            assert "{lines}" in catalog[key], (path.name, key)


def test_draw_panel_row_of_a_line_with_no_item_says_it_stays_on_the_memo():
    """Invoice selected leaves a line with no item on the memo, so its row in the panel says
    that, as the preview's note does, and never "Not shipped", which points at shipping it."""
    from ui.i18n import t
    from ui.routes.documents import _draw_panel
    lines = [{**_STOCK, "shipped_label": "Not shipped"}, {**_GIA, "shipped_label": "Not shipped"}]
    html = to_xml(_draw_panel("doc:m", {"doc_type": "memo", "line_items": lines}, [], {}))
    rows = re.findall(r'<fieldset[^>]*data-line-index="(\d)"[^>]*>(.*?)</fieldset>', html, re.S)
    said = {i: body for i, body in rows}
    assert t("documents.line_label_not_shipped") in said["0"]
    assert t("documents.draw_row_stays_on_memo") in said["1"]
    assert t("documents.line_label_not_shipped") not in said["1"]


@pytest.mark.asyncio
async def test_save_hands_back_the_ids_it_wrote_not_a_later_read(ui_client):
    """A writer that commits between the save and the read that follows it, or a read that
    fails, never puts other ids on the page's rows: the ids handed back are the ones sent."""
    other = {"doc_type": "invoice", "status": "draft", "currency": "USD",
             "line_items": [dict(_GIA), dict(_STOCK)]}
    sent = [{"line_id": _L0, "sku": "S-1"}, {"line_id": _L1, "description": "GIA Certificate"}]
    with patch("ui.api_client.patch_doc", new=AsyncMock(return_value={"version": 3})), \
            patch("ui.api_client.get_doc", new=AsyncMock(return_value=other)):
        r = await ui_client.post("/docs/doc:1/lines", json={"line_items": sent, "expected_version": 2})
    assert r.json()["line_ids"] == [_L0, _L1]
    from ui.api_client import APIError
    with patch("ui.api_client.patch_doc", new=AsyncMock(return_value={"version": 3})), \
            patch("ui.api_client.get_doc", new=AsyncMock(side_effect=APIError(500, "down"))):
        r = await ui_client.post("/docs/doc:1/lines", json={"line_items": sent, "expected_version": 2})
    assert r.json()["line_ids"] == [_L0, _L1] and r.json()["error"]

    with patch("ui.api_client.patch_list_line_page", new=AsyncMock(return_value={"version": 5})), \
            patch("ui.api_client.get_list", new=AsyncMock(side_effect=APIError(500, "down"))):
        r = await ui_client.post("/lists/list:1/lines", json={
            "line_items": sent, "offset": 1, "original_count": 2, "expected_version": 4})
    assert r.json()["line_ids"] == [_L0, _L1]


def test_a_list_of_sentence_reasons_reads_as_sentences():
    """Reasons that are whole sentences (Set as shipped again: each lot "has status Sold.")
    follow one another as sentences; reasons that are not keep their semicolons."""
    from ui.i18n import refusal_text
    sold = [{"message": f"{s} has status Sold.", "message_key": "lines.lot_status",
             "params": {"sku": s, "status": "sold"}} for s in ("EMR-1", "SAP-1")]
    text = refusal_text({"message": "Not available", "message_key": "lines.unavailable", "params": {"reasons": sold}})
    assert ".;" not in text and text.endswith("EMR-1 has status Sold. SAP-1 has status Sold."), text
    parts = [{"message": f"{s}: only goods out on memo can be part-returned, this one is sold",
              "message_key": "lines.revert_part_not_memo", "params": {"name": s, "status": "sold"}} for s in ("A", "B")]
    text = refusal_text({"message": "Cannot take back", "message_key": "lines.cannot_revert", "params": {"reasons": parts}})
    assert "this one is sold; B:" in text.replace("Sold", "sold"), text
