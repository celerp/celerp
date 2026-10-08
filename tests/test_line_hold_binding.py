# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A lot a record holds for one of its lines is that line's stock: no other line of the
record may newly take it, while the line that holds it stays editable."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


async def _lot(client, auth) -> tuple[str, str]:
    sku = f"HOLD-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": sku, "quantity": 10, "sell_by": "piece", "status": "available",
        "allow_splitting": True})
    assert r.status_code == 200, r.text
    return r.json()["id"], sku


def _line(line_id: str, eid: str, sku: str, qty: float = 1) -> dict:
    return {"line_id": line_id, "sku": sku, "name": sku, "quantity": qty, "unit_price": 10.0, "entity_id": eid}


async def _hold(session, auth, eid: str, doc_id: str, line_id: str | None) -> None:
    """The hold reserving writes: the lot reserved to ``doc_id``, attributed to ``line_id``."""
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": eid})
    state = {**row.state, "status": "reserved", "status_doc_id": doc_id}
    if line_id:
        state["status_line_entity_id"] = line_id
    row.state = state
    flag_modified(row, "state")
    await session.flush()


async def _invoice(client, auth, lines) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": lines})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _save(client, auth, doc_id, lines):
    return await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                              json={"fields_changed": {"line_items": {"new": lines}}})


async def test_another_line_cannot_newly_take_a_lot_held_for_a_line(client, session, auth):
    eid, sku = await _lot(client, auth)
    doc_id = await _invoice(client, auth, [_line("L1", eid, sku)])
    await _hold(session, auth, eid, doc_id, "L1")
    r = await _save(client, auth, doc_id, [_line("L1", eid, sku), _line("L2", eid, sku)])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["errors"][0]["message_key"] == "item.held_for_other_line"


async def test_moving_the_reference_to_another_line_is_refused(client, session, auth):
    eid, sku = await _lot(client, auth)
    other, other_sku = await _lot(client, auth)
    doc_id = await _invoice(client, auth, [_line("L1", eid, sku)])
    await _hold(session, auth, eid, doc_id, "L1")
    r = await _save(client, auth, doc_id, [_line("L1", other, other_sku), _line("L2", eid, sku)])
    assert r.status_code == 422, r.text


async def test_the_holding_line_stays_editable(client, session, auth):
    eid, sku = await _lot(client, auth)
    doc_id = await _invoice(client, auth, [_line("L1", eid, sku)])
    await _hold(session, auth, eid, doc_id, "L1")
    r = await _save(client, auth, doc_id, [_line("L1", eid, sku, qty=3)])
    assert r.status_code == 200, r.text


async def test_a_hold_without_line_attribution_is_the_whole_record(client, session, auth):
    eid, sku = await _lot(client, auth)
    doc_id = await _invoice(client, auth, [_line("L1", eid, sku)])
    await _hold(session, auth, eid, doc_id, None)
    r = await _save(client, auth, doc_id, [_line("L1", eid, sku), _line("L2", eid, sku)])
    assert r.status_code == 200, r.text
