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


async def _company(factory, uses: set[str] | None = None) -> uuid.UUID:
    """A company using the installation's modules, or the modules *uses* names."""
    from celerp.modules.registry import set_enabled
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Module Co", slug=f"module-{cid.hex[:8]}",
                      settings=set_enabled({}, uses) if uses else {}))
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


async def _disable(factory, cid, module: str = DOCS):
    from celerp.routers.companies import disable_module
    async with factory() as s:
        return await disable_module(module, company_id=cid, session=s)


async def _written(factory, cid) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.event_type == "crm.contact.created"))


async def _docs_on(factory, cid, module: str = DOCS) -> bool:
    from celerp.modules.registry import uses_module
    async with factory() as s:
        return uses_module((await s.get(Company, cid)).settings, module)


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


async def _holding_gate(engine, cid, module: str = DOCS) -> None:
    """Wait until turning *module* off for *cid* holds that module's gate for the company,
    which it holds while it waits for the company's running work in the module."""
    async with engine.connect() as conn:
        key = (await conn.execute(text("SELECT hashtextextended(:k, 0)"),
                                  {"k": f"module-gate:{cid}:{module}"})).scalar_one() & 0xFFFFFFFFFFFFFFFF
    for _ in range(400):
        async with engine.connect() as conn:
            if (await conn.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND mode = 'ExclusiveLock' AND classid = :hi AND objid = :lo"),
                    {"hi": key >> 32, "lo": key & 0xFFFFFFFF})).scalar_one():
                return
        await asyncio.sleep(0.02)
    raise AssertionError("turning the module off never held its gate for the company")


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
    await _holding_gate(committed_engine, cid)
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
    monkeypatch.setattr(boundary, "MODULE_SETTLE_SECONDS", 0.3)
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


@pytest.fixture
async def request_factory(committed_engine, monkeypatch, factory):
    """Sessions with the request timeouts (lock_timeout 3s), as a request in the app has,
    for admission and for the side that must give up rather than wait."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.db import REQUEST_CONNECT_ARGS
    engine = create_async_engine(committed_engine.url.render_as_string(hide_password=False),
                                 poolclass=NullPool, connect_args=REQUEST_CONNECT_ARGS)
    made = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session():
        async with made() as s:
            yield s

    monkeypatch.setattr("celerp.modules.boundary.get_session_ctx", _session)
    yield made
    await engine.dispose()


async def _settling(factory, committed_engine, cid):
    """Turning Documents off for *cid* while its Documents work runs: returns the held
    work's release, the work and the change, the change waiting on the work."""
    admitted, release = asyncio.Event(), asyncio.Event()
    work = asyncio.create_task(_work(factory, cid, admitted, release)())
    await asyncio.wait_for(admitted.wait(), 10)
    change = asyncio.create_task(_disable(factory, cid))
    await _holding_gate(committed_engine, cid)
    return release, work, change


async def test_a_turn_off_waiting_on_its_work_holds_up_no_other_company(factory, committed_engine):
    """Company A turns Documents off while A's Documents work runs: company B's Documents
    work is admitted and finishes meanwhile, and A's change still waits for A's work."""
    a, b = await _company(factory), await _company(factory)
    release, work, change = await _settling(factory, committed_engine, a)

    other_admitted, other_release = asyncio.Event(), asyncio.Event()
    other_release.set()
    await asyncio.wait_for(_work(factory, b, other_admitted, other_release)(), 5)
    assert await _written(factory, b) == 1
    assert not change.done() and await _docs_on(factory, a)

    release.set()
    await asyncio.wait_for(work, 30)
    assert (await asyncio.wait_for(change, 30))["enabled"] is False
    assert await _written(factory, a) == 1
    assert not await _docs_on(factory, a) and await _docs_on(factory, b)


async def test_turning_another_module_off_never_waits_on_documents_work(factory):
    """Documents work runs for the company: turning Labels off goes ahead at once, and the
    Documents work finishes as admitted."""
    labels = "celerp-labels"
    cid = await _company(factory, {DOCS, "celerp-contacts", "celerp-inventory", labels})
    admitted, release = asyncio.Event(), asyncio.Event()
    work = asyncio.create_task(_work(factory, cid, admitted, release)())
    await asyncio.wait_for(admitted.wait(), 10)

    assert (await asyncio.wait_for(_disable(factory, cid, labels), 5))["enabled"] is False
    assert not await _docs_on(factory, cid, labels) and await _docs_on(factory, cid)

    release.set()
    await asyncio.wait_for(work, 30)
    assert await _written(factory, cid) == 1


