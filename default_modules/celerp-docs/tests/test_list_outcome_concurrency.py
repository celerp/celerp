# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Outcome-level proof that _get_list_for_update's row lock keeps concurrent read-modify-writes
over a list's line_items correct, not merely that a lock is requested.

Every scan/patch/finalize/count/adjust route reads the whole line_items array, mutates it in
Python, and emits an event carrying the FULL new array. The projection fold overwrites line_items
wholesale from that array, so two writers that each read the same base and each write their own
absolute array lose one update: whichever event lands last wins the field. _get_list_for_update
loads the row SELECT ... FOR UPDATE, so the second writer's READ blocks until the first commits,
then re-reads the committed array and builds on top - both changes survive.

These use independent sessions on the shared engine with real commits (not the savepoint session):
a row lock is only observable, and a lost update only forms, between two separately committed
transactions. They assert the FINAL COMMITTED state, so neutralizing the lock (a plain unlocked
load) makes each test fail on the lost update it targets."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp_accounting.routes import seed_chart_of_accounts
from celerp_accounting.models import Account
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_company(factory):
    """A committed company plus a committed user: the routes emit events with actor_id=user.id,
    which foreign-keys to users.id, so the actor must be a real row (unlike the lock-request test,
    whose routes 409 before emitting)."""
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Outcome Co", slug=f"outcome-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"race-{user_id.hex[:8]}@outcome.test", name="Race User",
                   auth_hash="x"))
        await s.flush()
        await seed_chart_of_accounts(s, company_id)
        await s.commit()
    return company_id, user_id, types.SimpleNamespace(id=user_id)


async def _cleanup(factory, company_id, user_id):
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Account).where(Account.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


def _barcode() -> str:
    return str(uuid.uuid4().int)[:12]


async def _seed_item(factory, company_id, user, *, sku, name, qty, barcode) -> str:
    """An AVAILABLE item the scan resolver matches by barcode (a draft item would be rejected)."""
    entity_id = f"item:{uuid.uuid4()}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="item",
            event_type="item.created",
            data={"status": "available", "sku": sku, "name": name, "quantity": qty,
                  "barcode": barcode, "cost_price": 1},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _seed_list(factory, company_id, user, *, list_type, ref_id, line_items) -> str:
    entity_id = f"list:{ref_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="list",
            event_type="list.created",
            data={"list_type": list_type, "status": "draft", "ref_id": ref_id,
                  "line_items": line_items},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _read(factory, company_id, entity_id) -> Projection:
    async with factory() as s:
        return await s.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                           populate_existing=True)


async def _state(factory, company_id, entity_id) -> dict:
    return dict((await _read(factory, company_id, entity_id)).state)


async def _version(factory, company_id, entity_id) -> int:
    return (await _read(factory, company_id, entity_id)).version


async def _item_qty(factory, company_id, entity_id) -> float:
    return float((await _read(factory, company_id, entity_id)).state.get("quantity") or 0)


