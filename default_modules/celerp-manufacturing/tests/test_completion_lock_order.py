# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""A manufacturing completion (issue components, receive the output) races a real, concurrent
barcode edit on one of the run's own components: both must complete without a hang or a
Postgres-detected deadlock, and the completion session must claim the company code-namespace
lock no later than it claims the racing component's own item-projection lock.

Both flows serialize the same two row locks - the company code-namespace row
(celerp_inventory.services.lock_item_code_namespace, SELECT Company FOR UPDATE) and a
component's projection row (celerp.projections.engine.ProjectionEngine._locked_projection,
SELECT ... FOR UPDATE). A concurrent barcode edit (default_modules/celerp-inventory
routes.patch_item) always claims company first, then the item lock. Before the fix, a
completion claimed the item lock first (item.consumed during issue) and the company lock
second (minting the output lot's barcode during receive) - the reverse order, which is the
precondition for an AB/BA cycle between the two sessions. The fix takes the company lock
first in every issue-then-receive completion path (_lock_code_namespace_for_completion), so
both flows now agree on lock order.

These use independent sessions bound to the shared engine with real commits (not the
savepoint session): a row lock is only observable, and contention only forms, between two
separately committed transactions - a single rolled-back savepoint session cannot exercise
it faithfully.

A free-running race between the two paths is timing-dependent: patch_item's preamble (field
schema, price config, several other reads) is long enough relative to the completion path
that the completion transaction usually finishes before the barcode edit ever reaches its
own lock request, so the two never actually contend. One plain asyncio barrier (not a change
to production code) makes the interleaving deterministic instead of a matter of luck: the
barcode edit does not start at all until completion has genuinely issued the racing
component (holds its item-projection lock) - see _race_once for the exact hook. Everything
before and after that single gate is the real, unmodified code on each side deciding for
itself which lock it reaches for, in what order, and whether it blocks.

A companion investigation (a standalone Postgres session pair inserting a child row under an
open, uncommitted transaction and separately requesting FOR UPDATE on the parent it
references) confirmed that every ledger insert - including the racing component's own
item.consumed - takes an implicit row-share lock on the referenced Company row via the
foreign key check (celerp/models/ledger.py: LedgerEntry.company_id -> companies.id), held
until that transaction ends. That implicit lock lands before completion's own explicit
company-lock call on every code path, on both trees, which is why the barrier above cannot
be extended into forcing a genuine two-way Postgres deadlock (SQLSTATE 40P01) for this exact
pairing: completion's session touches the company row (implicitly) no later than its first
item lock regardless of tree, so the two sessions can only ever queue one-way behind each
other, never cross. The test proves the two guarantees that are actually reachable and are
exactly what the fix's diff changed: (1) real concurrent contention between the two flows
never errors or hangs, checked on every race, and (2) the completion session's own lock
requests are ordered company-then-item, checked directly rather than inferred from whether a
race happened to fail - true on the fixed tree, false (item-then-company) on the pre-fix
tree, so it is exactly the fix's guarantee, not a proxy for it."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

_RACES = 5  # the barrier makes each race deterministic; a handful confirms it is not a fluke


async def _cleanup(factory, company_id, user_id) -> None:
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _seed_run(factory, company_id, user, i: int) -> tuple[str, str, str]:
    """A component with 100 on hand, a product with 0 on hand, and a planned run that will
    consume 5 of the component and receive 2 of the product. Returns
    (order_id, component_id, old_barcode)."""
    component_id = f"item:comp-{i}-{uuid.uuid4().hex[:8]}"
    product_id = f"item:prod-{i}-{uuid.uuid4().hex[:8]}"
    order_id = f"mfg:{uuid.uuid4()}"
    # 13 digits: a GTIN-length barcode, excluded from _next_seq's internal-code
    # scan (celerp_inventory/services.py: only <= 9-digit barcodes count), so the
    # auto-minted output-lot barcode (a short zero-padded sequence) can never
    # collide with these manually-assigned values.
    old_barcode = f"2{i:05d}0000000"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=component_id, entity_type="item",
            event_type="item.created",
            data={"sku": f"COMP{i}", "name": "Component", "quantity": 100, "cost_total": 100,
                  "barcode": old_barcode},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=product_id, entity_type="item",
            event_type="item.created",
            data={"sku": f"PROD{i}", "name": "Product", "quantity": 0},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=order_id, entity_type="mfg_order",
            event_type="mfg.order.created",
            data={
                "description": f"Build 2 x PROD{i}", "order_type": "assembly",
                "inputs": [{"item_id": component_id, "quantity": 5}],
                "expected_outputs": [{"sku": f"PROD{i}", "name": "Product", "quantity": 2}],
                "output_item_id": product_id,
            },
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return order_id, component_id, old_barcode