async def test_work_asking_to_start_while_its_module_goes_off_is_told_to_retry(
        factory, request_factory, committed_engine):
    """The company's Documents work asks to start while turning Documents off still waits
    on earlier work, for longer than the request lock timeout: it is refused as busy, a
    409 that says to try again, before it does anything, and the module stays on until
    the earlier work is done."""
    from celerp.modules.boundary import ModuleBusy
    cid = await _company(factory)
    release, work, change = await _settling(factory, committed_engine, cid)

    late_admitted, late_release = asyncio.Event(), asyncio.Event()
    late_release.set()
    with pytest.raises(ModuleBusy) as busy:
        await asyncio.wait_for(_work(factory, cid, late_admitted, late_release)(), 15)
    assert busy.value.status_code == 409
    assert busy.value.detail["message_key"] == "modules.use_busy"
    assert busy.value.detail["params"] == {"module": "Documents"}
    assert busy.value.headers["Retry-After"] == "30"
    assert not late_admitted.is_set()
    assert not change.done() and await _docs_on(factory, cid)

    release.set()
    await asyncio.wait_for(work, 30)
    assert (await asyncio.wait_for(change, 30))["enabled"] is False
    assert await _written(factory, cid) == 1


@asynccontextmanager
async def _held_elsewhere(factory, lock, *args):
    """Another process holding *lock* (a registry lock) until the block ends, as a
    company restore holds the module-state boundary."""
    async with factory() as s:
        await lock(s, *args)
        try:
            yield
        finally:
            await s.rollback()


async def test_work_asking_to_start_during_a_long_module_change_is_told_to_retry(factory, request_factory):
    """Another company's long module change (a restore) holds the module-state boundary:
    work asking to start is refused as busy, never a server error, and starts once the
    change is done."""
    from celerp.modules.boundary import ModuleBusy
    from celerp.modules.registry import hold_module_state
    cid = await _company(factory)
    admitted, release = asyncio.Event(), asyncio.Event()
    release.set()
    async with _held_elsewhere(factory, hold_module_state):
        with pytest.raises(ModuleBusy) as busy:
            await asyncio.wait_for(_work(factory, cid, admitted, release)(), 15)
    assert (busy.value.status_code, busy.value.detail["message_key"]) == (409, "modules.use_busy")
    assert not admitted.is_set() and await _written(factory, cid) == 0

    await asyncio.wait_for(_work(factory, cid, admitted, release)(), 15)
    assert await _written(factory, cid) == 1


async def test_turning_a_module_off_during_a_long_module_change_is_told_to_retry(factory, request_factory):
    """A long module change elsewhere holds the module-state boundary: turning Documents
    off answers within the request lock timeout, a 409 that says to try again, and the
    module stays on."""
    from celerp.modules.registry import hold_module_state
    cid = await _company(factory)
    async with _held_elsewhere(factory, hold_module_state):
        with pytest.raises(HTTPException) as busy:
            await asyncio.wait_for(_disable(request_factory, cid), 15)
    assert busy.value.status_code == 409
    assert busy.value.detail["message_key"] == "modules.change_busy"
    assert busy.value.detail["params"] == {"module": "Documents"}
    assert await _docs_on(factory, cid)
    assert (await _disable(request_factory, cid))["enabled"] is False


async def test_a_write_meeting_a_turn_off_part_way_through_for_too_long_is_told_to_retry(
        factory, request_factory):
    """A turn-off elsewhere holds the company's module settings for longer than the request
    lock timeout when admitted work writes: the write is refused as busy with nothing
    written, and the work ends refused even though it caught the refusal and went on."""
    from celerp.modules.boundary import ModuleBusy, needs_modules
    from celerp.modules.registry import hold_company_modules
    cid = await _company(factory)
    admitted, release = asyncio.Event(), asyncio.Event()
    caught = []

    @needs_modules(DOCS, company=lambda: str(cid))
    async def run():
        admitted.set()
        await release.wait()
        async with request_factory() as s:
            try:
                await _write(s, cid)
            except ModuleBusy as exc:
                caught.append(exc)
            await s.rollback()

    work = asyncio.create_task(run())
    await asyncio.wait_for(admitted.wait(), 10)
    async with _held_elsewhere(factory, hold_company_modules, cid):
        release.set()
        with pytest.raises(ModuleBusy):
            await asyncio.wait_for(work, 15)
    assert len(caught) == 1 and caught[0].detail["message_key"] == "modules.use_busy"
    assert await _written(factory, cid) == 0
    assert await _docs_on(factory, cid)
