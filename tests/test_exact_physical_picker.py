# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An exact barcode or RFID tag names one physical lot, and the autocomplete list (and
leaving the SKU field, which takes its exact option) picks that lot, never a sibling
lot of the same SKU. A collision or a failed search is said plainly, and a lot held
for another record or another line is never offered for a new line. Scanning the same
codes is covered by the document scan tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_UNITS = [{"name": "piece", "label": "Piece", "decimals": 0, "unit_type": "count"}]
_EN = json.loads((Path(__file__).resolve().parents[1] / "ui" / "locales" / "en.json").read_text())


def _lot(eid: str, created: str, **extra) -> dict:
    return {"entity_id": eid, "sku": "W", "name": "Widget", "quantity": 5, "allow_splitting": True,
            "created_at": created, "sell_by": "piece", "status": "available", **extra}


OLD = _lot("item:old", "2026-01-01")
TAGGED = _lot("item:tagged", "2026-02-01", barcode="BC-77", rfid_epc="E2000077")


def _catalog(items: list[dict]):
    """A stand-in for GET /items filtering by the exact fields the picker sends."""
    async def list_items(_token, params):
        def keep(item):
            for field in ("barcode", "rfid_epc", "gtin", "sku", "status"):
                if field in params and str(item.get(field) or "") != params[field]:
                    return False
            if "q" in params:
                return params["q"].lower() in f"{item.get('sku')} {item.get('name')}".lower()
            return True
        return {"items": [i for i in items if keep(i)]}
    return list_items


_DOCS = {
    "doc:other": {"entity_id": "doc:other", "doc_number": "SO-3"},
    "doc:mine": {"entity_id": "doc:mine", "doc_number": "PF-1"},
    "doc:from-memo": {"entity_id": "doc:from-memo", "doc_number": "PF-2", "source_memo_id": "doc:memo"},
}


async def _get_doc(_token, entity_id):
    if entity_id not in _DOCS:
        raise APIError(404, "Not found")
    return _DOCS[entity_id]


def _patches(items, list_items=None):
    return (
        patch("ui.api_client.get_units", new=AsyncMock(return_value=_UNITS)),
        patch("ui.api_client.get_company", new=AsyncMock(return_value={"settings": {"inventory_method": "fifo"}})),
        patch("ui.api_client.list_items", new=list_items or AsyncMock(side_effect=_catalog(items))),
        patch("ui.api_client.get_doc", new=AsyncMock(side_effect=_get_doc)),
    )


async def _get(ui_client, url, items, list_items=None):
    p1, p2, p3, p4 = _patches(items, list_items)
    with p1, p2, p3, p4:
        return await ui_client.get(url, cookies=_authed())


async def _search(ui_client, code, items, hint=""):
    """What leaving the SKU field and the autocomplete list pick."""
    return await _get(ui_client, f"/docs/catalog-search?q={code}&doc_type=invoice{hint}", items)


@pytest.mark.parametrize("code", ["BC-77", "E2000077"])
async def test_blur_and_autocomplete_pick_the_exact_lot(ui_client, code):
    search = await _search(ui_client, code, [OLD, TAGGED])
    options = search.json()
    # Autocomplete offers exactly the tagged lot, and leaving the field picks the option
    # marked exact, not the older sibling of the same SKU.
    assert [o["entity_id"] for o in options] == ["item:tagged"]
    assert options[0]["exact"] is True


async def test_a_cross_field_collision_is_refused_everywhere(ui_client):
    other = _lot("item:other", "2026-03-01", rfid_epc="BC-77")
    search = await _search(ui_client, "BC-77", [OLD, TAGGED, other])
    assert search.status_code == 409
    assert search.json()["error"] == _EN["documents.code_names_several_lots"].format(code="BC-77")


async def test_a_search_failure_is_an_error_not_a_no_match(ui_client):
    failing = AsyncMock(side_effect=APIError(503, "Service unavailable"))
    r = await _get(ui_client, "/docs/catalog-search?q=BC-77", [], failing)
    assert r.status_code == 502, r.text
    assert r.json()["error"] == "Service unavailable"


