# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An exact barcode or RFID tag names one physical lot, and every way of entering it
(scan Enter, leaving the SKU field, the autocomplete list) picks that lot, never a
sibling lot of the same SKU. A collision or a failed lookup is said plainly, and a lot
held for another record or another line is never offered for a new line."""
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


def _patches(items, list_items=None):
    return (
        patch("ui.api_client.get_units", new=AsyncMock(return_value=_UNITS)),
        patch("ui.api_client.get_company", new=AsyncMock(return_value={"settings": {"inventory_method": "fifo"}})),
        patch("ui.api_client.list_items", new=list_items or AsyncMock(side_effect=_catalog(items))),
    )


async def _get(ui_client, url, items, list_items=None):
    p1, p2, p3 = _patches(items, list_items)
    with p1, p2, p3:
        return await ui_client.get(url, cookies=_authed())


async def _three_ways(ui_client, code, items, hint=""):
    """What scan Enter (lookup), leaving the SKU field and the autocomplete list pick."""
    enter = await _get(ui_client, f"/docs/catalog-lookup?sku={code}&doc_type=invoice{hint}", items)
    search = await _get(ui_client, f"/docs/catalog-search?q={code}&doc_type=invoice{hint}", items)
    return enter, search


@pytest.mark.parametrize("code", ["BC-77", "E2000077"])
async def test_enter_blur_and_autocomplete_pick_the_same_lot(ui_client, code):
    enter, search = await _three_ways(ui_client, code, [OLD, TAGGED])
    assert enter.status_code == 200 and enter.json()["entity_id"] == "item:tagged"
    options = search.json()
    # Autocomplete offers exactly the tagged lot, and leaving the field picks the option
    # marked exact: the same lot Enter picked, not the older sibling of the same SKU.
    assert [o["entity_id"] for o in options] == ["item:tagged"]
    assert options[0]["exact"] is True


async def test_a_cross_field_collision_is_refused_everywhere(ui_client):
    other = _lot("item:other", "2026-03-01", rfid_epc="BC-77")
    enter, search = await _three_ways(ui_client, "BC-77", [OLD, TAGGED, other])
    assert enter.status_code == 409 and search.status_code == 409
    assert search.json()["error"] == _EN["documents.code_names_several_lots"].format(code="BC-77")


async def test_a_lookup_failure_is_an_error_not_a_no_match(ui_client):
    failing = AsyncMock(side_effect=APIError(503, "Service unavailable"))
    enter = await _get(ui_client, "/docs/catalog-lookup?sku=BC-77", [], failing)
    search = await _get(ui_client, "/docs/catalog-search?q=BC-77", [], failing)
    for r in (enter, search):
        assert r.status_code == 502, r.text
        assert r.json()["error"] == "Service unavailable"


async def test_a_lot_reserved_on_another_invoice_is_refused_not_substituted(ui_client):
    held = {**TAGGED, "status": "reserved", "status_doc_id": "doc:other", "status_doc_number": "INV-9"}
    enter, search = await _three_ways(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine")
    expected = _EN["documents.lot_reserved_elsewhere"].format(code="BC-77")
    for r in (enter, search):
        assert r.status_code == 409, r.text
        assert r.json()["error"] == expected


async def test_a_lot_held_for_another_line_cannot_be_rebound(ui_client):
    held = {**TAGGED, "status": "reserved", "status_doc_id": "doc:mine", "status_line_entity_id": "L1"}
    enter, search = await _three_ways(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine&line_id=L2")
    expected = _EN["documents.lot_held_by_other_line"].format(code="BC-77")
    for r in (enter, search):
        assert r.status_code == 409, r.text
        assert r.json()["error"] == expected
    # The line that holds it may still pick it.
    enter, search = await _three_ways(ui_client, "BC-77", [OLD, held], "&doc_id=doc:mine&line_id=L1")
    assert enter.json()["entity_id"] == "item:tagged" and search.json()[0]["entity_id"] == "item:tagged"


async def test_sku_grouping_never_presents_a_foreign_held_representative(ui_client):
    held_old = {**OLD, "status": "reserved", "status_doc_id": "doc:other"}
    free_new = _lot("item:new", "2026-02-01", quantity=3)
    enter, search = await _three_ways(ui_client, "W", [held_old, free_new], "&doc_id=doc:mine")
    assert enter.json()["entity_id"] == "item:new"
    assert enter.json()["quantity"] == 3            # only what this record may draw
    assert [o["entity_id"] for o in search.json()] == ["item:new"]


async def test_a_sku_held_entirely_elsewhere_is_refused_not_fuzzy_matched(ui_client):
    held_old = {**OLD, "status": "reserved", "status_doc_id": "doc:other"}
    held_new = {**_lot("item:new", "2026-02-01"), "status": "reserved", "status_doc_id": "doc:other"}
    lookalike = {**_lot("item:x", "2026-01-05"), "sku": "W-2", "name": "W"}
    enter = await _get(ui_client, "/docs/catalog-lookup?sku=W&doc_type=invoice&doc_id=doc:mine",
                       [held_old, held_new, lookalike])
    assert enter.status_code == 409, enter.text
    assert enter.json()["error"] == _EN["documents.lot_reserved_elsewhere"].format(code="W")


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
