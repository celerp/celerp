# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""List line_items are read-modify-written by scan check-off and counting: a route reads the
whole array, mutates it in Python, and writes it back. Two concurrent writers on the same list
would each read the same array and the second's write would clobber the first (a lost update).
_get_list_for_update takes a row lock (SELECT ... FOR UPDATE) so the second writer blocks until
the first commits, then re-reads the committed array.

These use independent sessions on the shared engine with real commits (not the savepoint
session): a row lock is only observable across separately committed transactions."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


async def _seed_company(factory) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="LockCo", slug=f"lock-{company_id.hex[:8]}"))
        await s.commit()
    return company_id


async def _seed_list(factory, company_id) -> str:
    entity_id = "list:LOCKTEST"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="list",
            event_type="list.created",
            data={"list_type": "audit", "status": "finalized", "ref_id": "LOCKTEST",
                  "line_items": [{"item_id": "item:x", "sku": "X", "quantity": 1}]},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _cleanup(factory, company_id):
    from celerp_accounting.models import Account
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Account).where(Account.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.commit()


async def _seed_typed_list(factory, company_id, entity_id, list_type, status):
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="list",
            event_type="list.created",
            data={"list_type": list_type, "status": status, "ref_id": entity_id.split(":")[-1],
                  "line_items": [{"item_id": "item:x", "sku": "X", "quantity": 1}]},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()


# Each terminal/undo route reads the list's line_items, mutates stock, and writes the array back, so it
# must take the list row lock. Seed a status the route's early guard rejects (a fast 409): the block we
# assert happens at the FOR UPDATE loader, before that guard, so the guard's outcome is irrelevant - all
# we need is that the route reaches and acquires the lock. Without the lock (the pre-change loader) the
# route returns immediately and finishes inside the grace window.
_LOCK_CALLERS = [
    ("finalize_list", "shipment", "finalized"),  # guard: only a draft can be finalized
    ("adjust_audit", "audit", "draft"),          # guard: finalize the count before adjusting
    ("undo_audit_adjust", "audit", "draft"),     # guard: no adjustment to undo
    ("undo_write_off", "writeoff", "draft"),     # guard: no removal to undo
]


@pytest.mark.parametrize("fn_name, list_type, status", _LOCK_CALLERS)
@pytest.mark.asyncio
async def test_terminal_route_takes_list_row_lock(_db_engine, fn_name, list_type, status):
    """While one transaction holds the list row lock, the terminal/undo route on the same list blocks
    until that transaction commits. Before the fix each route loaded the row without FOR UPDATE, so it
    ran immediately against a stale line_items array (a lost update against a concurrent scan/count)."""
    from celerp_docs import routes as _routes
    from celerp_docs.routes import _get_list_for_update

    fn = getattr(_routes, fn_name)
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    entity_id = f"list:LOCK-{fn_name}"
    await _seed_typed_list(factory, company_id, entity_id, list_type, status)
    user = types.SimpleNamespace(id=uuid.uuid4())
    s_lock, s_route = factory(), factory()
    try:
        await _get_list_for_update(s_lock, company_id, entity_id)  # hold the row lock

        done = asyncio.Event()

        async def _run():
            try:
                await fn(entity_id, company_id=company_id, _=None, user=user, session=s_route)
            except Exception:
                pass  # a status-guard 409 still proves the route got PAST the FOR UPDATE loader
            finally:
                done.set()

        task = asyncio.create_task(_run())
        await asyncio.sleep(0.3)
        assert not done.is_set(), f"{fn_name} did not block on the list row lock"

        await s_lock.commit()  # release the lock
        await asyncio.wait_for(task, timeout=10)
        assert done.is_set()
    finally:
        await s_lock.close()
        await s_route.close()
        await _cleanup(factory, company_id)


@pytest.mark.asyncio
async def test_get_list_for_update_serializes_concurrent_writers(_db_engine):
    """While one transaction holds the list row lock, a second _get_list_for_update on the same
    list blocks until the first commits. Without the FOR UPDATE lock the second returns
    immediately and both writers derive from the same stale line_items array."""
    from celerp_docs.routes import _get_list_for_update

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    entity_id = await _seed_list(factory, company_id)
    s1, s2 = factory(), factory()
    try:
        await _get_list_for_update(s1, company_id, entity_id)  # s1 holds the row lock

        second_locked = asyncio.Event()

        async def _second():
            await _get_list_for_update(s2, company_id, entity_id)
            second_locked.set()

        task = asyncio.create_task(_second())
        await asyncio.sleep(0.3)
        assert not second_locked.is_set(), "second writer did not block on the list row lock"

        await s1.commit()  # release the lock
        await asyncio.wait_for(task, timeout=10)
        assert second_locked.is_set()
        await s2.commit()
    finally:
        await s1.close()
        await s2.close()
        await _cleanup(factory, company_id)


async def _seed_chart(factory, company_id):
    """Give the company its default chart so the write-off account validation and the Inventory
    credit both resolve real accounts."""
    from celerp_accounting.routes import seed_chart_of_accounts
    async with factory() as s:
        await seed_chart_of_accounts(s, company_id)
        await s.commit()


async def _seed_item(factory, company_id, entity_id, *, quantity, cost_total, sku):
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item",
            event_type="item.created",
            data={"sku": sku, "name": sku, "quantity": quantity, "cost_total": cost_total,
                  "sell_by": "piece", "status": "available", "inventory_type": "stocked"},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()


async def _seed_user(factory, company_id) -> uuid.UUID:
    """A real user row so the write-off's ledger actor_id FK resolves."""
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id, email=f"wo-{user_id.hex[:8]}@example.test", name="Manager"))
        await s.commit()
    return user_id


