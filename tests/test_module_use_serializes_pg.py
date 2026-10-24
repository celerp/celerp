# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Work inside a module and turning that module off happen one after the other.

Each test pauses one side at a known point (the work right after it was admitted, or
the change while it holds its locks), starts the other side, waits until that side is
blocked or finished, then lets the first go on. The outcome must be the one a serial
run would produce in some order: the work finished and then the module went off, or
the module went off and the work was refused with nothing written."""

from __future__ import annotations

import asyncio
import importlib
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from test_helpers import mark_running

pytestmark = pytest.mark.asyncio

DOCS = "celerp-docs"


@pytest.fixture
def factory(committed_engine, monkeypatch):
    """Sessions on the test's own database, for the work, the change and admission."""
    made = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session():
        async with made() as s:
            yield s

    monkeypatch.setattr("celerp.modules.boundary.get_session_ctx", _session)
    mark_running(monkeypatch, DOCS)
    return made


async def _company(factory) -> uuid.UUID:
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Module Co", slug=f"module-{cid.hex[:8]}", settings={}))
        await s.commit()
    return cid


async def _write(s, cid) -> None:
    from celerp.events.engine import emit_event
    await emit_event(s, company_id=cid, entity_id=f"contact:{uuid.uuid4()}", entity_type="contact",
                     event_type="crm.contact.created", data={"name": "Written", "contact_type": "both"},
                     actor_id=None, location_id=None, source="test",
                     idempotency_key=f"test:{uuid.uuid4()}", metadata_={})


def _work(factory, cid, admitted: asyncio.Event, release: asyncio.Event):
    """Work inside Documents that writes one record once it is let go."""
    from celerp.modules.boundary import needs_modules

    @needs_modules(DOCS, company=lambda: str(cid))
    async def run():
        admitted.set()
        await release.wait()
        async with factory() as s:
            await _write(s, cid)
            await s.commit()
    return run


async def _disable(factory, cid):
    from celerp.routers.companies import disable_module
    async with factory() as s:
        return await disable_module(DOCS, company_id=cid, session=s)


async def _written(factory, cid) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.event_type == "crm.contact.created"))


async def _docs_on(factory, cid) -> bool:
    from celerp.modules.registry import uses_module
    async with factory() as s:
        return uses_module((await s.get(Company, cid)).settings, DOCS)


async def _blocked(engine, task: asyncio.Task) -> None:
    """Wait until *task*'s database work waits on a lock, or the task has finished."""
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            if (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"))).scalar_one():
                return
        await asyncio.sleep(0.05)
    raise AssertionError("neither blocked nor finished")


async def _holding_module_state(engine) -> None:
    """Wait until turning a module off holds the module-state boundary, which it holds
    while it waits for the company's running work."""
    from celerp.modules.registry import _MODULE_STATE_LOCK_KEY
    key = _MODULE_STATE_LOCK_KEY
    for _ in range(400):
        async with engine.connect() as conn:
            if (await conn.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND mode = 'ExclusiveLock' AND classid = :hi AND objid = :lo"),
                    {"hi": key >> 32, "lo": key & 0xFFFFFFFF})).scalar_one():
                return
        await asyncio.sleep(0.02)
    raise AssertionError("turning the module off never held the module-state boundary")