def _is_deadlock(exc: BaseException) -> bool:
    """True if exc (or anything in its cause chain, including a wrapped DBAPI .orig) is a
    Postgres deadlock (SQLSTATE 40P01), the exact failure the lock-order fix removes."""
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


async def _race_once(
    factory, company_id, user, i: int
) -> tuple[BaseException | None, BaseException | None, str, str, str, list[str]]:
    """Run one real completion concurrently with one real barcode edit on the run's own
    component, each on its own committed transaction. Returns (completion_exc, barcode_exc,
    order_id, component_id, new_barcode, completion_lock_order).

    ``completion_lock_order`` is the order, on the completion session only, in which the two
    contended locks were actually requested: "company" (celerp_inventory.services.
    lock_item_code_namespace - the code-namespace lock _lock_code_namespace_for_completion
    takes up front on the fixed tree) and "item" (celerp.projections.engine.ProjectionEngine.
    _locked_projection for the racing component's own item.consumed). This is the exact
    invariant the fix establishes (company lock claimed no later than the first item lock, so
    a concurrent barcode edit - which always claims company then item - can never observe the
    reverse) and is checked directly, not inferred from whether a race happened to fail.

    One plain asyncio barrier pins the earliest moment a race between the two is even
    possible, so it is deterministic instead of a matter of scheduling luck, without
    changing WHEN either side's lock requests resolve or what they do:
    celerp.projections.engine.ProjectionEngine._locked_projection (reached from
    celerp_manufacturing.routes._consume_components -> emit_event, the completion path's
    component-projection lock) signals once completion genuinely holds the racing
    component's projection row locked, from having issued it (item.consumed). The barcode
    edit does not start at all until that signal fires, so it can never simply run to
    completion before completion has touched the shared component - every remaining step on
    both sides, including which lock each reaches for next and whether it blocks, is the
    real, unmodified production code deciding for itself.
    """
    from celerp_inventory.routes import ItemPatch, patch_item
    import celerp_inventory.services as inventory_services
    from celerp_manufacturing.routes import _complete_work_order_now
    from celerp.projections.engine import ProjectionEngine

    order_id, component_id, old_barcode = await _seed_run(factory, company_id, user, i)
    new_barcode = f"3{i:05d}0000000"
    s_completion, s_barcode = factory(), factory()
    outcome: dict[str, BaseException | None] = {"completion": None, "barcode": None}
    completion_lock_order: list[str] = []

    completion_holds_component_lock = asyncio.Event()
    orig_locked_projection = ProjectionEngine._locked_projection
    orig_lock_namespace_services = inventory_services.lock_item_code_namespace

    async def _patched_locked_projection(session, entry):
        # Real acquisition, never delayed - only observed, and only to learn the one instant
        # completion has issued the racing component (this race's session and entity only).
        result = await orig_locked_projection(session, entry)
        if session is s_completion and entry.entity_id == component_id and entry.event_type == "item.consumed":
            completion_holds_component_lock.set()
            completion_lock_order.append("item")
        return result

    async def _patched_lock_namespace_services(session, cid):
        # allocate_internal_codes' internal call and, on the fixed tree,
        # _lock_code_namespace_for_completion: real acquisition, never delayed, only
        # observed, to record when the completion session claims the company lock relative
        # to the racing component's item lock above.
        if session is s_completion and cid == company_id:
            completion_lock_order.append("company")
        return await orig_lock_namespace_services(session, cid)

    async def _run_completion() -> None:
        try:
            await _complete_work_order_now(s_completion, company_id, user, order_id, 2.0, {})
            await s_completion.commit()
        except Exception as exc:  # noqa: BLE001 - captured for the race assertion, not swallowed
            outcome["completion"] = exc
            await s_completion.rollback()
        finally:
            # Safety net: never leave the barcode edit's bounded start-wait as the only thing
            # standing between completion erroring out before consuming the component and a
            # real hang.
            completion_holds_component_lock.set()

    async def _run_barcode_edit() -> None:
        try:
            # Bounded: if completion never reaches the racing component (e.g. it errored out
            # earlier), the barcode edit must still be able to run rather than hang.
            try:
                await asyncio.wait_for(completion_holds_component_lock.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            await patch_item(
                component_id,
                ItemPatch(fields_changed={"barcode": {"old": old_barcode, "new": new_barcode}}),
                company_id=company_id, _=None, user=user, role="owner", settings={}, session=s_barcode,
            )
            # patch_item is the raw route handler, called directly (no HTTP layer, no
            # commit-on-teardown dependency around it) - it never commits its own session,
            # matching how every other route in this module is exercised in-process elsewhere
            # in this suite. Without this the edit's transaction, and the company lock it
            # holds, would stay open (and keep blocking the other side) until s_barcode.close()
            # much later, which is a test-harness artifact, not the race under test.
            await s_barcode.commit()
        except Exception as exc:  # noqa: BLE001 - captured for the race assertion, not swallowed
            outcome["barcode"] = exc
            await s_barcode.rollback()

    ProjectionEngine._locked_projection = staticmethod(_patched_locked_projection)
    inventory_services.lock_item_code_namespace = _patched_lock_namespace_services
    try:
        await asyncio.wait_for(
            asyncio.gather(_run_completion(), _run_barcode_edit()), timeout=15
        )
    finally:
        ProjectionEngine._locked_projection = orig_locked_projection
        inventory_services.lock_item_code_namespace = orig_lock_namespace_services
        await s_completion.close()
        await s_barcode.close()

    return outcome["completion"], outcome["barcode"], order_id, component_id, new_barcode, completion_lock_order


@pytest.mark.asyncio
async def test_completion_races_barcode_edit_without_deadlock(_db_engine):
    """Race a manufacturing completion against a concurrent barcode edit on one of its own
    components, repeatedly, with the interleaving pinned deterministically (see _race_once).
    Both transactions must always serialize and commit cleanly (the run and the barcode both
    land in their new state) with no Postgres deadlock; separately, and on every race, the
    completion session must claim the company lock no later than the racing component's item
    lock (see _race_once's docstring for why that ordering, not a forced two-way deadlock, is
    the fix's actual, provable guarantee for this pairing) - true after the fix, false
    (item-then-company) before it."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    deadlocks: list[tuple[int, str, str]] = []
    other_failures: list[tuple[int, str, str]] = []
    order_violations: list[tuple[int, list[str]]] = []
    try:
        async with factory() as s:
            s.add(Company(id=company_id, name="Lockrace Co", slug=f"lockrace-{company_id.hex[:8]}"))
            s.add(User(id=user_id, email=f"race-{user_id.hex[:8]}@lockrace.test", name="Race User",
                       auth_hash="x"))
            await s.commit()
        # A bare id/.id accessor is all production code reads off `user` on these paths
        # (actor_id on emitted events); the FK target is the committed row above.
        user = types.SimpleNamespace(id=user_id)

        for i in range(_RACES):
            completion_exc, barcode_exc, order_id, component_id, new_barcode, lock_order = await _race_once(
                factory, company_id, user, i
            )
            # The fix's own guarantee, checked directly: the completion session must claim
            # the company lock no later than the racing component's item lock, on every race,
            # not just the ones that happened to also error - a concurrent barcode edit always
            # claims company then item, so the reverse on the completion side is exactly the
            # ordering that lets the two cross.
            if lock_order.index("company") > lock_order.index("item"):
                order_violations.append((i, lock_order))
            for label, exc in (("completion", completion_exc), ("barcode-edit", barcode_exc)):
                if exc is None:
                    continue
                if _is_deadlock(exc):
                    deadlocks.append((i, label, str(exc)))
                else:
                    other_failures.append((i, label, repr(exc)))

            if completion_exc is None and barcode_exc is None:
                # Both sides won the race cleanly: verify the actual outcome, not only the
                # absence of an exception - the run completed and the barcode edit landed.
                async with factory() as s:
                    order = await s.get(Projection, {"company_id": company_id, "entity_id": order_id})
                    component = await s.get(Projection, {"company_id": company_id, "entity_id": component_id})
                assert order.state["status"] == "completed"
                assert component.state["barcode"] == new_barcode

        assert not other_failures, f"race produced unexpected non-deadlock failures: {other_failures}"
        assert not deadlocks, (
            f"deadlock detected on {len(deadlocks)}/{_RACES} completion-vs-barcode-edit races "
            f"(sample: {deadlocks[:3]})"
        )
        assert not order_violations, (
            f"completion claimed the item lock before the company lock on "
            f"{len(order_violations)}/{_RACES} races (sample: {order_violations[:3]}) - a "
            f"concurrent barcode edit claims company then item, so this ordering is exactly "
            f"what lets the two cross"
        )
    finally:
        await _cleanup(factory, company_id, user_id)
