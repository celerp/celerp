# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Receiving goods takes only the lines picked, each up to what it still awaits.

The server finds the line each receipt is for under the document lock, by the line's id
when the caller gives one, else by its position or a unique item, SKU or name. A
receipt that names no line, or names one ambiguously, is refused on every purchase
document, consignments included. What each line has received across all receipts caps
the next one, and every new receipt records the line it came in on. Goods are received
only while the document is in a status that receives them.
"""
from __future__ import annotations

import uuid
from html.parser import HTMLParser

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from celerp.models.projections import Projection
from test_cost_restatement import _item, _state
from test_receipt_accounting import _doc, _finalize, _parcels
from test_receive_goods_form import _Request, _Routes


async def _stamp_line_ids(session, auth, doc_id: str) -> list[str]:
    """Give every line of the document a line id, as line writers do, and return them."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc_id})
    lines = [{**li, "line_id": li.get("line_id") or str(uuid.uuid4())} for li in row.state["line_items"]]
    row.state = {**row.state, "line_items": lines}
    await session.commit()
    return [li["line_id"] for li in lines]


async def _post(client, auth, doc_id: str, *items: dict, **extra):
    return await client.post(f"/docs/{doc_id}/receive", headers=auth["headers"],
                             json={"location_id": "", "received_items": list(items), **extra})


def _stock_lines(n: int, qty: float = 10) -> list[dict]:
    tag = uuid.uuid4().hex[:6]
    return [{"sku": f"SEL-{tag}-{i}", "name": f"Goods {i}", "quantity": qty, "unit_price": 2.0} for i in range(n)]


async def _issued(client, session, auth, doc_type: str, lines: list[dict]) -> tuple[str, list[str]]:
    doc_id = await _doc(client, auth, doc_type, lines)
    await _finalize(client, auth, doc_id)
    return doc_id, await _stamp_line_ids(session, auth, doc_id)


async def _received(session, auth, doc_id: str) -> list[float]:
    return [float(li.get("quantity_received") or 0) for li in (await _state(session, auth, doc_id))["line_items"]]


@pytest.mark.asyncio
async def test_receive_selected_rows_only(client, session, auth):
    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(3))

    r = await _post(client, auth, bill, {"source_line_id": ids[2], "quantity_received": 10})
    assert r.status_code == 200, r.text
    assert await _received(session, auth, bill) == [0, 0, 10]
    state = await _state(session, auth, bill)
    assert [x["source_line_id"] for x in state["received_items"]] == [ids[2]]
    assert [p["sku"] for p in await _parcels(session, auth, bill)] == [state["line_items"][2]["sku"]]

    # Rows 1 and 3 together: row 3 has nothing left to receive, so nothing is received.
    r = await _post(client, auth, bill, {"source_line_id": ids[0], "quantity_received": 10},
                    {"source_line_id": ids[2], "quantity_received": 1})
    assert r.status_code == 422, r.text
    assert await _received(session, auth, bill) == [0, 0, 10]

    r = await _post(client, auth, bill, {"source_line_id": ids[0], "quantity_received": 10})
    assert r.status_code == 200, r.text
    assert await _received(session, auth, bill) == [10, 0, 10]
    state = await _state(session, auth, bill)
    assert [x["source_line_id"] for x in state["received_items"]] == [ids[2], ids[0]]
    assert [x["po_line_index"] for x in state["received_items"]] == [2, 0]


