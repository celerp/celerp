# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A migration run's routes act for the run's company, not the company the caller's
token is scoped to. Loading the run registers that authority, so the company lock the
start, finalize and discard take judges the caller's membership and role there again,
and a cancel, which must not wait for the runner's batch, is judged again without it.
A control lost meanwhile refuses the change and writes nothing.

Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.migration import MigrationRun, MigrationStatus
from celerp.services import migrations
from celerp.services.company_lock import lock_company

pytestmark = pytest.mark.asyncio

_LOST = {"revoked": {"is_active": False}, "demoted": {"role": "admin"}}
_ACTIONS = {
    "start": (MigrationStatus.READY, migrations.request_start),
    "finalize": (MigrationStatus.READY_TO_FINALIZE, migrations.finalize),
    "discard": (MigrationStatus.READY, migrations.discard),
}


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _run(factory, status: MigrationStatus) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    cid, uid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Staged Co", slug=f"mg-{cid.hex[:8]}", settings={}, is_migration_staged=True))
        s.add(User(id=uid, email=f"mg-{uid.hex[:8]}@test.co", name="Owner", auth_hash="x", is_active=True))
        await s.flush()
        s.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="owner", is_active=True))
        await s.flush()
        s.add(MigrationRun(id=rid, company_id=cid, created_by_user_id=uid,
                           scan_claim_sha256=hashlib.sha256(rid.bytes).hexdigest(), source_system="csv",
                           source_artifact_sha256="0" * 64, adapter_version="1", cif_version="1",
                           mode="new_company", status=status.value))
        await s.commit()
    return cid, uid, rid


def _lose(cid, uid, lost: str):
    return update(UserCompany).where(UserCompany.company_id == cid, UserCompany.user_id == uid).values(**_LOST[lost])


async def _state(factory, cid, rid) -> tuple[str | None, bool]:
    async with factory() as s:
        status = await s.scalar(select(MigrationRun.status).where(MigrationRun.id == rid))
        return status, await s.get(Company, cid) is not None


@pytest.mark.parametrize("lost", sorted(_LOST))
@pytest.mark.parametrize("action", sorted(_ACTIONS))
async def test_a_run_change_waiting_while_its_control_is_lost_is_refused(factory, action, lost):
    status, change = _ACTIONS[action]
    cid, uid, rid = await _run(factory, status)
    async with factory() as holding, factory() as s:
        await lock_company(holding, cid)
        run = await migrations.get_owned_migration_run(s, rid, uid)
        waiting = asyncio.create_task(change(s, run))
        await asyncio.sleep(0.5)
        assert not waiting.done(), f"{action} did not wait for the company lock"
        await holding.execute(_lose(cid, uid, lost))
        await holding.commit()
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(waiting, 10)
            await s.commit()
        assert refused.value.status_code == 403
        await s.rollback()
    assert await _state(factory, cid, rid) == (status.value, True), f"the refused {action} wrote"


@pytest.mark.parametrize("lost", sorted(_LOST))
async def test_a_cancel_after_its_control_was_lost_is_refused(factory, lost):
    cid, uid, rid = await _run(factory, MigrationStatus.RUNNING)
    async with factory() as s:
        run = await migrations.get_owned_migration_run(s, rid, uid)
        async with factory() as losing:
            await losing.execute(_lose(cid, uid, lost))
            await losing.commit()
        with pytest.raises(HTTPException) as refused:
            await migrations.request_cancel(s, run)
        assert refused.value.status_code == 403
        await s.rollback()
    assert await _state(factory, cid, rid) == (MigrationStatus.RUNNING.value, True)


async def test_a_cancel_waits_for_a_role_change_in_flight_and_is_judged_by_it(factory):
    cid, uid, rid = await _run(factory, MigrationStatus.RUNNING)
    async with factory() as losing, factory() as s:
        run = await migrations.get_owned_migration_run(s, rid, uid)
        await losing.execute(_lose(cid, uid, "demoted"))
        cancelling = asyncio.create_task(migrations.request_cancel(s, run))
        await asyncio.sleep(0.5)
        assert not cancelling.done(), "the cancel did not wait for the role change in flight"
        await losing.commit()
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(cancelling, 10)
        assert refused.value.status_code == 403
        await s.rollback()
    assert await _state(factory, cid, rid) == (MigrationStatus.RUNNING.value, True)


async def test_a_cancel_while_the_runner_holds_the_company_lock_is_saved_at_once(factory):
    cid, uid, rid = await _run(factory, MigrationStatus.RUNNING)
    async with factory() as runner, factory() as s:
        await lock_company(runner, cid)
        run = await migrations.get_owned_migration_run(s, rid, uid)
        await asyncio.wait_for(migrations.request_cancel(s, run), 5)
        await runner.rollback()
    assert await _state(factory, cid, rid) == (MigrationStatus.CANCEL_REQUESTED.value, True)
