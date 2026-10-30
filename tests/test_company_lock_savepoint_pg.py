# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""What the company lock proved holds only as long as PostgreSQL holds the lock.

A lock taken inside a savepoint is released when that savepoint rolls back, so
whatever the lock judged there (the caller's access, the modules the work relies on,
the company loaded for its settings) is no longer protected: another transaction may
change it and commit. The next lock in the same outer transaction must judge again
and refuse cleanly, leaving nothing committed. A lock taken before the savepoint
stays held after the savepoint rolls back, so nothing can have changed and the next
lock must not refuse.

Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.modules.boundary import ModuleOff, module_use
from celerp.modules.registry import set_enabled
from celerp.services.company_lock import lock_company, locked_company
from celerp.services.permissions import authorize_request
from test_helpers import mark_running

pytestmark = pytest.mark.asyncio

DOCS = "celerp-docs"
KEY = "view_contacts"
ROLE = "manager"


@pytest.fixture
def factory(committed_engine, monkeypatch):
    made = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session():
        async with made() as s:
            yield s

    monkeypatch.setattr("celerp.modules.boundary.get_session_ctx", _session)
    mark_running(monkeypatch, DOCS)
    return made


async def _company(factory) -> tuple[uuid.UUID, uuid.UUID]:
    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Savepoint Co", slug=f"sp-{cid.hex[:8]}",
                      settings=set_enabled({}, {DOCS})))
        s.add(User(id=uid, email=f"sp-{uid.hex[:8]}@test.co", name="Member", auth_hash="x", is_active=True))
        await s.flush()
        s.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role=ROLE, is_active=True))
        await s.commit()
    return cid, uid


async def _write(s, cid) -> None:
    from celerp.events.engine import emit_event
    await emit_event(s, company_id=cid, entity_id=f"contact:{uuid.uuid4()}", entity_type="contact",
                     event_type="crm.contact.created", data={"name": "Written", "contact_type": "both"},
                     actor_id=None, location_id=None, source="test",
                     idempotency_key=f"test:{uuid.uuid4()}", metadata_={})


async def _written(factory, cid) -> int:
    async with factory() as s:
        return (await s.execute(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == cid))).scalar_one()


async def _change(factory, cid, uid, change: str) -> None:
    """Commit *change* from another request, the way an owner's edit does: under the
    company lock, in a context of its own (outside any module work of the caller)."""
    task = asyncio.get_running_loop().create_task(
        _change_now(factory, cid, uid, change), context=contextvars.Context())
    await asyncio.wait_for(task, 10)


async def _change_now(factory, cid, uid, change: str) -> None:
    async with factory() as s:
        await s.execute(text("SET LOCAL lock_timeout = '2s'"))
        company = await locked_company(s, cid)
        if change == "membership":
            await s.execute(update(UserCompany).where(
                UserCompany.company_id == cid, UserCompany.user_id == uid).values(is_active=False))
        elif change == "permission":
            company.settings = {**company.settings, "role_grants": {KEY: ["owner"]}}
        elif change == "module":
            company.settings = set_enabled(company.settings, set())
        await s.commit()


async def _change_waits(factory, cid, uid) -> bool:
    """True when a change cannot take the company lock because it is held."""
    try:
        await _change(factory, cid, uid, "membership")
    except Exception as exc:  # noqa: BLE001 - lock_timeout surfaces through the driver
        return "lock" in str(exc).lower()
    return False


def _authorize(s, cid, uid) -> None:
    authority = authorize_request(s, cid, uid, ROLE, module=DOCS)
    authority.keys.add(KEY)


@pytest.mark.parametrize("change", ["membership", "permission", "module"])
async def test_a_lock_rolled_back_with_its_savepoint_is_judged_again(factory, change):
    cid, uid = await _company(factory)
    async with factory() as s:
        _authorize(s, cid, uid)
        await s.execute(select(1))
        nested = await s.begin_nested()
        await lock_company(s, cid)
        await _write(s, cid)
        await nested.rollback()

        await _change(factory, cid, uid, change)

        with pytest.raises(HTTPException) as refused:
            await lock_company(s, cid)
        assert refused.value.status_code == 403
        await _write(s, cid)
        with pytest.raises(HTTPException):
            await s.commit()
        await s.rollback()
    assert await _written(factory, cid) == 0


async def test_module_work_rolled_back_with_its_savepoint_is_judged_again(factory):
    cid, uid = await _company(factory)
    with pytest.raises(ModuleOff):  # the work ends with its refusal
        async with module_use(cid, DOCS):
            async with factory() as s:
                await s.execute(select(1))
                nested = await s.begin_nested()
                await lock_company(s, cid)
                await nested.rollback()

                await _change(factory, cid, uid, "module")

                with pytest.raises(ModuleOff):
                    await lock_company(s, cid)
                await _write(s, cid)
                with pytest.raises(ModuleOff):
                    await s.commit()
                await s.rollback()
    assert await _written(factory, cid) == 0


async def test_a_company_loaded_in_a_rolled_back_savepoint_is_loaded_again(factory):
    cid, uid = await _company(factory)
    async with factory() as s:
        await s.execute(select(1))
        nested = await s.begin_nested()
        company = await locked_company(s, cid)
        await nested.rollback()
        company.settings = {**company.settings, "stale": True}
        with pytest.raises(RuntimeError, match="locked_company"):
            await s.flush()
        await s.rollback()


async def test_a_lock_taken_before_the_savepoint_stays_judged(factory):
    cid, uid = await _company(factory)
    async with factory() as s:
        _authorize(s, cid, uid)
        await lock_company(s, cid)
        nested = await s.begin_nested()
        await lock_company(s, cid)
        await _write(s, cid)
        await nested.rollback()

        assert await _change_waits(factory, cid, uid)

        await lock_company(s, cid)
        await _write(s, cid)
        await s.commit()
    assert await _written(factory, cid) == 1


async def test_module_work_locked_before_the_savepoint_stays_judged(factory):
    cid, uid = await _company(factory)
    async with module_use(cid, DOCS):
        async with factory() as s:
            await lock_company(s, cid)
            nested = await s.begin_nested()
            await lock_company(s, cid)
            await nested.rollback()
            await lock_company(s, cid)
            await _write(s, cid)
            await s.commit()
    assert await _written(factory, cid) == 1


async def test_a_company_loaded_before_the_savepoint_stays_loaded(factory):
    cid, uid = await _company(factory)
    async with factory() as s:
        company = await locked_company(s, cid)
        nested = await s.begin_nested()
        await nested.rollback()
        company.settings = {**company.settings, "kept": True}
        await s.commit()
    async with factory() as s:
        assert (await s.get(Company, cid)).settings["kept"] is True


async def test_a_released_savepoint_keeps_its_lock_judged(factory):
    cid, uid = await _company(factory)
    async with factory() as s:
        _authorize(s, cid, uid)
        await s.execute(select(1))
        async with s.begin_nested():
            await lock_company(s, cid)
        assert await _change_waits(factory, cid, uid)
        await lock_company(s, cid)
        await _write(s, cid)
        await s.commit()
    assert await _written(factory, cid) == 1
