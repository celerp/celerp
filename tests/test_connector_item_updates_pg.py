# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A connector re-import changes an existing item the way an edit does.

Only a record the connector has never seen creates an item. A re-import of a known
record is an update of the item it created: it names the fields that changed, a new
SKU is handled as a SKU edit, and an item deleted while the re-import waited stays
deleted. A second creation of an item that already exists is refused outright, while
rebuilding from history replays every recorded event as it always has.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from migration_support import auth, maker
from test_item_births_pg import _delete, _left_behind
from test_posting_roles_race_pg_draft import _books, _company, _draft, _ok, _race, race  # noqa: F401  (race is a fixture)

pytestmark = pytest.mark.asyncio


def _record(key: str, **fields) -> SimpleNamespace:
    """A record from an accounting system. It carries a cost, so it arrives as a draft,
    which can be deleted."""
    base = {"sku": "QB-1", "name": "Widget", "idempotency_key": key, "sale_price": None,
            "quantity": None, "cost_price": 5.0, "description": None}
    return SimpleNamespace(**(base | fields))


@pytest.fixture
def connector(committed_engine, race, monkeypatch):
    """Runs upsert_from_connector on the test database; ``held`` holds its commit."""
    import celerp.db

    _, hold = race
    sessions = maker(committed_engine)

    def _local():
        s = sessions()
        hold.wrap(s)
        return s

    monkeypatch.setattr(celerp.db, "SessionLocal", _local)

    async def upsert(cid, record):
        from celerp_inventory.services import upsert_from_connector
        try:
            return await upsert_from_connector(str(cid), record)
        except HTTPException as exc:
            return exc

    return upsert


async def _events(engine, cid, lot: str) -> list:
    from celerp.models.ledger import LedgerEntry

    async with maker(engine)() as s:
        return list((await s.scalars(select(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.entity_id == lot).order_by(LedgerEntry.id))).all())


async def _state(engine, cid, lot: str) -> dict | None:
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        row = await s.get(Projection, {"company_id": cid, "entity_id": lot})
        return None if row is None else row.state


async def test_a_reimport_waiting_on_a_delete_leaves_the_item_deleted(committed_engine, race, connector):
    client, hold = race
    cid, tok = await _company(committed_engine)
    assert await connector(cid, _record("qb:1")) == "created"
    lot = "item:qb:1"

    async def reimport():
        return await connector(cid, _record("qb:1", name="Widget renamed"))

    deleted, late = await _race(committed_engine, client, hold, _delete(client, tok, lot), reimport)

    assert deleted.status_code == 200, deleted.text
    assert isinstance(late, HTTPException) and late.status_code == 404, late
    assert await _left_behind(committed_engine, cid, lot) == (False, 0)


async def test_a_delete_waiting_on_a_reimport_still_deletes_the_item(committed_engine, race, connector):
    client, hold = race
    cid, tok = await _company(committed_engine)
    assert await connector(cid, _record("qb:2")) == "created"
    lot = "item:qb:2"

    async def reimport():
        return await connector(cid, _record("qb:2", name="Widget renamed"))

    updated, deleted = await _race(committed_engine, client, hold, reimport, _delete(client, tok, lot))

    assert updated == "updated", updated
    assert deleted.status_code == 200, deleted.text
    assert await _left_behind(committed_engine, cid, lot) == (False, 0)


@pytest.mark.parametrize("name", ["Widget", "Widget renamed"])
async def test_an_item_deleted_before_a_reimport_stays_deleted(committed_engine, race, connector, name):
    client, _ = race
    cid, tok = await _company(committed_engine)
    assert await connector(cid, _record("qb:3")) == "created"
    lot = "item:qb:3"
    assert (await _delete(client, tok, lot)()).status_code == 200

    assert await connector(cid, _record("qb:3", name=name)) == "noop"
    assert await _left_behind(committed_engine, cid, lot) == (False, 0)
    assert await connector(cid, _record("qb:4")) == "created"  # a record never deleted still arrives


async def test_a_changed_reimport_is_recorded_as_an_update_of_the_changed_fields(committed_engine, race, connector):
    cid, _ = await _company(committed_engine)
    assert await connector(cid, _record("qb:3")) == "created"
    lot = "item:qb:3"

    assert await connector(cid, _record("qb:3", sku="QB-1B", name="Widget")) == "updated"
    assert await connector(cid, _record("qb:3", sku="QB-1B", name="Widget")) == "noop"

    events = await _events(committed_engine, cid, lot)
    assert [e.event_type for e in events] == ["item.created", "item.updated"]
    assert events[1].data["fields_changed"] == {"sku": {"old": "QB-1", "new": "QB-1B"}}
    state = await _state(committed_engine, cid, lot)
    assert state["sku"] == "QB-1B" and state["status"] == "draft"


