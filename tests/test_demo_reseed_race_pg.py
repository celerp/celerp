# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Reloading the sample items runs one at a time with the other writers of demo
inventory: a business-type change and a first real import.

Each test pauses the reload partway, starts the other writer, waits until that
writer is blocked on a lock or has finished, then lets the reload finish. The final
inventory must be the one a serial run would produce in either order.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company, Location, User
from celerp.models.projections import Projection
from celerp.services import demo

pytestmark = pytest.mark.asyncio

_START, _NEXT = "agricultural", "electronics"


def _skus(vertical: str) -> set[str]:
    return {d["sku"] for d in demo._VERTICAL_ITEMS[vertical]}


async def _seed(factory) -> tuple[uuid.UUID, uuid.UUID]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="SampleCo", slug=f"sample-{company_id.hex[:8]}",
                      settings={"vertical": _START}))
        s.add(User(id=user_id, email=f"owner-{user_id.hex[:8]}@example.test", name="Owner",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main",
                       type="warehouse", is_default=True))
        await s.commit()
    async with factory() as s:
        await demo.seed_demo_items(s, company_id, user_id, vertical=_START)
        await s.commit()
    return company_id, user_id


async def _demo_skus(factory, company_id) -> set[str]:
    async with factory() as s:
        ids = await demo.demo_item_ids(s, company_id)
        rows = (await s.execute(select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id, Projection.entity_id.in_(ids),
        ))).scalars().all()
    return set(rows)


async def _stored_vertical(factory, company_id) -> str:
    async with factory() as s:
        return (await s.get(Company, company_id)).settings["vertical"]


def _pause_first_call(monkeypatch, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the first call of ``demo.<name>`` (the reload's) until released."""
    paused, release = asyncio.Event(), asyncio.Event()
    real = getattr(demo, name)

    async def _held(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(demo, name, _held)
    return paused, release


async def _until_blocked_or_done(engine, task: asyncio.Task) -> None:
    """Wait until the other writer is blocked on a row lock, or has finished.

    pg_stat_activity is a per-transaction snapshot, so every poll opens its own."""
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the other writer neither blocked nor finished")


async def _race_reload_against(engine, monkeypatch, pause_at, company_id, user_id, other) -> None:
    """Pause the reload at its first ``demo.<pause_at>`` call and run ``other`` meanwhile.

    Both must succeed: a lost race surfaces either as the wrong final inventory or
    as a failed writer (for example a deadlock abort)."""
    from celerp.routers.companies import reseed_demo_items

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    paused, release = _pause_first_call(monkeypatch, pause_at)
    user = types.SimpleNamespace(id=user_id)
    async with factory() as s_reload, factory() as s_other:
        reload = asyncio.create_task(
            reseed_demo_items(company_id=company_id, user=user, session=s_reload))
        await asyncio.wait_for(paused.wait(), timeout=10)
        writer = asyncio.create_task(other(s_other))
        await _until_blocked_or_done(engine, writer)
        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(reload, writer, return_exceptions=True), timeout=30)
    failures = {name: o for name, o in zip(("reload", "other writer"), outcomes)
                if isinstance(o, BaseException)}
    assert not failures, failures


async def test_reload_and_type_change_leave_samples_of_the_stored_type(committed_engine, monkeypatch):
    from celerp.services.business_type import set_business_type

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed(factory)
    assert await _demo_skus(factory, company_id) == _skus(_START)

    await _race_reload_against(
        committed_engine, monkeypatch, "replace_demo_items", company_id, user_id,
        lambda s: set_business_type(s, company_id, user_id, _NEXT),
    )

    stored = await _stored_vertical(factory, company_id)
    assert stored == _NEXT
    assert await _demo_skus(factory, company_id) == _skus(stored)


async def test_reload_never_brings_samples_back_after_first_import(committed_engine, monkeypatch):
    from celerp_inventory.services import import_items

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed(factory)

    async def _import(s):
        result = await import_items(
            s, company_id, user_id, "owner", {},
            [{"name": "Real item", "sku": "REAL-1", "sell_by": "piece", "pieces": "1"}],
            upsert=False, filename="stock.csv", idempotency_key="first-import",
        )
        assert result.created == 1, result

    await _race_reload_against(
        committed_engine, monkeypatch, "seed_demo_items", company_id, user_id, _import)

    assert await _demo_skus(factory, company_id) == set()
    async with factory() as s:
        skus = set((await s.execute(select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id, Projection.entity_type == "item",
        ))).scalars().all())
    assert skus == {"REAL-1"}
