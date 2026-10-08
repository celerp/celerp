# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Return Goods sends back what selected lines received, in stock units, from goods on hand.

The caller names lines and a stock quantity per line. Under the document lock the server
finds every lot those lines' receipts went into (a new parcel, or a lot a purchase order
added to), takes the quantity from them in receipt order, and sends it back through the
same return as a request that names lots. Only goods on hand and free go back: a lot that
is reserved, sold or out on memo is refused, and so is a line whose goods cannot be told
apart from another line's. A request that names lots keeps its old shape and digest.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from celerp.models.projections import Projection
from test_cost_restatement import _item, _state
from test_receipt_accounting import _books, _doc, _finalize, _parcels
from test_receive_goods_form import _Request, _Routes
from test_receive_selected_lines import _issued, _post, _ReceiveRows, _stamp_line_ids, _stock_lines

pytestmark = pytest.mark.asyncio


async def _return_lines(client, auth, doc_id: str, *lines: dict, **extra):
    return await client.post(f"/docs/{doc_id}/return-items", headers=auth["headers"],
                             json={"lines": list(lines), **extra})


async def _qty(session, auth, item_id: str) -> float:
    return float((await _state(session, auth, item_id))["quantity"])


async def test_return_selected_line_from_its_parcels(client, session, auth):
    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(2, qty=4))
    for line_id, qty in ((ids[0], 2), (ids[0], 2), (ids[1], 4)):
        r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": qty})
        assert r.status_code == 200, r.text
    first, second, other = (await _state(session, auth, bill))["received_item_ids"]
    books = await _books(session, auth, "1130-P", "2110")

    r = await _return_lines(client, auth, bill, {"line_id": ids[0], "quantity_returned": 3})
    assert r.status_code == 200, r.text
    # Earliest receipt first; the other line's goods stay put.
    assert [await _qty(session, auth, i) for i in (first, second, other)] == [0, 1, 4]
    returned = (await _state(session, auth, bill))["returned_items"]
    assert [(x["item_id"], x["quantity_returned"], x["source_line_id"]) for x in returned] == [
        (first, 2, ids[0]), (second, 1, ids[0])]
    after = await _books(session, auth, "1130-P", "2110")
    assert after["1130-P"] == pytest.approx(books["1130-P"] - 6)
    assert after["2110"] == pytest.approx(books["2110"] + 6)


async def test_return_po_existing_lot_line(client, session, auth):
    lot = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    [line_id] = await _stamp_line_ids(session, auth, po)
    r = await _post(client, auth, po, {"source_line_id": line_id, "quantity_received": 5, "receive_as": "stock"})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 15

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 6})
    assert r.status_code == 422, r.text
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 12
    [entry] = (await _state(session, auth, po))["returned_items"]
    assert (entry["item_id"], entry["quantity_returned"], entry["source_line_id"]) == (lot, 3, line_id)


async def test_return_line_quantity_is_in_stock_units(client, session, auth):
    sku = f"RBX-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": sku, "name": "Box of 12", "quantity": 0, "sell_by": "piece",
        "purchase_conversion_factor": 12})
    assert r.status_code == 200, r.text
    bill, [line_id] = await _issued(client, session, auth, "bill", [
        {"item_id": r.json()["id"], "sku": sku, "name": "Box of 12", "quantity": 3, "unit_price": 24.0}])
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 2})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, bill)
    assert parcel["quantity"] == 24

    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 24

    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 30})
    assert r.status_code == 422, r.text
    assert "24" in r.json()["detail"]["message"]
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 12})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, bill)
    assert parcel["quantity"] == 12
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 12
    # Half the parcel is still here: the bill is partly returned, judged in stock units.
    assert doc["status"] == "partial_returned"
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 12})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, bill))["status"] == "returned"


