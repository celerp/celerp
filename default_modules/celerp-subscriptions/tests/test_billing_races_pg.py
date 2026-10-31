# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""One document per period under real concurrency: two requests, two dispatchers, a
crash between the claim and its commit. Runs on real PostgreSQL across connections,
in the steps of tests/test_fresh_authority_races_pg."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agreement_kit import agreement, freeze_day, generate_due, schedule
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_fresh_authority_races_pg import _Held, _app_client, _race, _seed, _until_waiting_or_done

pytestmark = [pytest.mark.process, pytest.mark.asyncio]


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _billed(factory, company_id) -> list[tuple[str, int]]:
    """(agreement, period) of every billed document, one entry per document."""
    async with factory() as s:
        rows = (await s.execute(select(Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    return sorted((st["source_agreement"], st["period_index"]) for st in rows if st.get("source_agreement"))


async def _claims(factory, company_id) -> list[str]:
    async with factory() as s:
        return sorted((await s.execute(select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id, LedgerEntry.idempotency_key.like("sub:%")))).scalars().all())


async def _auto(client, owner, *, start, catchup) -> str:
    eid = await agreement(client, owner, start=start)
    r = await schedule(client, owner, eid, generation_mode="auto_invoice", catchup_periods=catchup)
    assert r.status_code == 200, r.text
    return eid


def _holding_first(factory, held: _Held):
    """A session factory whose first session holds its commit until released."""
    opened = []

    @asynccontextmanager
    async def _open():
        async with factory() as s:
            if not opened:
                held.hold(s)
            opened.append(s)
            yield s

    return _open


async def test_same_period_two_workers_one_invoice(committed_engine, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    factory = _factory(committed_engine)
    seeded = await _seed(factory)
    owner = seeded["h"]["owner"]
    async with _app_client(factory) as (client, pending):
        eid = await agreement(client, owner, start="2026-03-01")

        def _generate(key):
            async def _go(held):
                if held is not None:
                    pending.append(held)
                return await generate_due(client, owner, eid, key=key)
            return _go

        first, second = await _race(committed_engine, _generate("worker-a"), _generate("worker-b"))
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["doc_id"] == second.json()["doc_id"]
    assert await _billed(factory, seeded["company_id"]) == [(eid, 0)]
    assert await _claims(factory, seeded["company_id"]) == [f"sub:{eid}:p0:invoice:r0"]


class _Crash:
    """The request's commit never happens: the process dies first."""

    def hold(self, session) -> None:
        async def _die() -> None:
            raise ConnectionError("process stopped before commit")

        session.commit = _die


async def test_restart_between_claim_and_commit_one_invoice(committed_engine, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    factory = _factory(committed_engine)
    seeded = await _seed(factory)
    owner = seeded["h"]["owner"]
    async with _app_client(factory) as (client, pending):
        eid = await agreement(client, owner, start="2026-03-01")
        pending.append(_Crash())
        with pytest.raises(ConnectionError):
            await generate_due(client, owner, eid, key="before-crash")
        assert await _billed(factory, seeded["company_id"]) == []
        retried = await generate_due(client, owner, eid, key="after-restart")
        assert retried.status_code == 200 and retried.json()["period_index"] == 0
        again = await generate_due(client, owner, eid, key="after-restart-2")
        assert again.json()["doc_id"] == retried.json()["doc_id"]
    assert await _billed(factory, seeded["company_id"]) == [(eid, 0)]
    assert await _claims(factory, seeded["company_id"]) == [f"sub:{eid}:p0:invoice:r0"]


async def test_two_processes_dispatch_same_company_one_invoice(committed_engine, monkeypatch):
    from celerp_docs import periodic

    freeze_day(monkeypatch, "2026-03-15")
    factory = _factory(committed_engine)
    seeded = await _seed(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    today = date(2026, 3, 15)
    async with _app_client(factory) as (client, _pending):
        eid = await _auto(client, owner, start="2026-01-01", catchup=2)  # periods 0, 1, 2 due
        await asyncio.gather(
            periodic.run_company(factory, cid, today=today),
            periodic.run_company(factory, cid, today=today),
            client.post("/subscriptions/generate-due", headers=owner, json={"ids": [eid]}),
        )
        await periodic.run_company(factory, cid, today=today)
    assert await _billed(factory, cid) == [(eid, 0), (eid, 1), (eid, 2)]
    assert await _claims(factory, cid) == [f"sub:{eid}:p{k}:invoice:r0" for k in range(3)]


async def test_two_dispatchers_respect_budget_and_claims(committed_engine, monkeypatch):
    from celerp.modules import slots
    from celerp_docs import periodic

    # Billing alone, so the held commit is the pass that holds the company's billing budget.
    monkeypatch.setitem(slots._slots, "periodic_operation", [
        c for c in slots.get("periodic_operation") if c["handler"].startswith("celerp_subscriptions.")])
    freeze_day(monkeypatch, "2026-07-15")
    factory = _factory(committed_engine)
    seeded = await _seed(factory)
    owner, cid = seeded["h"]["owner"], seeded["company_id"]
    today = date(2026, 7, 15)
    async with _app_client(factory) as (client, _pending):
        eids = [await _auto(client, owner, start="2026-04-01", catchup=3) for _ in range(3)]  # 12 due
    held = _Held()
    first = asyncio.create_task(periodic.run_company(_holding_first(factory, held), cid, today=today))
    await asyncio.wait_for(held.reached.wait(), 30)
    second = asyncio.create_task(periodic.run_company(factory, cid, today=today))
    await _until_waiting_or_done(committed_engine, second)
    held.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 30)
    billed = await _billed(factory, cid)
    assert len(billed) == 10 and len(set(billed)) == 10  # one budget per pass, one document per period
    assert {e for e, _ in billed} <= set(eids)
