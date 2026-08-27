# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""The company code-namespace lock must not deadlock when the transaction taking it has
already emitted a ledger event for the same company.

Every ledger insert takes an implicit foreign-key KEY SHARE lock on its company row
(celerp/models/ledger.py: LedgerEntry.company_id -> companies.id) and holds it until the
transaction ends. A one-tap build (celerp_manufacturing.routes.build_item with complete=True)
emits mfg.order.created and then, in the same transaction, locks the company row to mint the
output lot's barcode (_lock_code_namespace_for_completion -> lock_item_code_namespace). If
that lock is FOR UPDATE it has to upgrade past the transaction's own KEY SHARE, and two such
transactions - each already holding KEY SHARE on the company, each now requesting the row
lock - block on each other, so Postgres aborts one with a deadlock (SQLSTATE 40P01). The same
shape reaches every issue-then-receive completion path: build_item, make_work_orders,
automatic completion during finalize, and a later run in a bulk transaction after an earlier
one emitted an event.

The fix takes the lock as FOR NO KEY UPDATE (celerp_inventory.services.lock_item_code_
namespace, with_for_update(key_share=True)). FOR NO KEY UPDATE does not conflict with KEY
SHARE, so no upgrade happens; it still conflicts with another FOR NO KEY UPDATE, so barcode
allocators stay serialized for every module. The two tests below prove both halves: the lock
still serializes (one holder at a time), and two transactions that each already hold the
implicit KEY SHARE both commit without a deadlock.

These use independent sessions bound to the shared engine with real commits (not the
savepoint session): a row lock is only observable, and contention only forms, between two
separately committed transactions - a single rolled-back savepoint session cannot exercise it
faithfully."""

from __future__ import annotations

import asyncio
import time
import types
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.inventory_codes import BarcodeConflictError  # noqa: F401 - imported for parity with services
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

from celerp_inventory.services import lock_item_code_namespace


def _is_deadlock(exc: BaseException) -> bool:
    """True if exc (or anything in its cause chain, including a wrapped DBAPI .orig) is a
    Postgres deadlock (SQLSTATE 40P01), the exact failure the lock-mode fix removes."""
    seen: list[BaseException] = []
    chain = [exc]
    while chain:
        e = chain.pop()
        if e is None or e in seen:
            continue
        seen.append(e)
        if getattr(e, "sqlstate", None) == "40P01" or "deadlock detected" in str(e).lower():
            return True
        orig = getattr(e, "orig", None)
        if orig is not None:
            chain.append(orig)
        chain.append(e.__cause__)
        chain.append(e.__context__)
    return False


async def _cleanup(factory, company_id, user_id) -> None:
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _seed_company(factory) -> tuple[uuid.UUID, uuid.UUID, types.SimpleNamespace]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Lockrace Co", slug=f"lockrace-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"race-{user_id.hex[:8]}@lockrace.test", name="Race User",
                   auth_hash="x"))
        await s.commit()
    # A bare .id accessor is all production code reads off `user` on these paths (actor_id on
    # emitted events); the FK target is the committed row above.
    return company_id, user_id, types.SimpleNamespace(id=user_id)


@pytest.mark.asyncio
async def test_company_lock_upgrade_serializes_without_deadlock(_db_engine):
    """Two transactions each emit a ledger event for the same company (so each holds the
    implicit KEY SHARE on the company row), synchronize so both hold it, then concurrently
    take the code-namespace lock. FOR NO KEY UPDATE never has to upgrade past KEY SHARE, so
    one acquires and the other waits for it to commit; both then commit with no deadlock. On
    the pre-fix FOR UPDATE lock the same interleaving is a lock upgrade past the peer's KEY
    SHARE, which Postgres breaks with 40P01."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    both_hold_key_share = asyncio.Barrier(2)
    outcome: dict[int, BaseException | None] = {0: None, 1: None}
    window: dict[int, tuple[float, float]] = {}

    async def _hold_and_lock(idx: int) -> None:
        s = factory()
        try:
            await emit_event(
                s, company_id=company_id, entity_id=f"item:ks-{idx}-{uuid.uuid4().hex[:8]}",
                entity_type="item", event_type="item.created",
                data={"sku": f"KS{idx}", "name": "Key-share holder", "quantity": 1},
                actor_id=user.id, location_id=None, source="test",
                idempotency_key=str(uuid.uuid4()), metadata_={},
            )
            # Flush so the INSERT lands and its FK check takes the KEY SHARE on the company row
            # now, before the barrier - the emit alone would not touch the DB until the next
            # execute or commit, and the whole point is that both hold KEY SHARE first.
            await s.flush()
            await both_hold_key_share.wait()
            await lock_item_code_namespace(s, company_id)
            acquired = time.monotonic()
            # Hold the lock briefly so the two holders' windows are measurable: under a lock
            # that serializes, the second cannot acquire until the first has committed.
            await asyncio.sleep(0.15)
            await s.commit()
            window[idx] = (acquired, time.monotonic())
        except Exception as exc:  # noqa: BLE001 - captured for the race assertion, not swallowed
            outcome[idx] = exc
            await s.rollback()
        finally:
            await s.close()

    try:
        await asyncio.wait_for(asyncio.gather(_hold_and_lock(0), _hold_and_lock(1)), timeout=20)
        failures = {i: repr(e) for i, e in outcome.items() if e is not None}
        deadlocks = {i: str(e) for i, e in outcome.items() if e is not None and _is_deadlock(e)}
        assert not deadlocks, f"company-lock upgrade deadlocked (40P01): {deadlocks}"
        assert not failures, f"lock attempt failed for a non-deadlock reason: {failures}"
        # Serialization: exactly one holder at a time. Ordered by acquisition, the earlier
        # holder must have committed (released the lock) no later than the later one acquired
        # it - the windows do not overlap.
        first, second = sorted(window.values(), key=lambda w: w[0])
        assert second[0] >= first[1] - 0.01, (
            f"the two lock holders overlapped ({first} vs {second}); the namespace lock did "
            f"not serialize them"
        )
    finally:
        await _cleanup(factory, company_id, user_id)


