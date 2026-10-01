# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Concurrent split, transform, and merge must each compute from committed item state.

Every physical restructure reads its source items, derives quantities and costs from
them, and emits the result. Two requests for the same source must not both read the
state from before the other committed. These tests run two real requests on separately
committed sessions and hold each one after its source read until the other has read too
(or a short timeout passes, when the other is correctly waiting on a lock), so a stale
read is exercised deterministically rather than by scheduling luck.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.accounting_roles import LEGACY_LOT_ACCOUNT_KEY
from celerp.events.engine import emit_event
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

import celerp_inventory.routes as inventory_routes
from celerp_inventory.routes import (
    MergeBody,
    SplitBody,
    SplitChild,
    TransformBody,
    merge_items,
    split_item,
    transform_item,
)


async def _seed_company(factory):
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        # The stock is older than recorded inventory accounts; the company names where it is valued.
        s.add(Company(id=company_id, name="Restructure Co", slug=f"restructure-{company_id.hex[:8]}",
                      settings={LEGACY_LOT_ACCOUNT_KEY: "1130-P"}))
        s.add(User(id=user_id, email=f"race-{user_id.hex[:8]}@restructure.test", name="Race User",
                   auth_hash="x"))
        await s.commit()
    return company_id, user_id, types.SimpleNamespace(id=user_id)


async def _seed_item(factory, company_id, user, sku: str, quantity: float) -> str:
    entity_id = f"item:{uuid.uuid4()}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item",
            event_type="item.created",
            data={"sku": sku, "name": sku, "quantity": quantity, "sell_by": "piece",
                  "cost_total": quantity * 10, "status": "available"},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _cleanup(factory, company_id, user_id) -> None:
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _items(factory, company_id) -> list[dict]:
    async with factory() as s:
        rows = (await s.execute(
            select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
        )).scalars().all()
        return [dict(r.state or {}, entity_id=r.entity_id) for r in rows]


async def _race(factory, monkeypatch, calls) -> list[dict | BaseException]:
    """Run two route calls concurrently, each on its own committed session.

    Every restructure reads the company units right after reading its sources. That
    read is where each call waits for the other to have read too, bounded by a short
    timeout: with correct locking the other call is blocked on a lock and never gets
    there, so the first proceeds and commits alone.
    """
    both_read = asyncio.Barrier(2)
    real_units = inventory_routes._get_company_units

    async def _units_then_wait(session, company_id):
        units = await real_units(session, company_id)
        try:
            await asyncio.wait_for(both_read.wait(), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
            pass
        return units

    monkeypatch.setattr(inventory_routes, "_get_company_units", _units_then_wait)
    results: list[dict | BaseException] = [None, None]

    async def _run(i, call):
        s = factory()
        try:
            results[i] = await call(s)
            await s.commit()
        except Exception as exc:  # noqa: BLE001 - captured for the race assertion
            results[i] = exc
            await s.rollback()
        finally:
            await s.close()

    await asyncio.wait_for(asyncio.gather(*(_run(i, c) for i, c in enumerate(calls))), timeout=30)
    return results


def _route_kwargs(company_id, user, session):
    return {"company_id": company_id, "_": None, "role": "owner", "settings": {}, "user": user,
            "session": session}


def _merge_kwargs(company_id, user, session):
    """A merge reads the company's settings itself, under its locks."""
    return {k: v for k, v in _route_kwargs(company_id, user, session).items() if k != "settings"}


def _split(entity_id, qty, company_id, user):
    return lambda s: split_item(entity_id, SplitBody(children=[SplitChild(quantity=qty)]),
                                **_route_kwargs(company_id, user, s))


@pytest.mark.asyncio
async def test_concurrent_splits_cannot_oversell_parent(_db_engine, monkeypatch):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    try:
        parent_id = await _seed_item(factory, company_id, user, "LOT10", 10)
        results = await _race(factory, monkeypatch, [
            _split(parent_id, 6, company_id, user), _split(parent_id, 6, company_id, user)])
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1, f"exactly one split of 6 from 10 must succeed: {results!r}"
        assert isinstance(failures[0], HTTPException) and failures[0].status_code == 422, repr(failures[0])
        items = await _items(factory, company_id)
        parent = next(i for i in items if i["entity_id"] == parent_id)
        children = [i for i in items if i["entity_id"] != parent_id]
        assert float(parent["quantity"]) == 4
        assert [float(c["quantity"]) for c in children] == [6]
        assert sum(float(i["quantity"]) for i in items) == 10
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_concurrent_splits_that_fit_both_apply(_db_engine, monkeypatch):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    try:
        parent_id = await _seed_item(factory, company_id, user, "LOT10", 10)
        results = await _race(factory, monkeypatch, [
            _split(parent_id, 3, company_id, user), _split(parent_id, 3, company_id, user)])
        assert not [r for r in results if isinstance(r, BaseException)], repr(results)
        items = await _items(factory, company_id)
        parent = next(i for i in items if i["entity_id"] == parent_id)
        children = [i for i in items if i["entity_id"] != parent_id]
        assert float(parent["quantity"]) == 4
        assert sorted(float(c["quantity"]) for c in children) == [3, 3]
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_concurrent_transforms_consume_parent_once(_db_engine, monkeypatch):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    try:
        parent_id = await _seed_item(factory, company_id, user, "RAW", 5)

        def _transform(sku):
            body = TransformBody(child_sku=sku, child_category="Finished", child_sell_by="piece",
                                 child_quantity=5)
            return lambda s: transform_item(parent_id, body, **_route_kwargs(company_id, user, s))

        results = await _race(factory, monkeypatch, [_transform("OUT-A"), _transform("OUT-B")])
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1, f"exactly one transform of one parent must succeed: {results!r}"
        assert isinstance(failures[0], HTTPException) and failures[0].status_code == 404, repr(failures[0])
        items = await _items(factory, company_id)
        parent = next(i for i in items if i["entity_id"] == parent_id)
        assert parent["status"] == "archived"
        assert len([i for i in items if i["entity_id"] != parent_id]) == 1
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_concurrent_merges_consume_sources_once(_db_engine, monkeypatch):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    try:
        a = await _seed_item(factory, company_id, user, "PART", 2)
        b = await _seed_item(factory, company_id, user, "PART", 3)

        def _merge(order):
            body = MergeBody(source_entity_ids=order, target_sku_from=a)
            return lambda s: merge_items(body, **_merge_kwargs(company_id, user, s))

        results = await _race(factory, monkeypatch, [_merge([a, b]), _merge([b, a])])
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1, f"exactly one merge of the same sources must succeed: {results!r}"
        assert isinstance(failures[0], HTTPException) and failures[0].status_code == 409, repr(failures[0])
        items = await _items(factory, company_id)
        assert {i["status"] for i in items if i["entity_id"] in (a, b)} == {"merged"}
        results_created = [i for i in items if i["entity_id"] not in (a, b)]
        assert len(results_created) == 1
        assert float(results_created[0]["quantity"]) == 5
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_merge_rejects_duplicate_source_ids(_db_engine):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    try:
        a = await _seed_item(factory, company_id, user, "PART", 2)
        body = MergeBody(source_entity_ids=[a, a], target_sku_from=a)
        async with factory() as s:
            with pytest.raises(HTTPException) as exc:
                await merge_items(body, **_merge_kwargs(company_id, user, s))
            await s.rollback()
        assert exc.value.status_code == 422
        items = await _items(factory, company_id)
        assert len(items) == 1
        assert items[0]["status"] == "available" and float(items[0]["quantity"]) == 2
    finally:
        await _cleanup(factory, company_id, user_id)
