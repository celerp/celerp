# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A location reference resolves in the caller's company, at every door that writes one.

Another company's location id is refused with location.not_found and nothing is
written: the audit route, list import (single and batch), item import, item transfer
(single and bulk), an item edit, the transfer list's move action, a manufacturing run, and
the event boundary itself for writers with no check of their own (scanning, connectors).
The neighbour tests show the caller's own location still works at the same doors.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.company import Company, Location
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

KEY = "location.not_found"


async def _foreign_location(session) -> str:
    other = uuid.uuid4()
    session.add(Company(id=other, name="OtherCo", slug=f"other-{other.hex[:8]}", settings={"currency": "USD"}))
    await session.flush()
    loc = Location(id=uuid.uuid4(), company_id=other, name="Their shelf", type="warehouse")
    session.add(loc)
    await session.commit()
    return str(loc.id)


async def _own_location(client, auth, name="Shelf") -> str:
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, auth, loc: str, sku: str | None = None) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku or f"L-{uuid.uuid4().hex[:6]}", "name": "Stone", "quantity": 1, "sell_by": "piece",
        "status": "available", "location_id": loc})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _events(session, auth, **where) -> int:
    q = select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == auth["company_id"])
    for col, val in where.items():
        q = q.where(getattr(LedgerEntry, col) == val)
    session.expire_all()
    return (await session.execute(q)).scalar_one()


def _keyed(r):
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == KEY, detail
    return detail


async def _item_location(session, auth, item_id) -> str | None:
    session.expire_all()
    row = await session.get(Projection, (auth["company_id"], item_id))
    return str(row.location_id) if row.location_id else None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

async def test_audit_for_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    before = await _events(session, auth, entity_type="list")
    _keyed(await client.post("/lists/audit", headers=auth["headers"], json={"location_id": foreign}))
    assert await _events(session, auth, entity_type="list") == before


async def test_audit_with_a_malformed_location_is_refused(client, session, auth):
    before = await _events(session, auth, entity_type="list")
    _keyed(await client.post("/lists/audit", headers=auth["headers"], json={"location_id": "not-a-location"}))
    assert await _events(session, auth, entity_type="list") == before


async def test_audit_for_own_location_is_created(client, session, auth):
    """Neighbour: the company's own location still makes an audit of its items."""
    loc = await _own_location(client, auth)
    await _item(client, auth, loc)
    r = await client.post("/lists/audit", headers=auth["headers"], json={"location_id": loc})
    assert r.status_code == 200, r.text
    assert r.json()["line_count"] == 1


# ---------------------------------------------------------------------------
# List import
# ---------------------------------------------------------------------------

def _list_record(n: str, loc: str) -> dict:
    return {"entity_id": f"list:IMP-{n}", "event_type": "list.created", "source": "csv",
            "idempotency_key": f"list-imp-{n}",
            "data": {"ref_id": f"IMP-{n}", "list_type": "audit", "status": "draft",
                     "location_id": loc, "line_items": []}}


async def test_list_import_naming_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    _keyed(await client.post("/lists/import", headers=auth["headers"], json=_list_record("1", foreign)))
    assert await _events(session, auth, entity_type="list") == 0


async def test_list_batch_import_naming_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    r = await client.post("/lists/import/batch", headers=auth["headers"], json={"records": [
        _list_record("2", foreign), _list_record("3", own)]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1, body
    assert any("list:IMP-2" in e and "location" in e.lower() for e in body["errors"]), body
    session.expire_all()
    assert await session.get(Projection, (auth["company_id"], "list:IMP-2")) is None
    assert await session.get(Projection, (auth["company_id"], "list:IMP-3")) is not None


# ---------------------------------------------------------------------------
# Item import, transfer, bulk transfer
# ---------------------------------------------------------------------------

async def test_item_import_naming_another_company_location_is_rejected(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": [
        {"entity_id": "item:imp-foreign", "event_type": "item.created", "source": "csv",
         "idempotency_key": "imp-foreign",
         "data": {"sku": "IMP-F", "name": "Imported", "quantity": 1, "sell_by": "piece", "location_id": foreign}},
        {"entity_id": "item:imp-own", "event_type": "item.created", "source": "csv",
         "idempotency_key": "imp-own",
         "data": {"sku": "IMP-O", "name": "Imported", "quantity": 1, "sell_by": "piece", "location_id": own}},
    ]})
    assert r.status_code == 200, r.text
    session.expire_all()
    assert await session.get(Projection, (auth["company_id"], "item:imp-foreign")) is None, r.json()
    assert await _item_location(session, auth, "item:imp-own") == own