async def test_doc_says_what_each_line_sent_back(client, session, auth):
    """Each line states whether this document sent its goods back, in part or in full, so the
    line badge can say what the document did rather than what the catalog item is now."""
    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(2, qty=4))
    for line_id in ids:
        r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 4})
        assert r.status_code == 200, r.text

    async def _states():
        doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
        return [li.get("return_status") for li in doc["line_items"]]

    assert await _states() == [None, None]
    r = await _return_lines(client, auth, bill, {"line_id": ids[0], "quantity_returned": 1})
    assert r.status_code == 200, r.text
    assert await _states() == ["partial_returned", None]
    r = await _return_lines(client, auth, bill, {"line_id": ids[0], "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _states() == ["returned", None]
    r = await _return_lines(client, auth, bill, {"line_id": ids[1], "quantity_returned": 4})
    assert r.status_code == 200, r.text
    # The whole bill went back: still said per line once the bill reads Returned.
    assert (await _state(session, auth, bill))["status"] == "returned"
    assert await _states() == ["returned", "returned"]


async def test_return_form_says_why_a_line_is_capped(client, session, auth):
    """A line whose goods are partly or wholly held says what holds them next to its field,
    rather than a bare browser maximum or a plain "Nothing to return"."""
    from ui.routes import documents

    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(2, qty=4))
    for line_id in ids:
        r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 4})
        assert r.status_code == 200, r.text
    first, second = (await _state(session, auth, bill))["received_item_ids"]
    for parcel, qty in ((first, 2), (second, 4)):
        r = await client.post(f"/items/{parcel}/reserve", headers=auth["headers"], json={"quantity": qty})
        assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    assert [li.get("returnable_quantity") for li in doc["line_items"]] == [2, 0]
    assert [li.get("return_held") for li in doc["line_items"]] == [{"reserved": 2}, {"reserved": 4}]

    html = to_xml(documents._li_bulk_toolbar(bill, False, show_fulfill=True, is_inbound=True,
                                             inbound_line_items=doc["line_items"], locations=[]))
    assert "Not free to return: 2 reserved" in html
    assert "Nothing to return: 4 reserved" in html


async def test_return_refuses_reserved_lot(client, session, auth):
    bill, [line_id] = await _issued(client, session, auth, "bill", _stock_lines(1, qty=4))
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]

    # Part of the lot held: only the free part can go back.
    r = await client.post(f"/items/{parcel}/reserve", headers=auth["headers"], json={"quantity": 3})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": parcel, "quantity_returned": 2}]})
    assert r.status_code == 409, r.text
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 422, r.text
    assert await _qty(session, auth, parcel) == 4
    r = await client.post(f"/items/{parcel}/unreserve", headers=auth["headers"], json={"quantity": 3})
    assert r.status_code == 200, r.text

    # The whole lot reserved to a sale.
    inv = await _doc(client, auth, "invoice", [
        {"entity_id": parcel, "sku": (await _state(session, auth, parcel))["sku"], "name": "Goods",
         "quantity": 4, "unit_price": 5.0}])
    await _finalize(client, auth, inv)
    r = await client.post(f"/docs/{inv}/reserve-lines", headers=auth["headers"],
                          json={"line_entity_ids": [parcel], "new_status": "reserved"})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, parcel))["status"] == "reserved"

    r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": parcel, "quantity_returned": 1}]})
    assert r.status_code == 409, r.text
    assert "not on hand" in r.json()["detail"]["message"]
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 1})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_line_not_on_hand"
    assert await _qty(session, auth, parcel) == 4
    assert not (await _state(session, auth, bill)).get("returned_items")


async def test_return_lines_replay_and_old_request_digest(client, session, auth):
    from celerp_docs.routes import ReturnBody

    # A request naming lots serializes exactly as it always has.
    old = ReturnBody(items=[{"item_id": "item:x", "quantity_returned": 1}], idempotency_key="k")
    assert old.model_dump(mode="json", exclude={"idempotency_key"}) == {
        "items": [{"item_id": "item:x", "quantity_returned": 1.0}], "notes": None}

    bill, [line_id] = await _issued(client, session, auth, "bill", _stock_lines(1, qty=4))
    r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    key = f"ret-{uuid.uuid4().hex}"
    first = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 1}, idempotency_key=key)
    assert first.status_code == 200, first.text
    again = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 1}, idempotency_key=key)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await _qty(session, auth, parcel) == 3
    other = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2}, idempotency_key=key)
    assert other.status_code == 409, other.text
    assert await _qty(session, auth, parcel) == 3


