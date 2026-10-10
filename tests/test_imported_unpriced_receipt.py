# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods received on an imported purchase order or bill that no line of it prices.

What such goods cost is not known, so no cost is made up for them: the import is refused
with the same message a receipt gets, and the one-time correction of earlier imports
leaves them unmarked, so no return takes them off their lot at an invented cost.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from test_cost_restatement import _item, _state
from test_imported_document_cutover import PRICE, RET, _OPENING, _check, _legacy, _opening, _repair, _ret, _snapshot

pytestmark = pytest.mark.asyncio


def _stray(lot: str) -> dict:
    return {"item_id": lot, "po_line_index": 7, "quantity_received": 2.0, "receive_as": "stock"}


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
async def test_import_refuses_received_goods_no_line_prices(client, session, auth, doc_type):
    lot, stray = await _item(client, auth, _OPENING, qty=10), await _item(client, auth, _OPENING, qty=10)
    await _opening(client, auth, PRICE * 5, 0.0)
    data = _snapshot(lot, doc_type, 5, 3)
    data["received_items"].append(_stray(stray))
    before = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()
    doc = f"doc:{uuid.uuid4()}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc, "event_type": "doc.created", "source": "test",
        "idempotency_key": uuid.uuid4().hex, "data": data})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    sku = (await _state(session, auth, stray))["sku"]
    assert detail["message_key"] == "docs.unpriced_receipt", detail
    assert detail["params"]["goods"] == sku and detail["message"].startswith(f"{sku}: no line of "), detail
    session.expire_all()
    after = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()
    assert after == before


async def test_correction_leaves_unpriced_goods_unmarked_and_finishes(client, session, auth):
    from celerp.migrations._data_reconcile import get_meta
    from celerp_docs.imported_cutover import CUTOVER_KEY

    lot, stray = await _item(client, auth, _OPENING, qty=10), await _item(client, auth, _OPENING, qty=10)
    bill = await _legacy(client, session, auth, lot, doc_type="bill", qty=5, received=5)
    received = [*(await _state(session, auth, bill))["received_items"], _stray(stray)]
    await emit_event(session, company_id=auth["company_id"], entity_id=bill, entity_type="doc",
                     event_type="doc.updated", data={"fields_changed": {"received_items": {"old": None, "new": received}}},
                     actor_id=auth["user_id"], location_id=None, source="test", idempotency_key=uuid.uuid4().hex,
                     metadata_={})
    await session.commit()
    result = await _repair(session)
    assert result["errored"] == 0 and result["corrected"] == 1, result
    conn = await session.connection()
    assert await conn.run_sync(lambda c: get_meta(c, CUTOVER_KEY)) == "done"
    marks = {x["item_id"]: x for x in (await _state(session, auth, bill))["received_items"]}
    assert marks[lot]["lot_cost_added"] == PRICE * 5
    assert "lot_cost_added" not in marks[stray] and "lot_quantity_added" not in marks[stray]
    await _check(session, auth, [bill], "repaired")
    r = await client.post(RET.format(d=bill), headers=auth["headers"], **_ret(1, stray))
    assert r.status_code == 422, r.text
    assert await _state(session, auth, stray) and float((await _state(session, auth, stray))["quantity"]) == 10.0