@pytest.mark.asyncio
async def test_receive_partial_then_remaining(client, session, auth):
    from ui.routes import documents

    bill, [line_id] = await _issued(client, session, auth, "bill", _stock_lines(1))
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 4})
    assert r.status_code == 200, r.text

    # The form offers what the line still awaits, not what it ordered.
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    form = _ReceiveRows({0})
    form.feed(to_xml(documents._li_bulk_toolbar(bill, False, show_fulfill=True, is_inbound=True,
                                                inbound_line_items=doc["line_items"], locations=[])))
    assert form.qty_inputs == {"qty_0": {"value": "6", "max": "6"}}

    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 6})
    assert r.status_code == 200, r.text
    assert await _received(session, auth, bill) == [10]
    assert (await _state(session, auth, bill))["status"] == "received"
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 1})
    assert r.status_code == 422, r.text
    assert "at most 0 more" in r.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_consignment_in_cumulative_cap(client, session, auth):
    doc, [line_id] = await _issued(client, session, auth, "consignment_in", _stock_lines(1))
    r = await _post(client, auth, doc, {"source_line_id": line_id, "quantity_received": 10})
    assert r.status_code == 200, r.text
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    r = await _post(client, auth, doc, {"sku": sku, "name": "Goods 0", "quantity_received": 10})
    assert r.status_code == 422, r.text
    assert sum(p["quantity"] for p in await _parcels(session, auth, doc)) == 10


@pytest.mark.asyncio
async def test_unmatched_item_refused(client, session, auth):
    # A consignment receives only goods one of its lines is for.
    doc, _ = await _issued(client, session, auth, "consignment_in", _stock_lines(1))
    r = await _post(client, auth, doc, {"sku": "NOT-ON-IT", "name": "Stray", "quantity_received": 3})
    assert r.status_code == 422, r.text
    assert not (await _state(session, auth, doc)).get("received_item_ids")

    # Two lines for the same SKU: a receipt naming only the SKU could be for either.
    sku = f"DUP-{uuid.uuid4().hex[:6]}"
    bill, ids = await _issued(client, session, auth, "bill", [
        {"sku": sku, "name": "Twin", "quantity": 5, "unit_price": 2.0},
        {"sku": sku, "name": "Twin", "quantity": 5, "unit_price": 2.0},
    ])
    r = await _post(client, auth, bill, {"sku": sku, "quantity_received": 5})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.receive_line_ambiguous"

    # A line id that is not on the document, or that disagrees with the position given.
    r = await _post(client, auth, bill, {"source_line_id": str(uuid.uuid4()), "quantity_received": 1})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.receive_line_unknown"
    r = await _post(client, auth, bill, {"source_line_id": ids[1], "po_line_index": 0, "quantity_received": 1})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.receive_line_mismatch"
    assert await _received(session, auth, bill) == [0, 0]

    r = await _post(client, auth, bill, {"source_line_id": ids[1], "quantity_received": 5})
    assert r.status_code == 200, r.text
    assert await _received(session, auth, bill) == [0, 5]


@pytest.mark.asyncio
async def test_po_existing_lot_receipt_records_its_line(client, session, auth):
    lot = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order", [
        {"item_id": lot, "name": "Lot", "quantity": 10, "unit_price": 14.0},
        {"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0},
    ])
    ids = await _stamp_line_ids(session, auth, po)
    r = await _post(client, auth, po, {"source_line_id": ids[1], "quantity_received": 5, "receive_as": "stock"})
    assert r.status_code == 200, r.text
    [entry] = (await _state(session, auth, po))["received_items"]
    assert (entry["source_line_id"], entry["po_line_index"], entry["item_id"], entry["lot_quantity_added"]) == (
        ids[1], 1, lot, 5)
    assert (await _state(session, auth, lot))["quantity"] == 15
    assert await _received(session, auth, po) == [0, 5]


@pytest.mark.asyncio
async def test_receive_converts_purchase_units_to_stock_units(client, session, auth):
    sku = f"BOX-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": sku, "name": "Box of 12", "quantity": 0, "sell_by": "piece",
        "purchase_conversion_factor": 12})
    assert r.status_code == 200, r.text
    bill, [line_id] = await _issued(client, session, auth, "bill", [
        {"item_id": r.json()["id"], "sku": sku, "name": "Box of 12", "quantity": 3, "unit_price": 24.0},
    ])
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 2})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, bill)
    assert (parcel["quantity"], parcel["cost_total"]) == (24, 48.0)
    assert await _received(session, auth, bill) == [2]


@pytest.mark.asyncio
async def test_receive_void_refused(client, session, auth):
    bill, [line_id] = await _issued(client, session, auth, "bill", _stock_lines(1))
    r = await client.post(f"/docs/{bill}/void", headers=auth["headers"], json={"reason": "wrong supplier"})
    assert r.status_code == 200, r.text
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.receive_not_open"
    assert not (await _state(session, auth, bill)).get("received_items")



