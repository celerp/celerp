# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Saving a recipe and deleting an item it names at the same moment settle in one order,
never both half way.

Runs on real PostgreSQL across two connections. The first transaction holds its commit
while the second starts; whichever holds the locks first wins:

- delete first: the recipe save waits, then finds the item gone and is refused;
- recipe first: the Delete waits for the recipe to be saved, then finds the item named
  by it and deletes nothing.

The same holds when the recipe names the item through a sub-assembly: a product's recipe
names sub-assembly S, whose own recipe names X, and S and X are deleted together. A saved
recipe never names an item that was not there when it was saved.

Delete only removes a draft that nothing depends on, so the deleted items are drafts, and
each Delete takes the sub-assembly and its part together.
"""

from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.events.schemas import ComponentSpec, RecipeSpec
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.models.projections import Projection
from celerp_inventory import routes as inventory
from celerp_manufacturing import routes as mfg
from test_mfg_reference_delete_race_pg import _race

pytestmark = pytest.mark.asyncio

_PRODUCT, _SUB, _PART = "item:product", "item:sub", "item:part"


async def _item(s, company_id, user_id, entity_id: str, status: str, recipe: dict | None = None) -> None:
    await emit_event(
        s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.created",
        data={"sku": entity_id.upper(), "name": entity_id, "quantity": 1, "sell_by": "piece", "status": status,
              "cost_price": 1},
        actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
    if recipe:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.recipe.set",
            data={"recipe": recipe}, actor_id=user_id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()))


async def _seed(factory):
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Recipes", slug=f"rec-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await _item(s, company_id, user_id, _PRODUCT, "available")
        await _item(s, company_id, user_id, _PART, "draft")
        await _item(s, company_id, user_id, _SUB, "draft", {
            "output_qty": 1, "components": [{"item_id": _PART, "quantity": 1}], "labor": [], "overhead": []})
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


def _save_naming(component: str):
    def _save(s, company_id, user):
        spec = RecipeSpec(output_qty=1, components=[ComponentSpec(item_id=component, quantity=1)])
        return mfg.set_item_recipe(_PRODUCT, spec, company_id=company_id, user=user, _=None, session=s)
    return _save


def _deleting(*entity_ids: str):
    def _delete(s, company_id, user):
        return inventory.bulk_delete(inventory.BulkDeleteBody(entity_ids=list(entity_ids)), company_id=company_id,
                                     _=None, user=user, session=s)
    return _delete


async def _states(factory, company_id) -> dict:
    async with factory() as s:
        rows = {i: await s.get(Projection, {"company_id": company_id, "entity_id": i}) for i in (_PRODUCT, _SUB, _PART)}
    return {i: (r.state if r is not None else None) for i, r in rows.items()}


_DELETED = (_SUB, _PART)


async def _delete_first(committed_engine, component: str, deleted_ids: tuple[str, ...] = _DELETED):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)

    deleted, saved = await _race(committed_engine, factory, company_id, user, _deleting(*deleted_ids),
                                 _save_naming(component))

    assert deleted == {"deleted": len(deleted_ids)}
    assert isinstance(saved, HTTPException) and saved.status_code == 422, saved
    assert component in str(saved.detail)
    states = await _states(factory, company_id)
    assert all(states[i] is None for i in deleted_ids)
    assert not states[_PRODUCT].get("recipe")


async def _recipe_first(committed_engine, component: str, deleted_ids: tuple[str, ...] = _DELETED):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)

    saved, deleted = await _race(committed_engine, factory, company_id, user, _save_naming(component),
                                 _deleting(*deleted_ids))

    assert not isinstance(saved, HTTPException), saved
    assert isinstance(deleted, HTTPException) and deleted.status_code == 409, deleted
    states = await _states(factory, company_id)
    assert all(states[i] is not None for i in deleted_ids)
    assert [c["item_id"] for c in states[_PRODUCT]["recipe"]["components"]] == [component]


async def test_delete_first_refuses_the_waiting_recipe(committed_engine):
    await _delete_first(committed_engine, _PART)


async def test_recipe_first_is_saved_and_the_delete_waits_then_refuses(committed_engine):
    await _recipe_first(committed_engine, _PART)


async def test_delete_first_refuses_a_recipe_naming_it_through_a_sub_assembly(committed_engine):
    await _delete_first(committed_engine, _SUB)


async def test_recipe_through_a_sub_assembly_first_and_the_delete_refuses(committed_engine):
    await _recipe_first(committed_engine, _SUB)