async def _seed_writeoff_list(factory, company_id, entity_id, *, item_id, qty_out, account, sku):
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="list",
            event_type="list.created",
            data={"list_type": "writeoff", "status": "finalized", "ref_id": entity_id.split(":")[-1],
                  "line_items": [{"line_id": uuid.uuid4().hex, "item_id": item_id, "sku": sku,
                                  "qty_out": qty_out, "account": account, "comment": ""}]},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_writeoff_concurrent_same_item_two_lists_one_wins(_db_engine):
    """Two write-off lists each removing the SAME item's whole quantity, run concurrently from two
    independent sessions on real Postgres: the item projection is locked FOR UPDATE, so exactly one
    call disposes it and posts one Inventory credit; the other blocks, re-reads the committed
    decremented state, fails its aggregate check and returns 422. One physical disposal, one credit."""
    from fastapi import HTTPException

    from celerp_docs.routes import write_off_stock

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    item_id = "item:WO-CONC"
    await _seed_chart(factory, company_id)
    await _seed_item(factory, company_id, item_id, quantity=5, cost_total=50, sku="WO-CONC")
    list_a, list_b = "list:WOCONC-A", "list:WOCONC-B"
    await _seed_writeoff_list(factory, company_id, list_a, item_id=item_id, qty_out=5, account="6950", sku="WO-CONC")
    await _seed_writeoff_list(factory, company_id, list_b, item_id=item_id, qty_out=5, account="6600", sku="WO-CONC")

    user = types.SimpleNamespace(id=await _seed_user(factory, company_id))
    s_a, s_b = factory(), factory()

    async def _run(entity_id, s):
        try:
            result = await write_off_stock(entity_id, company_id=company_id, _=None, user=user, session=s)
            return ("ok", result)
        except HTTPException as exc:
            return ("http", exc.status_code)

    try:
        results = await asyncio.gather(_run(list_a, s_a), _run(list_b, s_b))

        oks = [r for r in results if r[0] == "ok"]
        rejects = [r for r in results if r[0] == "http"]
        assert len(oks) == 1, f"expected exactly one success, got {results}"
        assert len(rejects) == 1 and rejects[0][1] == 422, f"expected one 422, got {results}"

        # Exactly one physical disposal of the item value: one Inventory credit == item cost.
        async with factory() as s:
            jes = (await s.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "journal_entry",
                )
            )).scalars().all()
            credits = []
            for je in jes:
                for e in je.state.get("entries") or []:
                    c = float(e.get("credit", 0) or 0)
                    if e.get("account") == "1130-P" and c:
                        credits.append(c)
            assert credits == [50.0], f"expected one 50.0 Inventory credit, got {credits}"

            # The original item ends disposed exactly once (never double-decremented).
            item = (await s.execute(select(Projection).where(
                Projection.company_id == company_id, Projection.entity_id == item_id))).scalar_one()
            assert item.state.get("status") == "disposed"
    finally:
        await s_a.close()
        await s_b.close()
        await _cleanup(factory, company_id)
        async with factory() as s:
            await s.execute(delete(User).where(User.id == user.id))
            await s.commit()


@pytest.mark.asyncio
async def test_get_list_for_update_404s_non_list(_db_engine):
    """The locking loader keeps the same entity-type guard as the plain loader: a missing or
    non-list entity is a 404, not a silently locked wrong row."""
    from fastapi import HTTPException

    from celerp_docs.routes import _get_list_for_update

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    try:
        async with factory() as s:
            with pytest.raises(HTTPException) as exc:
                await _get_list_for_update(s, company_id, "list:NOPE")
            assert exc.value.status_code == 404
    finally:
        await _cleanup(factory, company_id)