async def test_item_transfer_to_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    item = await _item(client, auth, own)
    _keyed(await client.post(f"/items/{item}/transfer", headers=auth["headers"], json={"to_location_id": foreign}))
    assert await _item_location(session, auth, item) == own


async def test_bulk_transfer_to_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    item = await _item(client, auth, own)
    _keyed(await client.post("/items/bulk/transfer", headers=auth["headers"],
                             json={"entity_ids": [item], "to_location_id": foreign}))
    assert await _item_location(session, auth, item) == own


async def test_transfer_to_own_location_moves_the_item(client, session, auth):
    """Neighbour: a transfer between the company's own locations still moves the item."""
    a = await _own_location(client, auth, "A")
    b = await _own_location(client, auth, "B")
    item = await _item(client, auth, a)
    r = await client.post(f"/items/{item}/transfer", headers=auth["headers"], json={"to_location_id": b})
    assert r.status_code == 200, r.text
    assert await _item_location(session, auth, item) == b


# ---------------------------------------------------------------------------
# Transfer list move action
# ---------------------------------------------------------------------------

async def test_transfer_list_move_to_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    item = await _item(client, auth, own)
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "transfer", "line_items": [{"item_id": item, "name": "Stone", "quantity": 1}]})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    r = await client.post(f"/lists/{lid}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    _keyed(await client.post(f"/lists/{lid}/move", headers=auth["headers"], json={"to_location_id": foreign}))
    assert await _item_location(session, auth, item) == own


# ---------------------------------------------------------------------------
# The event boundary every writer passes through
# ---------------------------------------------------------------------------

async def test_event_naming_another_company_location_is_refused_at_the_boundary(client, session, auth):
    """A writer with no check of its own (a connector, a migration) is still refused,
    because emit_event checks every location an event names: its ledger location and
    the location keys of its data."""
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    for location_id, data in (
        (uuid.UUID(foreign), {"to_location_id": own}),
        (None, {"to_location_id": foreign}),
    ):
        with pytest.raises(HTTPException) as exc:
            await emit_event(session, company_id=auth["company_id"], entity_id=f"item:{uuid.uuid4()}",
                             entity_type="item", event_type="item.transferred", data=data,
                             actor_id=auth["user_id"], location_id=location_id, source="api",
                             idempotency_key=str(uuid.uuid4()), metadata_={})
        assert exc.value.status_code == 422 and exc.value.detail["message_key"] == KEY
    await session.rollback()
    assert await _events(session, auth, entity_type="item") == 0


async def test_item_patch_moving_to_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    item = await _item(client, auth, own)
    r = await client.patch(f"/items/{item}", headers=auth["headers"],
                           json={"fields_changed": {"location_id": {"old": own, "new": foreign}}})
    _keyed(r)
    assert await _item_location(session, auth, item) == own


# ---------------------------------------------------------------------------
# Manufacturing
# ---------------------------------------------------------------------------

async def test_manufacturing_run_at_another_company_location_is_refused(client, session, auth):
    foreign = await _foreign_location(session)
    own = await _own_location(client, auth)
    raw = await _item(client, auth, own)
    made = await _item(client, auth, own)
    before = await _events(session, auth, entity_type="mfg_order")
    _keyed(await client.post("/manufacturing", headers=auth["headers"], json={
        "description": "Run", "inputs": [{"item_id": raw, "quantity": 1}],
        "output_item_id": made, "quantity": 1, "location_id": foreign}))
    assert await _events(session, auth, entity_type="mfg_order") == before