async def _seed_buildable(factory, company_id, user, i: int) -> str:
    """A component with 100 on hand and a manufacturable product whose recipe consumes 5 of
    it. Returns the product's entity_id, ready for a one-tap build_item(complete=True)."""
    component_id = f"item:comp-{i}-{uuid.uuid4().hex[:8]}"
    product_id = f"item:prod-{i}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=component_id, entity_type="item",
            event_type="item.created",
            data={"sku": f"COMP{i}", "name": "Component", "quantity": 100, "cost_total": 100,
                  "status": "available"},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=product_id, entity_type="item",
            event_type="item.created",
            data={"sku": f"PROD{i}", "name": "Product", "quantity": 0, "status": "available",
                  "recipe": {"output_qty": 1, "components": [{"item_id": component_id, "quantity": 5}]}},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return product_id


@pytest.mark.asyncio
async def test_concurrent_one_tap_builds_no_deadlock(_db_engine):
    """Two real one-tap builds of two independent products (each with its own component, so
    the company row is the only lock they share) run concurrently on separately committed
    sessions. Each emits mfg.order.created and then locks the company to mint its output lot -
    the exact create-and-complete lock upgrade. Both must complete with no deadlock; before
    the fix this pair aborts one side with 40P01."""
    from celerp_manufacturing.routes import build_item, BuildBody
    import celerp_inventory.services as inventory_services

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id, user = await _seed_company(factory)
    product_a = await _seed_buildable(factory, company_id, user, 0)
    product_b = await _seed_buildable(factory, company_id, user, 1)
    results: dict[str, dict | BaseException] = {}

    # By the time a completion reaches _lock_code_namespace_for_completion it has already
    # emitted mfg.order.created and flushed it (the _all_item_states read autoflushes), so it
    # holds the company KEY SHARE. Barrier the FIRST namespace-lock acquisition per session so
    # both builds hold KEY SHARE before either takes the company lock, making the upgrade
    # contention deterministic instead of a matter of scheduling luck. This wraps the real
    # lock (never delays it beyond the barrier) and _lock_code_namespace_for_completion imports
    # the name from this module each call, so the wrapper is what completion sees.
    both_hold_key_share = asyncio.Barrier(2)
    orig_lock = inventory_services.lock_item_code_namespace
    synced: set[int] = set()

    async def _barrier_then_lock(session, cid):
        if cid == company_id and id(session) not in synced:
            synced.add(id(session))
            await both_hold_key_share.wait()
        return await orig_lock(session, cid)

    async def _one_tap(label: str, product_id: str) -> None:
        s = factory()
        try:
            res = await build_item(product_id, BuildBody(quantity=1.0, complete=True),
                                   company_id=company_id, user=user, _=None, session=s)
            results[label] = res
        except Exception as exc:  # noqa: BLE001 - captured for the race assertion, not swallowed
            results[label] = exc
            await s.rollback()
        finally:
            await s.close()

    inventory_services.lock_item_code_namespace = _barrier_then_lock
    try:
        await asyncio.wait_for(
            asyncio.gather(_one_tap("a", product_a), _one_tap("b", product_b)), timeout=30
        )
        deadlocks = {k: str(v) for k, v in results.items()
                     if isinstance(v, BaseException) and _is_deadlock(v)}
        failures = {k: repr(v) for k, v in results.items() if isinstance(v, BaseException)}
        assert not deadlocks, f"concurrent one-tap builds deadlocked (40P01): {deadlocks}"
        assert not failures, f"a one-tap build failed for a non-deadlock reason: {failures}"
        async with factory() as s:
            for label, product_id in (("a", product_a), ("b", product_b)):
                order_id = results[label]["id"]
                order = await s.get(Projection, {"company_id": company_id, "entity_id": order_id})
                assert order.state["status"] == "completed", f"build {label} did not complete"
    finally:
        inventory_services.lock_item_code_namespace = orig_lock
        await _cleanup(factory, company_id, user_id)
