# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every document's line-item picker opens on a starting list (SAL-014, SAL-003 everywhere).

The starting list is the first items in the inventory list's default order that this line may
draw. A lot held for another record (a quotation's reservation, a lot out on a memo) is skipped,
and the list reads on past it, so a newer document whose newest items are all held by an older
one still offers the rest of the stock instead of "No items yet"."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from test_form_submitted_twice import _Request, routes  # noqa: F401  (routes is a fixture)
from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

# The document types whose editor uses the line-item picker; lists cover quotations,
# transfers, audits and write-offs (one editor, doc_type "list").
_DOC_TYPES = ("invoice", "memo", "proforma", "credit_note", "debit_note", "bill", "purchase_order",
              "consignment_in", "production_order")
_UNITS = [{"name": "piece", "label": "Piece", "decimals": 0, "unit_type": "count"}]


def _lot(n: int, **extra) -> dict:
    return {"entity_id": f"item:{n:03d}", "sku": f"S-{n:03d}", "name": f"Item {n:03d}", "quantity": 1,
            "allow_splitting": True, "sell_by": "piece", "status": "available", **extra}


def _paged(items: list[dict]):
    """GET /items in its default order (the list as given), honouring limit, offset and status."""
    async def list_items(_token, params):
        rows = [i for i in items if "status" not in params or i.get("status") == params["status"]]
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
        return {"items": rows[offset:offset + limit], "total": len(rows)}
    return AsyncMock(side_effect=list_items)


async def _starting_list(ui_client, items, doc_type="invoice", doc_id="doc:new"):
    with patch("ui.api_client.get_units", new=AsyncMock(return_value=_UNITS)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"settings": {"inventory_method": "fifo"}})), \
         patch("ui.api_client.list_items", new=_paged(items)), \
         patch("ui.api_client.get_doc", new=AsyncMock(return_value={"entity_id": doc_id})):
        ui_client.cookies.update(_authed())
        r = await ui_client.get(f"/docs/catalog-search?q=&doc_type={doc_type}&doc_id={doc_id}")
    assert r.status_code == 200, r.text
    return [o["sku"] for o in r.json()]


# ── The defect: the newest items all held by another record ────────────────

@pytest.mark.asyncio
async def test_an_invoice_reads_past_items_held_by_a_quotation(ui_client):
    """The quotation reserved the 20 newest items; the invoice still opens on the free stock."""
    from ui.routes.documents import _PICKER_START_LIMIT
    held = [_lot(n, status="reserved", status_doc_id="list:Q-1") for n in range(_PICKER_START_LIMIT)]
    free = [_lot(100 + n) for n in range(5)]
    assert await _starting_list(ui_client, held + free) == [f"S-{100 + n:03d}" for n in range(5)]


@pytest.mark.asyncio
async def test_the_quotation_holding_them_still_sees_its_own_items(ui_client):
    held = [_lot(n, status="reserved", status_doc_id="list:Q-1") for n in range(3)]
    assert await _starting_list(ui_client, held, doc_type="list", doc_id="list:Q-1") == ["S-000", "S-001", "S-002"]


@pytest.mark.asyncio
async def test_a_partly_held_page_is_topped_up_to_the_limit(ui_client):
    from ui.routes.documents import _PICKER_START_LIMIT
    items = [_lot(n, **({"status": "memo_out", "status_doc_id": "doc:memo-9"} if n % 2 else {}))
             for n in range(_PICKER_START_LIMIT * 3)]
    offered = await _starting_list(ui_client, items)
    assert len(offered) == _PICKER_START_LIMIT
    assert offered == [f"S-{n:03d}" for n in range(0, _PICKER_START_LIMIT * 2, 2)]


@pytest.mark.asyncio
async def test_drafts_are_skipped_and_the_reading_stops(ui_client):
    """Drafts are not stock. The reading is bounded: a catalog of nothing but held lots ends
    with an empty list, not an endless scan."""
    from ui.routes.documents import _PICKER_START_LIMIT, _PICKER_START_PAGES
    drafts = [_lot(n, status="draft") for n in range(3)]
    assert await _starting_list(ui_client, drafts + [_lot(50)]) == ["S-050"]
    held = [_lot(n, status="reserved", status_doc_id="doc:other") for n in range(_PICKER_START_LIMIT * (_PICKER_START_PAGES + 2))]
    lookup = _paged(held)
    with patch("ui.api_client.get_units", new=AsyncMock(return_value=_UNITS)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"settings": {}})), \
         patch("ui.api_client.list_items", new=lookup), \
         patch("ui.api_client.get_doc", new=AsyncMock(return_value={})):
        ui_client.cookies.update(_authed())
        r = await ui_client.get("/docs/catalog-search?q=&doc_type=invoice&doc_id=doc:new")
    assert r.status_code == 200 and r.json() == []
    assert lookup.await_count == _PICKER_START_PAGES


# ── One test per document type, on real stock ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("doc_type", _DOC_TYPES + ("list",))
async def test_every_document_type_opens_on_a_starting_list(client, auth, routes, doc_type):
    tag = uuid.uuid4().hex[:6]
    for i in range(3):
        r = await client.post("/items", headers=auth["headers"], json={
            "status": "available", "sku": f"PT-{tag}-{i}", "name": f"Per type {tag} {i}",
            "quantity": 1, "sell_by": "piece"})
        assert r.status_code == 200, r.text
    if doc_type == "list":
        r = await client.post("/lists", headers=auth["headers"], json={"list_type": "quotation", "line_items": []})
    else:
        r = await client.post("/docs", headers=auth["headers"], json={"doc_type": doc_type, "line_items": []})
    assert r.status_code == 200, r.text

    query = {"q": "", "doc_type": doc_type, "doc_id": r.json()["id"]}
    resp = await routes[("get", "/docs/catalog-search")](_Request(query=query))
    assert resp.status_code == 200, resp.body
    offered = {o["sku"] for o in json.loads(resp.body)}
    assert {f"PT-{tag}-{i}" for i in range(3)} <= offered, offered
