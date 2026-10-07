# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A write whose rule depends on the item's status is judged by the status it lands on.

A draft's author edits its amounts and cost without the circulating-stock permissions,
a draft leaves only through Make Available, an item returns to draft only from
available, and a draft cannot be reserved or expired. Each of those rules reads the
item's current status, which a concurrent Make Available, Revert to Draft, or status
edit can change. Whichever commits first, the other must be judged against it. Each
race runs on real PostgreSQL across two connections, in both orders, with the first
writer holding its transaction open until the second has started.
"""

from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp_inventory import routes as inventory
from test_document_item_state_races_pg import _race, _refused

pytestmark = pytest.mark.asyncio

_ITEM = "item:x"
# An author: edit_inventory, but neither amounts nor prices once the item circulates.
_AUTHOR = "operator"
_AUTHOR_SETTINGS = {"role_grants": {"edit_inventory_amounts": ["manager", "admin", "owner"]}}


async def _seed(factory, status: str) -> tuple[uuid.UUID, types.SimpleNamespace]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Races", slug=f"races-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await emit_event(
            s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.created",
            data={"sku": "X", "name": "X", "quantity": 1, "sell_by": "piece", "status": status},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


async def _state(engine, company_id) -> dict:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT state::jsonb FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one()


# -- status changes ---------------------------------------------------------------------

def _make_available(s, company_id, user):
    return inventory.bulk_make_available(inventory.MakeAvailableBody(entity_ids=[_ITEM]), company_id=company_id,
                                         _=None, user=user, session=s)


def _revert(s, company_id, user):
    return inventory.bulk_revert_to_draft(inventory.RevertToDraftBody(entity_ids=[_ITEM]), company_id=company_id,
                                          _=None, user=user, role="admin", settings={}, session=s)


def _archive(s, company_id, user):
    return inventory.set_item_status(_ITEM, inventory.StatusBody(new_status="archived"), company_id=company_id,
                                     _=None, user=user, role="admin", settings={}, session=s)


def _patch_archive(s, company_id, user):
    payload = inventory.ItemPatch(fields_changed={"status": {"old": "available", "new": "archived"}})
    return inventory.patch_item(_ITEM, payload, company_id=company_id, _=None, user=user, role="admin",
                                settings={}, session=s)


def _bulk_archive(s, company_id, user):
    return inventory.bulk_set_status(inventory.BulkStatusBody(entity_ids=[_ITEM], status="archived"),
                                     company_id=company_id, _=None, user=user, role="admin", settings={}, session=s)


def _reserve(s, company_id, user):
    return inventory.reserve_item(_ITEM, inventory.ReserveBody(quantity=1), company_id=company_id, _=None,
                                  user=user, session=s)


def _expire(s, company_id, user):
    return inventory.expire_item(_ITEM, company_id=company_id, _=None, user=user, session=s)


def _bulk_expire(s, company_id, user):
    return inventory.bulk_expire(inventory.BulkExpireBody(entity_ids=[_ITEM]), company_id=company_id, _=None,
                                 user=user, session=s)


# -- a draft author's edits -------------------------------------------------------------

def _author_quantity(s, company_id, user):
    payload = inventory.ItemPatch(fields_changed={"quantity": {"old": 1, "new": 5}})
    return inventory.patch_item(_ITEM, payload, company_id=company_id, _=None, user=user, role=_AUTHOR,
                                settings=_AUTHOR_SETTINGS, session=s)


def _author_cost_patch(s, company_id, user):
    payload = inventory.ItemPatch(fields_changed={"cost_price": {"old": None, "new": 7}})
    return inventory.patch_item(_ITEM, payload, company_id=company_id, _=None, user=user, role=_AUTHOR,
                                settings=_AUTHOR_SETTINGS, session=s)


def _author_cost_price(s, company_id, user):
    payload = inventory.PriceBody(price_type="cost_price", new_price=7)
    return inventory.set_item_price(_ITEM, payload, company_id=company_id, user=user, role=_AUTHOR,
                                    settings=_AUTHOR_SETTINGS, session=s)


# cost_price is derived from cost_total at read time; with a quantity of 1 they are equal.
_AUTHOR_EDITS = [
    pytest.param(_author_quantity, "quantity", 5, id="amount-patch"),
    pytest.param(_author_cost_patch, "cost_total", 7, id="cost-patch"),
    pytest.param(_author_cost_price, "cost_total", 7, id="cost-price"),
]


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.parametrize("edit, key, value", _AUTHOR_EDITS)
async def test_an_author_edit_after_make_available_is_judged_as_circulating_stock(committed_engine, edit, key, value):
    factory = _factory(committed_engine)
    company_id, user = await _seed(factory, "draft")

    made, edited = await _race(factory, committed_engine,
                               lambda s: _make_available(s, company_id, user),
                               lambda s: edit(s, company_id, user))

    assert not isinstance(made, BaseException), made
    _refused(edited, 403)
    state = await _state(committed_engine, company_id)
    assert state["status"] == "available"
    assert state.get(key) != value


@pytest.mark.parametrize("edit, key, value", _AUTHOR_EDITS)
async def test_an_author_edit_before_make_available_stands(committed_engine, edit, key, value):
    factory = _factory(committed_engine)
    company_id, user = await _seed(factory, "draft")

    edited, made = await _race(factory, committed_engine,
                               lambda s: edit(s, company_id, user),
                               lambda s: _make_available(s, company_id, user))

    assert not isinstance(edited, BaseException), edited
    assert not isinstance(made, BaseException), made
    state = await _state(committed_engine, company_id)
    assert state["status"] == "available"
    assert state[key] == value


_GENERIC = [
    pytest.param(_archive, id="status"),
    pytest.param(_patch_archive, id="patch"),
    pytest.param(_bulk_archive, id="bulk-status"),
]


@pytest.mark.parametrize("generic", _GENERIC)
async def test_a_generic_status_change_after_revert_to_draft_is_refused(committed_engine, generic):
    factory = _factory(committed_engine)
    company_id, user = await _seed(factory, "available")

    reverted, changed = await _race(factory, committed_engine,
                                    lambda s: _revert(s, company_id, user),
                                    lambda s: generic(s, company_id, user))

    assert not isinstance(reverted, BaseException), reverted
    _refused(changed, 422)
    assert (await _state(committed_engine, company_id))["status"] == "draft"


@pytest.mark.parametrize("generic", _GENERIC)
async def test_revert_to_draft_after_a_generic_status_change_is_refused(committed_engine, generic):
    factory = _factory(committed_engine)
    company_id, user = await _seed(factory, "available")

    changed, reverted = await _race(factory, committed_engine,
                                    lambda s: generic(s, company_id, user),
                                    lambda s: _revert(s, company_id, user))

    assert not isinstance(changed, BaseException), changed
    _refused(reverted, 409)
    assert (await _state(committed_engine, company_id))["status"] == "archived"


@pytest.mark.parametrize("circulate", [
    pytest.param(_reserve, id="reserve"),
    pytest.param(_expire, id="expire"),
    pytest.param(_bulk_expire, id="bulk-expire"),
])
async def test_a_draft_cannot_be_circulated_by_a_change_racing_revert_to_draft(committed_engine, circulate):
    factory = _factory(committed_engine)
    company_id, user = await _seed(factory, "available")

    reverted, circulated = await _race(factory, committed_engine,
                                       lambda s: _revert(s, company_id, user),
                                       lambda s: circulate(s, company_id, user))

    assert not isinstance(reverted, BaseException), reverted
    _refused(circulated, 409)
    assert (await _state(committed_engine, company_id))["status"] == "draft"


async def test_a_generic_status_change_after_make_available_is_judged_on_available(committed_engine):
    factory = _factory(committed_engine)
    company_id, user = await _seed(factory, "draft")

    made, changed = await _race(factory, committed_engine,
                                lambda s: _make_available(s, company_id, user),
                                lambda s: _archive(s, company_id, user))

    assert not isinstance(made, BaseException), made
    assert not isinstance(changed, BaseException), changed
    assert (await _state(committed_engine, company_id))["status"] == "archived"