async def _journal_entries(session, auth) -> list[dict]:
    from sqlalchemy import select
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    return [r.state for r in rows]


@pytest.mark.asyncio
async def test_a_draft_consignment_receives_goods_and_books_nothing(client, session, auth):
    """Consigned goods are not owned, so receiving them books nothing to stock or payables, and
    a consignment still in draft receives them the same as a purchase order does."""
    before = await _journal_entries(session, auth)
    draft = await _doc(client, auth, "consignment_in", _stock_lines(1))
    [draft_line] = await _stamp_line_ids(session, auth, draft)
    r = await _post(client, auth, draft, {"source_line_id": draft_line, "quantity_received": 4})
    assert r.status_code == 200, r.text
    assert await _received(session, auth, draft) == [4]
    [parcel] = await _parcels(session, auth, draft)
    assert float(parcel["quantity"]) == 4.0
    assert await _journal_entries(session, auth) == before
    # Finalizing it afterwards books nothing either, and the rest still comes in.
    await _finalize(client, auth, draft)
    r = await _post(client, auth, draft, {"source_line_id": draft_line, "quantity_received": 6})
    assert r.status_code == 200, r.text
    assert await _received(session, auth, draft) == [10]
    assert await _journal_entries(session, auth) == before


@pytest.mark.asyncio
async def test_a_draft_bill_still_receives_nothing(client, session, auth):
    """Neighbour: a bill not yet issued booked nothing, so its goods wait for it."""
    bill = await _doc(client, auth, "bill", _stock_lines(1))
    [line_id] = await _stamp_line_ids(session, auth, bill)
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.receive_draft_bill"


def test_inbound_status_gate_from_constants():
    """The finalized view offers Receive Goods exactly in the statuses the API receives in."""
    from celerp_docs.doc_constants import RECEIVABLE_STATUSES
    from ui.routes.documents import _doc_detail

    assert {"paid", "partial", "partial_returned", "received"} <= RECEIVABLE_STATUSES["bill"]
    for doc_type in ("bill", "consignment_in", "purchase_order"):
        assert not {"void", "closed"} & RECEIVABLE_STATUSES[doc_type]
    # A draft bill has booked nothing, so it receives nothing; a draft order or consignment
    # receives (the order's receipt books its own entry, the consignment's books none).
    assert "draft" not in RECEIVABLE_STATUSES["bill"]
    for doc_type in ("consignment_in", "purchase_order"):
        assert "draft" in RECEIVABLE_STATUSES[doc_type]

    def offers_receive(status: str) -> bool:
        doc = {"entity_id": "doc:gate-1", "doc_type": "bill", "status": status, "ref_id": "B-1", "currency": "USD",
               "line_items": [{"sku": "G-1", "name": "Goods", "quantity": 2, "unit_price": 1, "line_total": 2,
                               "receive_as": "stock"}]}
        return 'id="li-bulk-fulfill-btn"' in to_xml(_doc_detail(doc, item_status_map={}))

    for status in RECEIVABLE_STATUSES["bill"]:
        assert offers_receive(status), status
    assert not offers_receive("void")


class _ReceiveRows(HTMLParser):
    """What a browser submits from the Receive Goods form once ``selected`` rows are ticked:
    the inputs in those rows' fieldsets (the page's script enables only theirs) and every
    shared input."""

    def __init__(self, selected: set[int], form_id: str = "li-bulk-fulfill-btn"):
        super().__init__()
        self.selected = selected
        self.form_id = form_id
        self.fields: list[tuple[str, str]] = []
        self.qty_inputs: dict[str, dict] = {}
        self.confirm: str | None = None
        self._inside = False
        self._row: int | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._inside = a.get("id") == self.form_id
            if self._inside:
                self.confirm = a.get("hx-confirm")
        elif self._inside and tag == "fieldset":
            self._row = int(a["data-line-index"])
        elif self._inside and tag in ("input", "select") and a.get("name"):
            if self._row is not None and self._row not in self.selected:
                return
            # A disabled field, or a box left unticked, sends nothing.
            if "disabled" in a or (a.get("type") == "checkbox" and "checked" not in a):
                return
            if tag == "input":
                self.fields.append((a["name"], a.get("value") or ""))
                if a.get("type") == "number":
                    self.qty_inputs[a["name"]] = {"value": a.get("value"), "max": a.get("max")}

    def handle_endtag(self, tag):
        if tag == "form":
            self._inside = False
        elif tag == "fieldset":
            self._row = None


