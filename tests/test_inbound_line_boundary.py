# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Receiving and returning goods work from the document's own lines.

A receipt names a line of the order or bill, and the goods it brings in are the ones that
line is for. A return sends back only goods the document brought in, and no more of them
than it brought in and has not already sent back. Both need the right to receive goods.
"""
from __future__ import annotations

import json
import uuid

import pytest

from celerp.models.company import Company
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_receipt_accounting import _doc, _receive


async def _return(client, auth, doc_id: str, item_id: str, qty):
    return await client.post(f"/docs/{doc_id}/return-items", headers=auth["headers"],
                             json={"items": [{"item_id": item_id, "quantity_returned": qty}]})


async def _received_po(client, session, auth, qty: float = 4) -> tuple[str, str]:
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": item_id, "name": "Lot", "quantity": qty, "unit_price": 14.0}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": item_id, "quantity_received": qty})
    assert r.status_code == 200, r.text
    return po, item_id


async def _stock(session, auth, item_id: str) -> tuple:
    state = await _state(session, auth, item_id)
    return state.get("quantity"), state.get("cost_base"), state.get("cost_total")


@pytest.mark.asyncio
async def test_receiving_and_returning_need_the_right_to_receive_goods(client, session, auth):
    po, item_id = await _received_po(client, session, auth, qty=2)
    other = await _doc(client, auth, "purchase_order",
                       [{"item_id": item_id, "name": "Lot", "quantity": 3, "unit_price": 14.0}])
    company = await session.get(Company, auth["company_id"])
    company.settings = {**(company.settings or {}), "role_grants": {"fulfill_documents": ["owner"]}}
    await session.commit()
    before = await _stock(session, auth, item_id)

    r = await _receive(client, auth, other, {"po_line_index": 0, "item_id": item_id, "quantity_received": 3})
    assert r.status_code == 403, r.text
    r = await _return(client, auth, po, item_id, 1)
    assert r.status_code == 403, r.text
    assert await _stock(session, auth, item_id) == before


@pytest.mark.asyncio
async def test_a_receipt_cannot_put_one_line_s_goods_on_another_item(client, session, auth):
    line_item = await _item(client, auth, 100.0, qty=10)
    unrelated = await _item(client, auth, 50.0, qty=5)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": line_item, "name": "Lot", "quantity": 4, "unit_price": 14.0}])
    before = await _stock(session, auth, unrelated)

    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": unrelated, "quantity_received": 4})
    assert r.status_code == 422, r.text
    assert await _stock(session, auth, unrelated) == before
    assert not (await _state(session, auth, po)).get("received_items")


@pytest.mark.asyncio
async def test_a_receipt_cannot_name_a_different_sku_than_its_line(client, session, auth):
    po = await _doc(client, auth, "purchase_order", [
        {"sku": f"NEW-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 4, "unit_price": 14.0},
    ])
    r = await _receive(client, auth, po, {"po_line_index": 0, "sku": f"OTHER-{uuid.uuid4().hex[:6]}",
                                          "name": "Beads", "quantity_received": 4})
    assert r.status_code == 422, r.text
    assert not (await _state(session, auth, po)).get("received_items")


@pytest.mark.asyncio
async def test_a_receipt_takes_the_line_s_kind(client, session, auth):
    bill = await _doc(client, auth, "bill", [
        {"sku": f"SVC-{uuid.uuid4().hex[:6]}", "name": "Freight service", "quantity": 1,
         "unit_price": 30.0, "receive_as": "expense"},
    ])
    r = await client.post(f"/docs/{bill}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    r = await _receive(client, auth, bill, {"po_line_index": 0, "name": "Freight service", "quantity_received": 1})
    assert r.status_code == 422, r.text
    state = await _state(session, auth, bill)
    assert not state.get("received_items") and not state.get("received_item_ids")


@pytest.mark.asyncio
async def test_only_goods_the_document_received_can_be_returned(client, session, auth):
    po, _ = await _received_po(client, session, auth)
    never_received = await _item(client, auth, 50.0, qty=5)
    before = await _stock(session, auth, never_received)

    r = await _return(client, auth, po, never_received, 1)
    assert r.status_code == 422, r.text
    assert await _stock(session, auth, never_received) == before


@pytest.mark.asyncio
async def test_a_return_is_limited_to_what_the_document_received_and_still_holds(client, session, auth):
    po, item_id = await _received_po(client, session, auth, qty=4)
    before = await _stock(session, auth, item_id)

    r = await _return(client, auth, po, item_id, 5)
    assert r.status_code == 422, r.text
    assert await _stock(session, auth, item_id) == before

    r = await _return(client, auth, po, item_id, 3)
    assert r.status_code == 200, r.text
    r = await _return(client, auth, po, item_id, 2)
    assert r.status_code == 422, r.text
    assert (await _stock(session, auth, item_id))[0] == 11


@pytest.mark.parametrize("qty", ["-2", "0", "NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_a_return_quantity_must_be_a_positive_number(client, session, auth, qty):
    po, item_id = await _received_po(client, session, auth)
    before = await _stock(session, auth, item_id)
    body = '{"items": [{"item_id": %s, "quantity_returned": %s}]}' % (json.dumps(item_id), qty)
    r = await client.post(f"/docs/{po}/return-items", content=body,
                          headers={**auth["headers"], "Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert await _stock(session, auth, item_id) == before
    assert not (await _state(session, auth, po)).get("returned_items")


@pytest.mark.asyncio
async def test_a_receipt_into_a_location_that_does_not_exist_is_refused(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": item_id, "name": "Lot", "quantity": 2, "unit_price": 14.0}])
    before = await _stock(session, auth, item_id)

    r = await client.post(f"/docs/{po}/receive", headers=auth["headers"], json={
        "location_id": str(uuid.uuid4()),
        "received_items": [{"po_line_index": 0, "item_id": item_id, "quantity_received": 2}],
    })
    assert r.status_code == 422, r.text
    assert await _stock(session, auth, item_id) == before
    assert not (await _state(session, auth, po)).get("received_items")