async def test_return_line_request_is_checked(client, session, auth):
    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(2, qty=4))
    r = await _post(client, auth, bill, {"source_line_id": ids[0], "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]

    bad = [
        {"lines": [{"line_id": "nope", "quantity_returned": 1}]},
        {"lines": [{"line_id": ids[1], "quantity_returned": 1}]},  # nothing received on it
        {"lines": [{"line_id": ids[0], "quantity_returned": 1}, {"line_id": ids[0], "quantity_returned": 1}]},
        {"lines": [{"line_index": 0, "quantity_returned": 1}]},  # the line has an id
        {"lines": [{"quantity_returned": 1}]},
        {"lines": []},
        {"items": [], "lines": []},
        {"items": [{"item_id": parcel, "quantity_returned": 1}], "lines": [{"line_id": ids[0], "quantity_returned": 1}]},
    ]
    for body in bad:
        r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"], json=body)
        assert r.status_code == 422, (body, r.text)
    assert await _qty(session, auth, parcel) == 4


async def test_return_line_sharing_a_lot_is_refused(client, session, auth):
    lot = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order", [
        {"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0},
        {"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0},
    ])
    ids = await _stamp_line_ids(session, auth, po)
    for line_id in ids:
        r = await _post(client, auth, po, {"source_line_id": line_id, "quantity_received": 5, "receive_as": "stock"})
        assert r.status_code == 200, r.text
    r = await _return_lines(client, auth, po, {"line_id": ids[0], "quantity_returned": 1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_line_shared"
    assert await _qty(session, auth, lot) == 20


async def _drop_line_ids(session, auth, doc_id: str) -> None:
    """Store the document's lines without ids, as lines written before line ids were kept
    still are: every line write since gives each line an id."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc_id})
    row.state = {**row.state, "line_items": [
        {k: v for k, v in li.items() if k != "line_id"} for li in row.state["line_items"]]}
    await session.commit()


async def test_legacy_line_without_id_returns_by_position(client, session, auth):
    bill = await _doc(client, auth, "bill", _stock_lines(1, qty=4))
    await _finalize(client, auth, bill)
    await _drop_line_ids(session, auth, bill)
    r = await _post(client, auth, bill, {"po_line_index": 0, "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    r = await _return_lines(client, auth, bill, {"line_index": 0, "quantity_returned": 1})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, parcel) == 3


async def test_return_goods_form_posts_selected_lines(client, session, auth, monkeypatch):
    from ui.routes import documents

    bill, ids = await _issued(client, session, auth, "bill", _stock_lines(2, qty=4))
    r = await _post(client, auth, bill, {"source_line_id": ids[0], "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    html = to_xml(documents._li_bulk_toolbar(bill, False, show_fulfill=True, is_inbound=True,
                                             inbound_line_items=doc["line_items"], locations=[]))
    assert f'hx-delete="/docs/{bill}/receive"' not in html, "Return Goods never undoes the whole receipt"
    form = _ReceiveRows({0, 1}, form_id="li-bulk-revert-btn")
    form.feed(html)
    assert form.confirm
    assert form.qty_inputs == {"qty_0": {"value": "4", "max": "4"}}

    sent: list[dict] = []

    async def return_goods(_tok, entity_id, data):
        sent.append(data)
        return api_client._raise(await client.post(
            f"/docs/{entity_id}/return-items", headers=auth["headers"], json=data)).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "return_goods", return_goods)
    routes = _Routes()
    documents.setup_routes(routes)
    handler = routes.routes[("post", "/docs/{entity_id}/return-goods")]

    resp = await handler(_Request([(n, "1" if n == "qty_0" else v) for n, v in form.fields]), bill)
    assert resp.status_code == 204, resp.body
    assert resp.headers["HX-Redirect"] == f"/docs/{bill}"
    assert json.loads(resp.headers["HX-Trigger"])["celerpToast"]["message"] == "1 line returned to the supplier."
    assert sent[0]["lines"] == [{"line_id": ids[0], "quantity_returned": 1.0}]
    assert sent[0]["idempotency_key"]
    assert await _qty(session, auth, parcel) == 3

    # Too much comes back as the server's message, never a raw payload.
    resp = await handler(_Request([("qty_0", "9"), ("line_id_0", ids[0])]), bill)
    assert "message_key" not in resp.headers["HX-Trigger"]
    assert "on hand" in resp.headers["HX-Trigger"]
    resp = await handler(_Request([]), bill)
    assert "Select at least one line" in resp.headers["HX-Trigger"]


@pytest.mark.parametrize("key", [
    "docs.return_line_not_on_hand", "docs.return_line_shared", "docs.return_line_unknown",
    "docs.return_line_untraced", "docs.return_not_on_hand", "documents.confirm_return_selected",
    "documents.nothing_to_return", "documents.return_nothing_selected",
])
def test_return_copy_in_every_locale(key):
    from ui import i18n
    for code in i18n.available_langs():
        assert i18n.t(key, code) != key, (key, code)