async def test_a_lot_reserved_on_another_invoice_is_refused_not_substituted(ui_client):
    held = {**TAGGED, "status": "reserved", "status_doc_id": "doc:other", "status_doc_number": "INV-9"}
    r = await _search(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine")
    expected = _EN["documents.lot_reserved_by"].format(code="BC-77", doc="INV-9")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == expected


@pytest.mark.parametrize("holder, expected", [
    ("doc:other", ("documents.lot_reserved_by", {"doc": "SO-3"})),        # number looked up
    ("doc:gone", ("documents.lot_reserved_elsewhere", {})),               # cannot be resolved
])
async def test_the_refusal_names_the_holding_record_when_it_can(ui_client, holder, expected):
    held = {**TAGGED, "status": "reserved", "status_doc_id": holder}
    r = await _search(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine")
    key, extra = expected
    assert r.status_code == 409, r.text
    assert r.json()["error"] == _EN[key].format(code="BC-77", **extra)


async def test_a_lot_out_on_another_memo_is_refused_naming_the_memo(ui_client):
    out = {**TAGGED, "status": "memo_out", "status_doc_id": "doc:memo", "status_doc_number": "MEMO-4"}
    r = await _search(ui_client, "BC-77", [OLD, out], "&doc_id=doc:mine")
    expected = _EN["documents.lot_on_memo_by"].format(code="BC-77", doc="MEMO-4")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == expected


async def test_an_invoice_made_from_the_memo_may_take_its_lot(ui_client):
    out = {**TAGGED, "status": "memo_out", "status_doc_id": "doc:memo", "status_doc_number": "MEMO-4"}
    r = await _search(ui_client, "BC-77", [OLD, out], "&doc_id=doc:from-memo")
    assert r.json()[0]["entity_id"] == "item:tagged"


async def test_a_lot_held_for_another_line_cannot_be_rebound(ui_client):
    held = {**TAGGED, "status": "reserved", "status_doc_id": "doc:mine", "status_line_entity_id": "L1"}
    r = await _search(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine&line_id=L2")
    expected = _EN["documents.lot_held_by_other_line"].format(code="BC-77")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == expected
    # The line that holds it may still pick it.
    r = await _search(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine&line_id=L1")
    assert r.json()[0]["entity_id"] == "item:tagged"


async def test_sku_grouping_never_presents_a_foreign_held_representative(ui_client):
    held_old = {**OLD, "status": "reserved", "status_doc_id": "doc:other"}
    free_new = _lot("item:new", "2026-02-01", quantity=3)
    r = await _search(ui_client, "W", [held_old, free_new], "&doc_id=doc:mine")
    assert [o["entity_id"] for o in r.json()] == ["item:new"]
    assert r.json()[0]["quantity"] == 3             # only what this record may draw


async def test_bill_free_text_entries_stay_selectable(ui_client):
    nonstock = {"entity_id": "item:svc", "sku": "FREIGHT", "name": "Freight", "quantity": 0,
                "sell_by": "piece", "status": "available", "manage_stock": False}
    r = await _get(ui_client, "/docs/catalog-search?q=FREIGHT&doc_type=bill", [nonstock])
    assert r.status_code == 200 and [o["entity_id"] for o in r.json()] == ["item:svc"]


async def test_the_editor_sends_the_line_hint_and_prefers_the_exact_option(ui_client):
    from ui.routes.documents import _doc_detail
    from fasthtml.common import to_xml

    doc = {"entity_id": "doc:mine", "id": "doc:mine", "doc_type": "invoice", "status": "draft", "line_items": []}
    html = to_xml(_doc_detail(doc))
    assert "_CELERP_DOC_ID = \"doc:mine\"" in html
    assert "_celerpHolderParam(" in html
    assert "i.exact" in html