async def _submit(client, auth, monkeypatch, doc_id: str, fields: list[tuple[str, str]]):
    from ui.routes import documents

    sent: list[dict] = []

    async def receive_po(_tok, entity_id, data):
        sent.append(data)
        r = await client.post(f"/docs/{entity_id}/receive", headers=auth["headers"], json=data)
        return api_client._raise(r).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "receive_po", receive_po)
    routes = _Routes()
    documents.setup_routes(routes)
    resp = await routes.routes[("post", "/docs/{entity_id}/receive")](_Request(fields), doc_id)
    return resp, sent


@pytest.mark.asyncio
async def test_receive_form_posts_only_selected_rows(client, session, auth, monkeypatch):
    from ui.routes import documents

    catalog = await _item(client, auth, 10.0, qty=0)
    lines = _stock_lines(3)
    lines[0] = {"item_id": catalog, "name": "Catalog goods", "quantity": 10, "unit_price": 2.0}
    bill, ids = await _issued(client, session, auth, "bill", lines)
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    form = _ReceiveRows({0, 2})
    form.feed(to_xml(documents._li_bulk_toolbar(bill, False, show_fulfill=True, is_inbound=True,
                                                inbound_line_items=doc["line_items"], locations=[])))
    assert form.confirm, "the receipt is confirmed before it is posted"
    assert set(form.qty_inputs) == {"qty_0", "qty_2"}
    # The form names lines and quantities only: the server takes item and kind from the line.
    names = {name for name, _ in form.fields}
    assert not any(n.startswith(("item_id_", "receive_as_", "sku_")) for n in names)

    resp, sent = await _submit(client, auth, monkeypatch, bill,
                               [(n, "3" if n == "qty_2" else v) for n, v in form.fields])
    assert resp.status_code == 204, resp.body
    assert [(x["po_line_index"], x["source_line_id"], x["quantity_received"]) for x in sent[0]["received_items"]] == [
        (0, ids[0], 10), (2, ids[2], 3)]
    assert await _received(session, auth, bill) == [10, 0, 3]
    # The parcel received on the catalog line is made from that catalog item.
    parcels = await _parcels(session, auth, bill)
    assert parcels[0].get("catalog_item_id"), parcels[0]


@pytest.mark.asyncio
async def test_receive_form_skips_received_rows_and_refuses_an_empty_selection(client, session, auth, monkeypatch):
    from ui.routes import documents

    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(2))
    r = await _post(client, auth, bill, {"source_line_id": ids[0], "quantity_received": 10})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    form = _ReceiveRows({0})
    html = to_xml(documents._li_bulk_toolbar(bill, False, show_fulfill=True, is_inbound=True,
                                             inbound_line_items=doc["line_items"], locations=[]))
    form.feed(html)
    assert form.qty_inputs == {}
    assert "Already received" in html

    resp, sent = await _submit(client, auth, monkeypatch, bill, form.fields)
    assert sent == []
    assert resp.status_code != 204
    assert "Select at least one line" in resp.headers["HX-Trigger"]


@pytest.mark.parametrize("key", [
    "docs.receive_not_open", "docs.receive_line_ambiguous", "docs.receive_line_mismatch", "docs.receive_line_unknown",
    "documents.already_received", "documents.confirm_receive_selected", "documents.receive_nothing_selected",
    "documents.receive_pick_quantity",
])
def test_receive_copy_in_every_locale(key):
    from ui import i18n

    english = i18n.t(key, "en")
    for code in i18n.available_langs():
        text = i18n.t(key, code)
        assert text != key, code
        assert code == "en" or text != english, code