@pytest.mark.asyncio
async def test_two_concurrent_scan_batches_both_survive(_db_engine):
    """Two scans of different barcodes onto the same draft quotation, on separately committed
    sessions, must both land: two lines and both run keys. Without the row lock both read the
    empty array and the second's commit clobbers the first - one line, one run key survive."""
    from celerp_docs.routes import scan_list, ListScanBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc_x, bc_y = _barcode(), _barcode()
        x_id = await _seed_item(factory, company_id, user, sku="X", name="Item X", qty=5, barcode=bc_x)
        y_id = await _seed_item(factory, company_id, user, sku="Y", name="Item Y", qty=5, barcode=bc_y)
        list_id = await _seed_list(factory, company_id, user, list_type="quotation", ref_id="Q1",
                                   line_items=[])
        s1, s2 = factory(), factory()
        outcome: dict = {}

        async def _scan(label, barcode, run_key, session):
            try:
                outcome[label] = await scan_list(
                    list_id, ListScanBody(barcode=barcode, run_key=run_key),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the race assertion
                outcome[label] = exc
                await session.rollback()

        try:
            await asyncio.wait_for(asyncio.gather(
                _scan("x", bc_x, "kX", s1), _scan("y", bc_y, "kY", s2)), timeout=20)
        finally:
            await s1.close()
            await s2.close()

        assert not isinstance(outcome["x"], Exception), f"scan X raised: {outcome['x']!r}"
        assert not isinstance(outcome["y"], Exception), f"scan Y raised: {outcome['y']!r}"
        assert outcome["x"]["scanned"] == 1
        assert outcome["y"]["scanned"] == 1

        state = await _state(factory, company_id, list_id)
        lines = state.get("line_items") or []
        item_ids = {l.get("item_id") for l in lines}
        assert len(lines) == 2, f"both scanned lines must survive; got {len(lines)}: {lines}"
        assert item_ids == {x_id, y_id}, f"both scanned items must survive; got {item_ids}"
        run_keys = {r.get("key") for r in (state.get("scan_runs") or [])}
        assert run_keys == {"kX", "kY"}, f"both scan runs must survive; got {run_keys}"
    finally:
        await _cleanup(factory, company_id, user_id)


async def _seed_memo(factory, company_id, user, *, ref_id, line_items) -> str:
    """A committed, fulfillable memo: doc.created (draft) then doc.sent (status 'sent', which is a
    fulfillable AND closable memo status). Lines carry entity_id so both fulfill_lines and close_doc
    resolve each line to its item projection."""
    entity_id = f"doc:{ref_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.created",
            data={"doc_type": "memo", "status": "draft", "ref_id": ref_id, "line_items": line_items},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.sent", data={},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


@pytest.mark.asyncio
async def test_close_fulfill_race_mutually_exclusive(_db_engine):
    """close_doc and fulfill_lines race on the same one-line memo. The pair is mutually exclusive:
    the final committed state must never be BOTH memo closed AND its item memo_out (the illegal
    'closed a memo whose stone is still at the customer' state). Without a doc-row lock both handlers
    read the doc unlocked and both commit: close sees the item still available (fulfill hasn't
    committed yet) and closes; fulfill ships the item to memo_out. Final state is closed + memo_out -
    the invariant FAILS. With the FOR UPDATE lock exactly one wins and the loser 409s."""
    from celerp_docs.routes import close_doc, fulfill_lines, DocCloseBody, FulfillLinesRequest

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        item_id = await _seed_item(factory, company_id, user, sku="M1", name="Stone 1", qty=1,
                                   barcode=_barcode())
        memo_id = await _seed_memo(
            factory, company_id, user, ref_id="MEMO-CF",
            line_items=[{"entity_id": item_id, "sku": "M1", "name": "Stone 1", "quantity": 1,
                         "unit_price": 10, "sell_by": "piece"}])
        s1, s2 = factory(), factory()
        outcome: dict = {}

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (fulfill won) is an allowed outcome
                outcome["close"] = exc
                await session.rollback()

        async def _fulfill(session):
            try:
                outcome["fulfill"] = await fulfill_lines(
                    memo_id, FulfillLinesRequest(line_entity_ids=[item_id]),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (close won) is an allowed outcome
                outcome["fulfill"] = exc
                await session.rollback()

        try:
            await asyncio.wait_for(asyncio.gather(_close(s1), _fulfill(s2)), timeout=20)
        finally:
            await s1.close()
            await s2.close()

        memo_state = await _state(factory, company_id, memo_id)
        item_state = await _state(factory, company_id, item_id)
        memo_closed = memo_state.get("status") == "closed"
        item_out = item_state.get("status") == "memo_out"

        assert not (memo_closed and item_out), (
            "illegal state: the memo is closed AND its line item is still memo_out (out at the "
            f"customer). memo status={memo_state.get('status')!r}, item status="
            f"{item_state.get('status')!r}; outcomes close={outcome.get('close')!r}, "
            f"fulfill={outcome.get('fulfill')!r}")

        # Exactly one side wins; the loser 409s. (Secondary to the state invariant above.)
        n_409 = sum(
            1 for o in (outcome.get("close"), outcome.get("fulfill"))
            if isinstance(o, HTTPException) and o.status_code == 409)
        assert n_409 == 1, (
            f"exactly one of close/fulfill must 409; got {n_409}. close={outcome.get('close')!r}, "
            f"fulfill={outcome.get('fulfill')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_send_close_race_no_unclose(_db_engine):
    """A Close that has committed must never be silently un-done by a Send whose read straddled it.
    The durable invariant: once a doc.closed event is in the ledger, the memo's committed status is
    'closed', never overwritten back to 'sent' by a racing doc.sent.

    The interleaving that breaks this at merge-base: Send reads the doc (unlocked, sees a live memo),
    Close then commits doc.closed, Send then commits doc.sent - which the projection fold overwrites
    to 'sent', un-closing the settled memo. The test drives exactly that order: Close runs first and
    fully commits, then a Send whose intent formed against the pre-close memo lands. At merge-base
    Send's unlocked read lets its doc.sent overwrite the committed close (final 'sent', RED). With
    the FOR UPDATE lock Send re-reads the committed 'closed' status under the lock and 409s, so the
    close stays durable (final 'closed', and the loser is the 409)."""
    from celerp_docs.routes import send_doc, close_doc, DocSendBody, DocCloseBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        # A resolved memo (no line memo_out) so close is admissible: one line whose item is left
        # 'available', never shipped. close counts memo_out lines as pending; there are none.
        item_id = await _seed_item(factory, company_id, user, sku="M2", name="Stone 2", qty=1,
                                   barcode=_barcode())
        memo_id = await _seed_memo(
            factory, company_id, user, ref_id="MEMO-SC",
            line_items=[{"entity_id": item_id, "sku": "M2", "name": "Stone 2", "quantity": 1,
                         "unit_price": 10, "sell_by": "piece"}])
        s_close, s_send = factory(), factory()
        outcome: dict = {}

        # Close commits first; Send is released only after, so its write lands on the just-closed
        # memo - the un-close ordering. The lock (fix) is what forces Send to re-read that committed
        # close instead of clobbering it.
        close_done = asyncio.Event()

        async def _close(session):
            try:
                outcome["close"] = await close_doc(
                    memo_id, DocCloseBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the race assertion
                outcome["close"] = exc
                await session.rollback()
            finally:
                close_done.set()

        async def _send(session):
            await close_done.wait()
            try:
                outcome["send"] = await send_doc(
                    memo_id, DocSendBody(), company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (memo already closed) is the fixed outcome
                outcome["send"] = exc
                await session.rollback()

        try:
            await asyncio.wait_for(asyncio.gather(_close(s_close), _send(s_send)), timeout=20)
        finally:
            await s_close.close()
            await s_send.close()

        memo_state = await _state(factory, company_id, memo_id)
        close_committed = not isinstance(outcome.get("close"), Exception)
        assert close_committed, f"close should have committed first; got {outcome.get('close')!r}"

        # Durable close: the committed doc.closed is never overwritten back to 'sent' by the racing
        # send. At merge-base the unlocked send clobbers it (final 'sent'); the fix makes send 409.
        assert memo_state.get("status") == "closed", (
            "un-close: a committed Close was overwritten - the racing Send's doc.sent reverted the "
            f"memo to {memo_state.get('status')!r}. outcomes close={outcome.get('close')!r}, "
            f"send={outcome.get('send')!r}")
        # The send that tried to un-close a settled memo is rejected, not silently applied.
        assert isinstance(outcome.get("send"), HTTPException) and outcome["send"].status_code == 409, (
            f"send on a closed memo must 409, not commit; got {outcome.get('send')!r}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_scan_versus_versioned_save_never_drops_scanned_line(_db_engine):
    """A scan (adds X) races a versioned line_items save (replaces with [Y]) on the same draft
    quotation. The scanned line X must always survive. The patch may 409 on a stale version (an
    allowed outcome); if it succeeds, Y is present too. Without the lock the patch passes its
    stale version check and its replacement drops X - X absent."""
    from celerp_docs.routes import scan_list, patch_list, ListScanBody, ListPatch

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc_x = _barcode()
        x_id = await _seed_item(factory, company_id, user, sku="X", name="Item X", qty=5, barcode=bc_x)
        y_id = await _seed_item(factory, company_id, user, sku="Y", name="Item Y", qty=5,
                                barcode=_barcode())
        list_id = await _seed_list(factory, company_id, user, list_type="quotation", ref_id="Q2",
                                   line_items=[])
        v0 = await _version(factory, company_id, list_id)
        s1, s2 = factory(), factory()
        outcome: dict = {}

        async def _scan(session):
            try:
                outcome["scan"] = await scan_list(
                    list_id, ListScanBody(barcode=bc_x, run_key="kX"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the race assertion
                outcome["scan"] = exc
                await session.rollback()

        async def _patch(session):
            try:
                outcome["patch"] = await patch_list(
                    list_id,
                    ListPatch(fields_changed={"line_items": {"new": [
                        {"item_id": y_id, "sku": "Y", "quantity": 1}]}}, expected_version=v0),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a stale-version 409 is an allowed outcome
                outcome["patch"] = exc
                await session.rollback()

        try:
            await asyncio.wait_for(asyncio.gather(_scan(s1), _patch(s2)), timeout=20)
        finally:
            await s1.close()
            await s2.close()

        assert not isinstance(outcome["scan"], Exception), f"scan raised: {outcome['scan']!r}"
        assert outcome["scan"]["scanned"] == 1

        patch_409 = (isinstance(outcome["patch"], HTTPException)
                     and outcome["patch"].status_code == 409)
        if isinstance(outcome["patch"], Exception) and not patch_409:
            raise AssertionError(f"patch failed for an unexpected reason: {outcome['patch']!r}")

        item_ids = {l.get("item_id") for l in (await _state(factory, company_id, list_id)).get("line_items") or []}
        assert x_id in item_ids, f"the scanned line X must always survive; got {item_ids}"
        if patch_409:
            assert item_ids == {x_id}, f"patch 409'd so only X should be present; got {item_ids}"
        else:
            assert item_ids == {x_id, y_id}, f"patch succeeded so X and Y both present; got {item_ids}"
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_scan_versus_finalize_audit_freezes_consistently(_db_engine):
    """A draft-audit scan (adds Z) races finalize (freezes on_hand over the whole array). Final
    status is finalized, and EVERY final line carries a numeric on_hand (proof the freeze saw the
    exact committed array). If the scan reported it added Z, Z's line is in the final list. Without
    the lock, a scan-committed-last run leaves an unfrozen Z on the finalized audit (a line with no
    on_hand), and a finalize-committed-last run drops the scanned Z."""
    from celerp_docs.routes import scan_list, finalize_list, ListScanBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        a_id = await _seed_item(factory, company_id, user, sku="A", name="Item A", qty=10,
                                barcode=_barcode())
        bc_z = _barcode()
        z_id = await _seed_item(factory, company_id, user, sku="Z", name="Item Z", qty=4, barcode=bc_z)
        list_id = await _seed_list(factory, company_id, user, list_type="audit", ref_id="AUD1",
                                   line_items=[{"item_id": a_id, "sku": "A", "quantity": 1}])
        s1, s2 = factory(), factory()
        outcome: dict = {}

        async def _scan(session):
            try:
                outcome["scan"] = await scan_list(
                    list_id, ListScanBody(barcode=bc_z, run_key="kZ"),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the race assertion
                outcome["scan"] = exc
                await session.rollback()

        async def _finalize(session):
            try:
                outcome["finalize"] = await finalize_list(
                    list_id, company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the race assertion
                outcome["finalize"] = exc
                await session.rollback()

        try:
            await asyncio.wait_for(asyncio.gather(_scan(s1), _finalize(s2)), timeout=20)
        finally:
            await s1.close()
            await s2.close()

        assert not isinstance(outcome["finalize"], Exception), f"finalize raised: {outcome['finalize']!r}"

        state = await _state(factory, company_id, list_id)
        assert state.get("status") == "finalized", f"status must be finalized; got {state.get('status')}"
        lines = state.get("line_items") or []
        for l in lines:
            assert isinstance(l.get("on_hand"), (int, float)), (
                f"every finalized audit line must carry a frozen numeric on_hand; offending line "
                f"{l} in {lines}")

        scan_res = outcome["scan"]
        scan_added_z = (not isinstance(scan_res, Exception)
                        and any(r.get("item_id") == z_id and r.get("state") == "added"
                                for r in scan_res.get("results", [])))
        if scan_added_z:
            assert z_id in {l.get("item_id") for l in lines}, (
                f"scan added Z so Z must be in the finalized list; got "
                f"{[l.get('item_id') for l in lines]}")
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_count_versus_adjust_honors_committed_count(_db_engine):
    """On a finalized audit with A pre-counted (7) and committed, a fresh B-count (3) races the
    terminal adjust. Adjust closes the list and A's committed count of 7 is always honored. If the
    B-count committed (no 409), B's item qty is 3 (its count was included, not discarded); if it
    409'd (adjust closed first), B stays 10. Without the lock, adjust reads the pre-B-count array,
    skips B, and closes - the B-count returns ok yet B's qty stays 10, the discarded update."""
    from celerp_docs.routes import finalize_list, set_audit_count, adjust_audit, ListCountBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        a_id = await _seed_item(factory, company_id, user, sku="A", name="Item A", qty=10,
                                barcode=_barcode())
        b_id = await _seed_item(factory, company_id, user, sku="B", name="Item B", qty=10,
                                barcode=_barcode())
        list_id = await _seed_list(
            factory, company_id, user, list_type="audit", ref_id="AUD2",
            line_items=[{"item_id": a_id, "sku": "A", "quantity": 1},
                        {"item_id": b_id, "sku": "B", "quantity": 1}])
        async with factory() as s:
            await finalize_list(list_id, company_id=company_id, _=None, user=user, session=s)
        async with factory() as s:
            await set_audit_count(list_id, a_id, ListCountBody(counted_qty=7),
                                  company_id=company_id, _=None, user=user, session=s)

        s1, s2 = factory(), factory()
        outcome: dict = {}

        async def _count_b(session):
            try:
                outcome["count"] = await set_audit_count(
                    list_id, b_id, ListCountBody(counted_qty=3),
                    company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - a 409 (adjust closed first) is allowed
                outcome["count"] = exc
                await session.rollback()

        async def _adjust(session):
            try:
                outcome["adjust"] = await adjust_audit(
                    list_id, company_id=company_id, _=None, user=user, session=session)
            except Exception as exc:  # noqa: BLE001 - captured for the race assertion
                outcome["adjust"] = exc
                await session.rollback()

        try:
            await asyncio.wait_for(asyncio.gather(_count_b(s1), _adjust(s2)), timeout=20)
        finally:
            await s1.close()
            await s2.close()

        assert not isinstance(outcome["adjust"], Exception), f"adjust raised: {outcome['adjust']!r}"

        state = await _state(factory, company_id, list_id)
        assert state.get("status") == "closed", f"adjust must close the audit; got {state.get('status')}"
        assert await _item_qty(factory, company_id, a_id) == 7, "A's pre-committed count of 7 must always be honored"

        count_409 = (isinstance(outcome["count"], HTTPException)
                     and outcome["count"].status_code == 409)
        if isinstance(outcome["count"], Exception) and not count_409:
            raise AssertionError(f"set_audit_count failed for an unexpected reason: {outcome['count']!r}")
        b_qty = await _item_qty(factory, company_id, b_id)
        if count_409:
            assert b_qty == 10, f"B-count 409'd so B stays unchanged at 10; got {b_qty}"
        else:
            assert b_qty == 3, f"B-count committed so its count of 3 must be applied; got {b_qty}"
    finally:
        await _cleanup(factory, company_id, user_id)
