# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An imported receipt is costed from the line that names the goods it received, whichever
key the line uses (item_id, entity_id or SKU only), and from the indexed line when two lines
name the same goods at different prices."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_books_carry_stock
from test_cost_restatement import _item, _state
from test_imported_document_cutover import _opening

pytestmark = pytest.mark.asyncio


async def _import(client, auth, lines: list[dict], received: list[dict], total: float) -> str:
    doc = f"doc:{uuid.uuid4()}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc, "event_type": "doc.created", "source": "test", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "purchase_order", "contact_id": "supplier:1", "status": "received",
                 "doc_number": f"IMP-{uuid.uuid4().hex[:4]}", "issue_date": "2026-01-01", "subtotal": total,
                 "total": total, "amount_outstanding": total, "amount_paid": 0,
                 "line_items": lines, "received_items": received, "import_treatment": "opening_balances"}})
    assert r.status_code == 200, r.text
    return doc


@pytest.mark.parametrize("key", ["entity_id", "sku"])
async def test_a_receipt_indexed_at_another_items_line_is_costed_from_its_own_line(client, session, auth, key):
    a = await _item(client, auth, 30.0, qty=3)
    b = await _item(client, auth, 150.0, qty=3)
    await _opening(client, auth, 180.0, 0.0)
    ident = {"entity_id": (a, b),
             "sku": ((await _state(session, auth, a))["sku"], (await _state(session, auth, b))["sku"])}[key]
    lines = [{key: ident[0], "name": "A", "quantity": 3, "unit_price": 10},
             {key: ident[1], "name": "B", "quantity": 3, "unit_price": 50}]
    doc = await _import(client, auth, lines,
                        [{"item_id": b, "po_line_index": 0, "quantity_received": 3.0, "receive_as": "stock"}], 180)
    assert (await _state(session, auth, doc))["received_items"][0]["lot_cost_added"] == 150.0
    await assert_books_carry_stock(session, auth["company_id"])


async def test_the_same_goods_on_two_lines_at_two_prices_follow_the_index(client, session, auth):
    b = await _item(client, auth, 180.0, qty=6)
    await _opening(client, auth, 180.0, 0.0)
    lines = [{"item_id": b, "name": "B", "quantity": 3, "unit_price": 10},
             {"item_id": b, "name": "B", "quantity": 3, "unit_price": 50}]
    doc = await _import(client, auth, lines, [
        {"item_id": b, "po_line_index": 1, "quantity_received": 3.0, "receive_as": "stock"},
        {"item_id": b, "po_line_index": 0, "quantity_received": 3.0, "receive_as": "stock"}], 180)
    got = [x["lot_cost_added"] for x in (await _state(session, auth, doc))["received_items"]]
    assert got == [150.0, 30.0], got
