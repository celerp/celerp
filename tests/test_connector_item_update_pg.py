# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""An item is born once; everything after is a change to it.

An accounting connector re-importing a product it already brought in changes that
item: the change follows the same rules as any edit, and it never brings back an
item deleted in the meantime. A second creation of an item that already exists is
refused rather than written over it. The races run on real PostgreSQL across two
connections, in both orders, with the first writer holding its transaction open
until the second has started.
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import celerp.db
from celerp.events.engine import emit_event
from celerp.models.company import Company, Location
from celerp_inventory import routes as inventory
from celerp_inventory import services
from test_document_item_state_races_pg import _race, _refused

pytestmark = pytest.mark.asyncio

_KEY = "xero:item:abc"
_ITEM = f"item:{_KEY}"


class _Lent:
    """A session handed to code that opens its own, left open for the caller to commit."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


async def _sync(session, company_id, **fields):
    product = types.SimpleNamespace(
        sku="X", name="Ring", description=None, sale_price=10.0, quantity=None, cost_price=4.0,
        idempotency_key=_KEY,
    )
    for key, value in fields.items():
        setattr(product, key, value)
    with patch.object(celerp.db, "SessionLocal", lambda: _Lent(session)):
        return await services.upsert_from_connector(company_id, product)


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Sync", slug=f"sync-{company_id.hex[:8]}", settings={}))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        assert await _sync(s, company_id) == "created"
        await s.commit()
    return company_id


async def _state(engine, company_id) -> dict | None:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT state::jsonb FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one_or_none()


async def _events(engine, company_id) -> list[tuple[str, dict]]:
    async with engine.connect() as conn:
        return [(t, d) for t, d in (await conn.execute(text(
            "SELECT event_type, data::jsonb FROM ledger WHERE company_id = :c AND entity_id = :e ORDER BY id"),
            {"c": company_id, "e": _ITEM})).all()]


def _delete(s, company_id):
    return inventory.bulk_delete(inventory.BulkDeleteBody(entity_ids=[_ITEM]), company_id=company_id, _=None,
                                 user=None, session=s)


def _birth(entity_id: str, name: str):
    async def emit(s, company_id):
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.created",
            data={"sku": "Y", "name": name, "quantity": 1, "sell_by": "piece"},
            actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return emit


# -- connector update vs Hard Delete -----------------------------------------------------

async def test_a_hard_delete_after_a_connector_update_removes_the_item(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)

    synced, deleted = await _race(factory, committed_engine,
                                  lambda s: _sync(s, company_id, name="Ring v2"),
                                  lambda s: _delete(s, company_id))

    assert synced == "updated"
    assert not isinstance(deleted, BaseException), deleted
    assert await _state(committed_engine, company_id) is None
    assert await _events(committed_engine, company_id) == []


async def test_a_connector_update_after_a_hard_delete_does_not_bring_the_item_back(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)

    deleted, synced = await _race(factory, committed_engine,
                                  lambda s: _delete(s, company_id),
                                  lambda s: _sync(s, company_id, name="Ring v2"))

    assert not isinstance(deleted, BaseException), deleted
    _refused(synced, 404)
    assert await _state(committed_engine, company_id) is None
    assert await _events(committed_engine, company_id) == []


async def test_the_next_sync_after_a_hard_delete_creates_the_item_again(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)
    async with factory() as s:
        await _delete(s, company_id)
    async with factory() as s:
        assert await _sync(s, company_id, name="Ring v2") == "created"
        await s.commit()

    assert (await _state(committed_engine, company_id))["name"] == "Ring v2"


# -- a re-import is an update -----------------------------------------------------------

async def test_a_changed_reimport_records_only_what_changed_as_an_update(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)
    async with factory() as s:
        assert await _sync(s, company_id, sku="X2", cost_price=6.0) == "updated"
        await s.commit()

    event_type, data = (await _events(committed_engine, company_id))[-1]
    assert event_type == "item.updated"
    assert set(data["fields_changed"]) == {"sku", "cost_price"}
    state = await _state(committed_engine, company_id)
    assert state["sku"] == "X2"
    # A renamed SKU still finds the item by its old code, as any SKU edit does.
    assert state["_catalog_sku_aliases"] == ["X"]
    assert state["name"] == "Ring"


async def test_an_unchanged_reimport_writes_nothing(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)
    async with factory() as s:
        assert await _sync(s, company_id, description="") == "noop"
        await s.commit()

    assert [t for t, _ in await _events(committed_engine, company_id)] == ["item.created"]


# -- a second birth ---------------------------------------------------------------------

async def test_creating_an_item_that_already_exists_is_refused(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)

    async with factory() as s:
        with pytest.raises(HTTPException) as refused:
            await _birth(_ITEM, "Overwritten")(s, company_id)
        await s.rollback()

    assert refused.value.status_code == 409
    assert (await _state(committed_engine, company_id))["name"] == "Ring"


async def test_of_two_concurrent_creations_of_one_item_only_the_first_lands(committed_engine):
    factory = _factory(committed_engine)
    company_id = await _seed(factory)
    entity_id = f"item:{uuid.uuid4()}"

    first, second = await _race(factory, committed_engine,
                                lambda s: _birth(entity_id, "First")(s, company_id),
                                lambda s: _birth(entity_id, "Second")(s, company_id))

    assert not isinstance(first, BaseException), first
    _refused(second, 409)
    async with committed_engine.connect() as conn:
        name = (await conn.execute(text(
            "SELECT state::jsonb ->> 'name' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": entity_id})).scalar_one()
    assert name == "First"