async def test_a_reimported_sku_change_keeps_the_items_lots_in_its_family(committed_engine, race, connector):
    """A SKU edit records which lots belong to the product before its SKU changes; a
    re-import that changes the SKU does the same."""
    client, _ = race
    cid, tok = await _company(committed_engine)
    assert await connector(cid, _record("qb:4", sku="FAM-1")) == "created"
    member = await _draft(client, tok, 10, sku="FAM-1")
    r = await client.patch(f"/items/{member}", headers=auth(tok), json={
        "fields_changed": {"barcode": {"old": None, "new": "40012345"}}})
    assert r.status_code == 200, r.text

    assert await connector(cid, _record("qb:4", sku="FAM-2")) == "updated"

    assert (await _state(committed_engine, cid, member)).get("catalog_item_id") == "item:qb:4"


@pytest.mark.parametrize("new_cost", [15.0, 4.0], ids=["up", "down"])
async def test_a_reimported_cost_on_available_stock_is_booked(committed_engine, race, connector, new_cost):
    """A store re-import that changes the cost of stock already made available books
    the difference on the lot's inventory account, like a cost edit."""
    client, _ = race
    cid, tok = await _company(committed_engine)
    assert await connector(cid, _record("qb:5", quantity=2, cost_price=10.0)) == "created"
    lot = "item:qb:5"
    await _ok(client, tok, "/items/bulk/make-available", {"entity_ids": [lot]})
    assert sum((await _books(committed_engine, cid)).values()) == 20

    assert await connector(cid, _record("qb:5", quantity=2, cost_price=new_cost)) == "updated"

    assert sum((await _books(committed_engine, cid)).values()) == 2 * new_cost
    r = await client.get("/accounting/balance-sheet", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert sum((await _books(committed_engine, cid)).values()) == 2 * new_cost


async def test_creating_an_item_that_already_exists_is_refused(committed_engine, race):
    from celerp.events.engine import emit_event

    client, _ = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 10)
    before = await _state(committed_engine, cid, lot)
    history = [e.id for e in await _events(committed_engine, cid, lot)]

    for birth in ("item.created", "item.snapshot"):
        async with maker(committed_engine)() as s:
            with pytest.raises(HTTPException) as refused:
                await emit_event(s, company_id=cid, entity_id=lot, entity_type="item", event_type=birth,
                                 data={"sku": "OTHER", "name": "Overwritten", "status": "available"},
                                 actor_id=None, location_id=None, source="api",
                                 idempotency_key=str(uuid.uuid4()), metadata_={})
            await s.commit()
        assert refused.value.status_code == 409

    assert await _state(committed_engine, cid, lot) == before
    assert [e.id for e in await _events(committed_engine, cid, lot)] == history


async def test_a_rebuild_replays_a_recorded_second_creation(committed_engine, race):
    """History written before second creations were refused still replays exactly."""
    from celerp.models.ledger import LedgerEntry
    from celerp.projections.engine import ProjectionEngine

    cid, _ = await _company(committed_engine)
    lot = f"item:{uuid.uuid4()}"
    async with maker(committed_engine)() as s:
        for n, data in enumerate(({"sku": "OLD", "name": "First", "status": "draft"},
                                  {"sku": "NEW", "name": "Second"})):
            s.add(LedgerEntry(company_id=cid, entity_id=lot, entity_type="item", event_type="item.created",
                              data=data, actor_id=None, location_id=None, source="connector",
                              idempotency_key=f"legacy:{lot}:{n}", metadata_={}))
        await s.commit()
    async with maker(committed_engine)() as s:
        await ProjectionEngine.rebuild(s, cid)
        await s.commit()

    state = await _state(committed_engine, cid, lot)
    assert (state["sku"], state["name"], state["status"]) == ("NEW", "Second", "draft")


async def test_an_import_row_creating_an_existing_item_is_rejected(committed_engine, race):
    client, _ = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 10)
    before = await _state(committed_engine, cid, lot)

    r = await client.post("/items/import/batch", headers=auth(tok), json={"records": [{
        "entity_id": lot, "event_type": "item.created", "source": "csv", "idempotency_key": str(uuid.uuid4()),
        "data": {"sku": "OTHER", "name": "Overwritten", "quantity": 5, "sell_by": "piece"}}]})

    assert r.status_code == 200, r.text
    assert "already exists" in r.text, r.text
    assert await _state(committed_engine, cid, lot) == before
