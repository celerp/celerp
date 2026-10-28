# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Two connections meeting on the same company and item never deadlock.

Each test runs one writer up to the point between its two locks, starts the other writer
until it waits, then lets the first go on. When both take their locks in rank order
(the company first, then the shop identity namespace, then item rows) the second simply
waits for the first; a writer that takes a later lock before the company makes PostgreSQL
abort one of them with a deadlock."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from test_import_preflight_invariants import _factory, _seed_member_with_item, _until_blocked_or_done

pytestmark = pytest.mark.asyncio

_PLATFORM = "woocommerce"


async def _paused(real, entered: asyncio.Event, go: asyncio.Event):
    async def wrapper(*args, **kwargs):
        entered.set()
        await go.wait()
        return await real(*args, **kwargs)
    return wrapper


async def _linked_item(factory):
    import celerp_inventory.services as svc

    company_id, user_id, entity_id = await _seed_member_with_item(factory)
    async with factory() as s:
        await svc.set_external_link(s, company_id, entity_id, _PLATFORM,
                                    {"product_id": "77", "variation_id": None, "sku": "HOLD-1",
                                     "sync_enabled": True})
        await s.commit()
    return company_id, user_id, entity_id


async def _both_finish(first: asyncio.Task, second: asyncio.Task) -> None:
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=5)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"one writer failed instead of waiting: {errors!r}"


async def test_a_store_stop_and_a_sale_of_the_same_item_wait_for_each_other(committed_engine, monkeypatch):
    """A WooCommerce stop (link state write) against a person's sale of the same item."""
    import celerp_inventory.services as svc
    from celerp.services.company_lock import lock_projections

    factory = _factory(committed_engine)
    company_id, _user_id, entity_id = await _linked_item(factory)
    entered, go = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(svc, "set_external_link", await _paused(svc.set_external_link, entered, go))

    async with factory() as store, factory() as person:
        async def stop():
            await svc.set_external_link_state(store, company_id, entity_id, _PLATFORM, sync_enabled=False)
            await store.commit()

        async def sale():
            await lock_projections(person, company_id, [entity_id])
            await person.commit()

        first = asyncio.create_task(stop())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(sale())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)


async def test_an_order_hold_and_a_first_sync_link_wait_for_each_other(committed_engine, monkeypatch):
    """An order hold (company lock, then the item's link state) against a first-sync page linking
    the same store product (upsert_external_product)."""
    import celerp.db
    import celerp_inventory.services as svc
    from celerp.services.company_lock import lock_company

    factory = _factory(committed_engine)
    company_id, _user_id, entity_id = await _linked_item(factory)
    monkeypatch.setattr(celerp.db, "SessionLocal", factory)
    entered, go = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(svc, "resolve_external_product",
                        await _paused(svc.resolve_external_product, entered, go))

    async with factory() as person:
        async def link():
            await svc.upsert_external_product(company_id, platform=_PLATFORM, product_id="77",
                                              variation_id=None, sku="HOLD-1", name="Original")

        async def hold():
            await lock_company(person, company_id)
            await svc.set_external_link_state(person, company_id, entity_id, _PLATFORM,
                                              sync_enabled=False, source="person")
            await person.commit()

        first = asyncio.create_task(link())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(hold())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)


async def test_an_attention_list_change_and_a_company_reset_wait_for_each_other(committed_engine, monkeypatch):
    """Resolving an entry on a connector's attention list (sync run row, then the company)
    against a reset of the company (company FOR UPDATE, then its sync runs)."""
    import celerp.connectors.sync_runner as runner
    from celerp.models.sync_run import SyncRun
    from celerp.services.company_lock import lock_company, lock_company_for_deletion

    factory = _factory(committed_engine)
    company_id, _user_id, _entity_id = await _seed_member_with_item(factory)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(SyncRun(company_id=str(company_id), connector=_PLATFORM, entity="orders", direction="inbound",
                      started_at=now, finished_at=now, status="partial",
                      attention_json=json.dumps([{"id": "x"}])))
        await s.commit()
    entered, go = asyncio.Event(), asyncio.Event()
    real = runner._attention_run

    async def attention_run(*args, **kwargs):
        run = await real(*args, **kwargs)
        entered.set()
        await go.wait()
        return run
    monkeypatch.setattr(runner, "_attention_run", attention_run)

    async with factory() as person, factory() as resetter:
        async def resolve():
            await runner.remove_attention_entry(person, str(company_id), _PLATFORM, "orders", "x")
            await lock_company(person, company_id)
            await person.commit()

        async def reset():
            await lock_company_for_deletion(resetter, company_id)
            await resetter.execute(sa.delete(SyncRun).where(SyncRun.company_id == str(company_id)))
            await resetter.commit()

        first = asyncio.create_task(resolve())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(reset())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)