def _pause(monkeypatch, target: str, paused: asyncio.Event, release: asyncio.Event) -> None:
    """Hold the first call of *target* until released, then run it."""
    owner_name, name = target.rsplit(".", 1)
    owner = importlib.import_module(owner_name)
    real = getattr(owner, name)

    async def held(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(owner, name, held)


async def _no_running_work(company_id, pkg_name):
    """Turning a module off in another process, which sees none of this one's work."""


async def test_work_admitted_first_finishes_before_the_module_goes_off(factory, committed_engine):
    """Serial order: the work, then turning Documents off. The change waits for the
    work, the work writes, and only then is the module off."""
    cid = await _company(factory)
    admitted, release = asyncio.Event(), asyncio.Event()
    work = asyncio.create_task(_work(factory, cid, admitted, release)())
    await asyncio.wait_for(admitted.wait(), 10)

    change = asyncio.create_task(_disable(factory, cid))
    await _holding_module_state(committed_engine)
    await asyncio.sleep(0.2)  # a change that did not wait would have committed by now
    assert not change.done() and await _docs_on(factory, cid)

    release.set()
    await asyncio.wait_for(work, 30)
    assert (await asyncio.wait_for(change, 30))["enabled"] is False
    assert await _written(factory, cid) == 1
    assert not await _docs_on(factory, cid)


async def test_work_admitted_while_the_module_goes_off_is_refused_with_nothing_written(
        factory, committed_engine, monkeypatch):
    """Serial order: turning Documents off, then the work. Work that asks to start while
    the change holds its locks waits for it, then is refused before it does anything."""
    from celerp.modules.boundary import ModuleOff
    cid = await _company(factory)
    held, let_go = asyncio.Event(), asyncio.Event()
    _pause(monkeypatch, "celerp.modules.boundary.settle_module_use", held, let_go)
    change = asyncio.create_task(_disable(factory, cid))
    await asyncio.wait_for(held.wait(), 10)

    admitted, release = asyncio.Event(), asyncio.Event()
    release.set()
    work = asyncio.create_task(_work(factory, cid, admitted, release)())
    await _blocked(committed_engine, work)
    assert not work.done()

    let_go.set()
    assert (await asyncio.wait_for(change, 30))["enabled"] is False
    with pytest.raises(ModuleOff) as refused:
        await asyncio.wait_for(work, 30)
    assert refused.value.detail["message_key"] == "modules.off_for_company"
    assert not admitted.is_set()
    assert await _written(factory, cid) == 0


async def test_a_change_committed_elsewhere_after_admission_refuses_the_write(factory, monkeypatch):
    """Another Celerp process (which cannot see this one's running work) turns Documents
    off after the work was admitted here: the work's write is refused and nothing is
    written, and the work ends refused even though it caught the refusal and went on."""
    from celerp.modules.boundary import ModuleOff, needs_modules
    cid = await _company(factory)
    monkeypatch.setattr("celerp.modules.boundary.settle_module_use", _no_running_work)
    admitted, release = asyncio.Event(), asyncio.Event()
    caught = []

    @needs_modules(DOCS, company=lambda: str(cid))
    async def run():
        admitted.set()
        await release.wait()
        async with factory() as s:
            try:
                await _write(s, cid)
            except ModuleOff as exc:
                caught.append(exc)
            await s.rollback()

    work = asyncio.create_task(run())
    await asyncio.wait_for(admitted.wait(), 10)
    assert (await _disable(factory, cid))["enabled"] is False

    release.set()
    with pytest.raises(ModuleOff):
        await asyncio.wait_for(work, 30)
    assert len(caught) == 1
    assert await _written(factory, cid) == 0


async def test_a_write_waits_for_a_change_part_way_through_elsewhere(factory, committed_engine, monkeypatch):
    """Another process is turning Documents off and holds the company's module settings
    when the work writes: the write waits for that change, then is refused."""
    from celerp.modules.boundary import ModuleOff
    cid = await _company(factory)
    monkeypatch.setattr("celerp.modules.boundary.settle_module_use", _no_running_work)
    admitted, release = asyncio.Event(), asyncio.Event()
    work = asyncio.create_task(_work(factory, cid, admitted, release)())
    await asyncio.wait_for(admitted.wait(), 10)

    held, let_go = asyncio.Event(), asyncio.Event()
    _pause(monkeypatch, "celerp.routers.companies.locked_company", held, let_go)
    change = asyncio.create_task(_disable(factory, cid))
    await asyncio.wait_for(held.wait(), 10)

    release.set()
    await _blocked(committed_engine, work)
    assert not work.done()

    let_go.set()
    assert (await asyncio.wait_for(change, 30))["enabled"] is False
    with pytest.raises(ModuleOff):
        await asyncio.wait_for(work, 30)
    assert await _written(factory, cid) == 0


async def test_turning_a_module_off_while_its_work_runs_too_long_is_refused(factory, monkeypatch):
    """Work that does not finish in time: the change is refused with a message to try
    again, the module stays on and the work finishes as admitted."""
    from celerp.modules import boundary
    cid = await _company(factory)
    monkeypatch.setattr(boundary, "SETTLE_SECONDS", 0.3)
    admitted, release = asyncio.Event(), asyncio.Event()
    work = asyncio.create_task(_work(factory, cid, admitted, release)())
    await asyncio.wait_for(admitted.wait(), 10)

    with pytest.raises(HTTPException) as busy:
        await _disable(factory, cid)
    assert busy.value.status_code == 409
    assert busy.value.detail["message_key"] == "modules.turn_off_busy"
    assert busy.value.detail["params"] == {"module": "Documents"}

    release.set()
    await asyncio.wait_for(work, 30)
    assert await _written(factory, cid) == 1
    assert await _docs_on(factory, cid)
