# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The business type, the company settings it lives in, and the demo inventory it
drives are written one at a time: a business-type change never interleaves with a
sample reload, a first real import, or another settings write.

Each test pauses the first writer partway, starts the other writer, waits until that
writer is blocked on a lock or has finished, then lets the first writer finish. The
final state must be the one a serial run would produce in either order.
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


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


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


async def _stored_settings(factory, company_id) -> dict:
    async with factory() as s:
        return (await s.get(Company, company_id)).settings


def _hold_first_call(monkeypatch, owner, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the first call of ``owner.<name>`` (the first writer's) until released."""
    paused, release = asyncio.Event(), asyncio.Event()
    real = getattr(owner, name)

    async def _held(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(owner, name, _held)
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


async def _race(engine, hold, first, other) -> None:
    """Run ``first`` until ``hold(session)`` pauses it and run ``other`` meanwhile.

    Both must succeed: a lost race surfaces either as the wrong final state or as a
    failed writer (for example a deadlock abort)."""
    factory = _factory(engine)
    async with factory() as s_first, factory() as s_other:
        paused, release = hold(s_first)
        task = asyncio.create_task(first(s_first))
        await asyncio.wait_for(paused.wait(), timeout=10)
        writer = asyncio.create_task(other(s_other))
        await _until_blocked_or_done(engine, writer)
        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(task, writer, return_exceptions=True), timeout=30)
    failures = {name: o for name, o in zip(("first writer", "other writer"), outcomes)
                if isinstance(o, BaseException)}
    assert not failures, failures


def _reload(company_id, user_id):
    from celerp.routers.companies import reseed_demo_items

    user = types.SimpleNamespace(id=user_id)
    return lambda s: reseed_demo_items(company_id=company_id, user=user, session=s)


def _type_change(company_id, user_id):
    from celerp.services.business_type import set_business_type

    return lambda s: set_business_type(s, company_id, user_id, _NEXT)


async def _assert_type_and_samples_agree(factory, company_id) -> None:
    stored = (await _stored_settings(factory, company_id))["vertical"]
    assert stored == _NEXT
    assert await _demo_skus(factory, company_id) == _skus(stored)


async def test_reload_and_type_change_leave_samples_of_the_stored_type(committed_engine, monkeypatch):
    factory = _factory(committed_engine)
    company_id, user_id = await _seed(factory)
    assert await _demo_skus(factory, company_id) == _skus(_START)

    await _race(
        committed_engine, lambda s: _hold_first_call(monkeypatch, demo, "replace_demo_items"),
        _reload(company_id, user_id), _type_change(company_id, user_id),
    )

    await _assert_type_and_samples_agree(factory, company_id)


async def test_reload_never_brings_samples_back_after_first_import(committed_engine, monkeypatch):
    from celerp_inventory.services import import_items

    factory = _factory(committed_engine)
    company_id, user_id = await _seed(factory)

    async def _import(s):
        result = await import_items(
            s, company_id, user_id, "owner", {},
            [{"name": "Real item", "sku": "REAL-1", "sell_by": "piece", "pieces": "1"}],
            upsert=False, filename="stock.csv", idempotency_key="first-import",
        )
        assert result.created == 1, result

    await _race(
        committed_engine, lambda s: _hold_first_call(monkeypatch, demo, "seed_demo_items"),
        _reload(company_id, user_id), _import,
    )

    assert await _demo_skus(factory, company_id) == set()
    async with factory() as s:
        skus = set((await s.execute(select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id, Projection.entity_type == "item",
        ))).scalars().all())
    assert skus == {"REAL-1"}


async def test_settings_save_never_reverts_a_type_change(committed_engine, monkeypatch):
    from celerp.routers.companies import CompanyPatch, patch_me

    factory = _factory(committed_engine)
    company_id, user_id = await _seed(factory)

    await _race(
        committed_engine, lambda s: _hold_first_call(monkeypatch, s, "commit"),
        lambda s: patch_me(payload=CompanyPatch(settings={"note_color": "blue"}),
                           company_id=company_id, _=None, session=s),
        _type_change(company_id, user_id),
    )

    assert (await _stored_settings(factory, company_id))["note_color"] == "blue"
    await _assert_type_and_samples_agree(factory, company_id)


@pytest.mark.parametrize("path, arg", [("/me/apply-preset", {"vertical": _START}),
                                       ("/me/apply-category", {"name": "book"})])
async def test_category_library_apply_never_reverts_a_type_change(committed_engine, monkeypatch, path, arg):
    from celerp_verticals.routes import _build_router

    endpoint = next(r.endpoint for r in _build_router().routes if r.path == path)
    factory = _factory(committed_engine)
    company_id, user_id = await _seed(factory)

    await _race(
        committed_engine, lambda s: _hold_first_call(monkeypatch, s, "commit"),
        lambda s: endpoint(**arg, company_id=company_id, session=s),
        _type_change(company_id, user_id),
    )

    await _assert_type_and_samples_agree(factory, company_id)
