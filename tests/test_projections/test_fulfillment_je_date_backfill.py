# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for legacy fulfillment JE date repair."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text

from celerp.events.engine import emit_event
from celerp.migrations._data_reconcile import get_meta
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services.business_time import business_date_at
from celerp.services.fulfillment_je_date_backfill import (
    FULFILLMENT_JE_DATE_BACKFILL_KEY,
    run_fulfillment_je_date_backfill,
)
from celerp.services.pick import PickLine, PickResult


async def _clear_marker(session):
    # Other migration tests may drop this non-model table. Use the canonical
    # metadata primitive to recreate it before clearing this test's marker.
    conn = await session.connection()
    await conn.run_sync(
        lambda c: get_meta(c, FULFILLMENT_JE_DATE_BACKFILL_KEY)
    )
    await session.execute(
        text("DELETE FROM instance_meta WHERE key = :k"),
        {"k": FULFILLMENT_JE_DATE_BACKFILL_KEY},
    )


async def _marker(session):
    conn = await session.connection()
    return await conn.run_sync(
        lambda c: get_meta(c, FULFILLMENT_JE_DATE_BACKFILL_KEY)
    )


async def _company_user(session, name="SyntheticCo", tz="UTC", lock_date=None):
    cid = uuid.uuid4()
    uid = uuid.uuid4()
    settings = {}
    if tz is not None:
        settings["timezone"] = tz
    if lock_date is not None:
        settings["lock_date"] = lock_date
    session.add(
        Company(
            id=cid,
            name=name,
            slug=f"synthetic-{cid.hex[:10]}",
            settings=settings,
        )
    )
    session.add(
        User(
            id=uid,
            email=f"synthetic-{uid.hex}@example.invalid",
            name="Synthetic User",
        )
    )
    await session.flush()
    return cid, uid


def _entries(amount=13.0):
    return [
        {"account": "5100", "debit": amount, "credit": 0.0},
        {"account": "1130-P", "debit": 0.0, "credit": amount},
    ]


async def _doc_fulfilled(session, cid, uid, doc_id, fulfilled_at, *, cycle=0):
    return await emit_event(
        session,
        company_id=cid,
        entity_id=doc_id,
        entity_type="doc",
        event_type="doc.fulfilled",
        data={
            "fulfilled_items": [],
            "fulfilled_by": str(uid),
            "fulfilled_at": fulfilled_at,
            "strategy": "fifo",
            "total_cogs": 13.0,
        },
        actor_id=uid,
        location_id=None,
        source="fulfillment",
        idempotency_key=f"{doc_id}:synthetic:fulfilled:{cycle}",
        metadata_={},
    )


async def _doc_partially_fulfilled(session, cid, uid, doc_id, fulfilled_at, *, cycle=0):
    return await emit_event(
        session,
        company_id=cid,
        entity_id=doc_id,
        entity_type="doc",
        event_type="doc.partially_fulfilled",
        data={
            "fulfilled_items": [],
            "unfulfilled_items": [{"sku": "SYNTH", "short_qty": 1.0}],
            "fulfilled_by": str(uid),
            "fulfilled_at": fulfilled_at,
            "strategy": "fifo",
        },
        actor_id=uid,
        location_id=None,
        source="fulfillment",
        idempotency_key=f"{doc_id}:synthetic:partial:{cycle}",
        metadata_={},
    )


async def _doc_reversed(session, cid, uid, doc_id, *, cycle=0):
    return await emit_event(
        session,
        company_id=cid,
        entity_id=doc_id,
        entity_type="doc",
        event_type="doc.fulfillment_reversed",
        data={
            "reversed_items": [],
            "reversed_by": str(uid),
            "reason": "synthetic",
        },
        actor_id=uid,
        location_id=None,
        source="fulfillment",
        idempotency_key=f"{doc_id}:synthetic:reversed:{cycle}",
        metadata_={},
    )


async def _legacy_pair(
    session,
    cid,
    uid,
    *,
    doc_id="doc:SYNTH-1",
    cycle=0,
    source="auto_je",
    metadata=None,
    entries=None,
    created_extra=None,
    ledger_ts=None,
):
    tag = "fulfill" if cycle == 0 else f"fulfill-{cycle}"
    je_id = f"je:auto:{doc_id}:{tag}"
    meta = (
        {"trigger": "doc.fulfilled", "doc_id": doc_id}
        if metadata is None
        else metadata
    )
    data = {
        "memo": f"Auto JE for {doc_id} fulfilled (COGS)",
        "entries": entries or _entries(),
    }
    if created_extra:
        data.update(created_extra)
    created = await emit_event(
        session,
        company_id=cid,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data=data,
        actor_id=uid,
        location_id=None,
        source=source,
        idempotency_key=f"{je_id}:create",
        metadata_=meta,
    )
    posted = await emit_event(
        session,
        company_id=cid,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={},
        actor_id=uid,
        location_id=None,
        source=source,
        idempotency_key=f"{je_id}:posted",
        metadata_=meta,
    )
    if ledger_ts is not None:
        created.ts = ledger_ts
        posted.ts = ledger_ts + timedelta(seconds=1)
        await session.flush()
    return je_id, created, posted


async def _void_je(session, cid, uid, je_id):
    await emit_event(
        session,
        company_id=cid,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.voided",
        data={"reason": "synthetic"},
        actor_id=uid,
        location_id=None,
        source="auto_je",
        idempotency_key=f"{je_id}:synthetic:void",
        metadata_={},
    )


async def _state(session, cid, entity_id):
    session.expire_all()
    row = await session.get(Projection, (cid, entity_id))
    return None if row is None else row.state


async def _repair_events(session, cid, je_id):
    return list(
        (
            await session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.company_id == cid,
                    LedgerEntry.entity_id == je_id,
                    LedgerEntry.event_type == "sys.journal_entry.date_repaired",
                )
            )
        ).scalars().all()
    )


def test_business_date_uses_business_timezone_and_rejects_ambiguity():
    assert business_date_at(
        datetime(2024, 1, 1, 17, 30, tzinfo=timezone.utc), "Asia/Bangkok"
    ) == "2024-01-02"
    assert business_date_at(
        datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), "America/Los_Angeles"
    ) == "2023-12-31"
    assert business_date_at(
        datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), None
    ) == "2024-01-01"
    with pytest.raises(ValueError):
        business_date_at(datetime(2024, 1, 1, 2, 30), "UTC")
    with pytest.raises(ValueError):
        business_date_at(
            datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), "Synthetic/Invalid"
        )


@pytest.mark.asyncio
async def test_live_fulfillment_uses_same_business_instant(monkeypatch):
    import celerp.services.fulfill as fulfill

    fixed = datetime(2024, 1, 1, 17, 30, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    class FakeSession:
        async def get(self, model, key):
            if model is Company:
                return SimpleNamespace(settings={"timezone": "Asia/Bangkok"})
            return None

    emitted = []
    je_calls = []

    async def fake_emit(session, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=len(emitted))

    async def fake_je(session, **kwargs):
        je_calls.append(kwargs)

    monkeypatch.setattr(fulfill, "datetime", FixedDateTime)
    monkeypatch.setattr(fulfill, "emit_event", fake_emit)
    monkeypatch.setattr(fulfill.auto_je, "create_for_doc_fulfilled", fake_je)

    await fulfill.execute_fulfill(
        FakeSession(),
        doc_entity_id="doc:SYNTH-LIVE",
        doc_state={"line_items": []},
        pick_result=PickResult(
            picks=[
                PickLine(
                    item_id="item:SYNTH-LIVE",
                    sku="SYNTH",
                    pick_qty=1.0,
                    cost_price=13.0,
                    action="full",
                )
            ]
        ),
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        doc_type="invoice",
    )

    fulfilled = next(e for e in emitted if e["event_type"] == "doc.fulfilled")
    assert fulfilled["data"]["fulfilled_at"] == fixed.isoformat()
    assert je_calls[0]["ts"] == "2024-01-02"


@pytest.mark.asyncio
async def test_repair_uses_fulfillment_instant_not_ledger_timestamp_and_rebuilds(session):
    await _clear_marker(session)
    cid, uid = await _company_user(session, tz="Asia/Bangkok")
    doc_id = "doc:SYNTH-REPAIR"
    await _doc_fulfilled(
        session, cid, uid, doc_id, "2024-02-03T17:30:00+00:00"
    )
    je_id, created, _ = await _legacy_pair(
        session,
        cid,
        uid,
        doc_id=doc_id,
        ledger_ts=datetime(2024, 2, 3, 16, 0, tzinfo=timezone.utc),
    )
    create_id = created.id
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result == {"changed": True, "repaired": 1, "deferred": 0, "skipped": 0}
    assert (await _state(session, cid, je_id))["ts"] == "2024-02-04"

    repairs = await _repair_events(session, cid, je_id)
    assert len(repairs) == 1
    assert repairs[0].idempotency_key == f"repair:fulfillment-je-date:{create_id}"
    assert await _marker(session) == "done"

    await ProjectionEngine.rebuild(session, company_id=cid)
    await session.commit()
    assert (await _state(session, cid, je_id))["ts"] == "2024-02-04"


@pytest.mark.asyncio
async def test_same_je_id_stays_company_scoped(session):
    await _clear_marker(session)
    a, ua = await _company_user(session, "SyntheticA", "Asia/Bangkok")
    b, ub = await _company_user(session, "SyntheticB", "America/Los_Angeles")
    doc_id = "doc:SAME-SYNTH"

    await _doc_fulfilled(session, a, ua, doc_id, "2024-02-03T17:30:00+00:00")
    je_a, _, _ = await _legacy_pair(session, a, ua, doc_id=doc_id)

    await _doc_fulfilled(session, b, ub, doc_id, "2024-02-03T02:30:00+00:00")
    je_b, _, _ = await _legacy_pair(session, b, ub, doc_id=doc_id)
    assert je_a == je_b
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 2
    assert (await _state(session, a, je_a))["ts"] == "2024-02-04"
    assert (await _state(session, b, je_b))["ts"] == "2024-02-02"


@pytest.mark.asyncio
async def test_cycle_evidence_repairs_only_live_unvoided_family(session):
    await _clear_marker(session)
    cid, uid = await _company_user(session)
    doc_id = "doc:CYCLE-SYNTH"

    await _doc_fulfilled(session, cid, uid, doc_id, "2024-03-01T10:00:00+00:00", cycle=0)
    old_je, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id, cycle=0)
    await _void_je(session, cid, uid, old_je)
    await _doc_reversed(session, cid, uid, doc_id, cycle=0)

    await _doc_fulfilled(session, cid, uid, doc_id, "2024-03-02T10:00:00+00:00", cycle=1)
    live_je, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id, cycle=1)
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 1
    assert result["skipped"] == 1
    assert not await _repair_events(session, cid, old_je)
    assert (await _state(session, cid, live_je))["ts"] == "2024-03-02"


@pytest.mark.asyncio
async def test_missing_projection_defers_then_rebuild_allows_repair(session):
    await _clear_marker(session)
    cid, uid = await _company_user(session)
    doc_id = "doc:PROJECTION-SYNTH"
    await _doc_fulfilled(session, cid, uid, doc_id, "2024-04-05T12:00:00+00:00")
    je_id, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id)
    await session.commit()

    await session.execute(
        delete(Projection).where(
            Projection.company_id == cid,
            Projection.entity_id == je_id,
        )
    )
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["deferred"] == 1
    assert not await _repair_events(session, cid, je_id)
    assert await _marker(session) is None

    await ProjectionEngine.rebuild(session, company_id=cid)
    await session.commit()
    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 1
    assert (await _state(session, cid, je_id))["ts"] == "2024-04-05"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["invalid_timezone", "conflicting_date", "locked_period"])
async def test_repairable_runtime_blockers_defer_without_mutation(session, case):
    await _clear_marker(session)
    tz = "Synthetic/Invalid" if case == "invalid_timezone" else "UTC"
    lock = "2024-12-31" if case == "locked_period" else None
    cid, uid = await _company_user(session, tz=tz, lock_date=lock)
    doc_id = f"doc:{case.upper()}-SYNTH"
    await _doc_fulfilled(session, cid, uid, doc_id, "2024-05-06T12:00:00+00:00")
    je_id, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id)
    if case == "conflicting_date":
        row = await session.get(Projection, (cid, je_id))
        row.state = {**(row.state or {}), "ts": "2024-05-07"}
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 0
    assert result["deferred"] == 1
    assert not await _repair_events(session, cid, je_id)
    assert await _marker(session) is None


@pytest.mark.asyncio
async def test_existing_same_date_gets_durable_repair_event(session):
    await _clear_marker(session)
    cid, uid = await _company_user(session)
    doc_id = "doc:SAME-DATE-SYNTH"
    await _doc_fulfilled(session, cid, uid, doc_id, "2024-06-07T12:00:00+00:00")
    je_id, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id)
    row = await session.get(Projection, (cid, je_id))
    row.state = {**(row.state or {}), "ts": "2024-06-07"}
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 1
    assert len(await _repair_events(session, cid, je_id)) == 1

    await ProjectionEngine.rebuild(session, company_id=cid)
    await session.commit()
    assert (await _state(session, cid, je_id))["ts"] == "2024-06-07"


@pytest.mark.asyncio
async def test_missing_immutable_fulfillment_evidence_is_skipped_not_guessed(session):
    await _clear_marker(session)
    cid, uid = await _company_user(session)
    doc_id = "doc:MISSING-EVIDENCE-SYNTH"
    je_id, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id)
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 0
    assert result["deferred"] == 0
    assert result["skipped"] == 1
    assert not await _repair_events(session, cid, je_id)
    assert await _marker(session) == "done"


@pytest.mark.asyncio
async def test_partial_fulfillment_uses_its_recorded_instant(session):
    await _clear_marker(session)
    cid, uid = await _company_user(session, tz="Asia/Bangkok")
    doc_id = "doc:PARTIAL-SYNTH"
    await _doc_partially_fulfilled(
        session, cid, uid, doc_id, "2024-07-08T17:30:00+00:00"
    )
    je_id, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id)
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 1
    assert (await _state(session, cid, je_id))["ts"] == "2024-07-09"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_source", "wrong_metadata", "wrong_entries", "already_dated"])
async def test_near_matches_are_not_candidates(session, case):
    await _clear_marker(session)
    cid, uid = await _company_user(session)
    doc_id = f"doc:{case.upper()}-SYNTH"
    await _doc_fulfilled(session, cid, uid, doc_id, "2024-08-09T12:00:00+00:00")
    kwargs = {}
    if case == "wrong_source":
        kwargs["source"] = "test"
    elif case == "wrong_metadata":
        kwargs["metadata"] = {"trigger": "doc.finalized", "doc_id": doc_id}
    elif case == "wrong_entries":
        kwargs["entries"] = [
            {"account": "5100", "debit": 13.0, "credit": 0.0},
            {"account": "1130", "debit": 0.0, "credit": 13.0},
        ]
    elif case == "already_dated":
        kwargs["created_extra"] = {"ts": "2024-08-09"}
    je_id, _, _ = await _legacy_pair(session, cid, uid, doc_id=doc_id, **kwargs)
    await session.commit()

    result = await run_fulfillment_je_date_backfill(session)
    await session.commit()
    assert result["repaired"] == 0
    assert not await _repair_events(session, cid, je_id)
    assert await _marker(session) == "done"



@pytest.mark.asyncio
async def test_non_cogs_fulfillment_does_not_require_valid_timezone(monkeypatch):
    import celerp.services.fulfill as fulfill

    class FakeSession:
        async def get(self, model, key):
            if model is Company:
                return SimpleNamespace(settings={"timezone": "Synthetic/Invalid"})
            return None

    emitted = []
    je_calls = []

    async def fake_emit(session, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=len(emitted))

    async def fake_je(session, **kwargs):
        je_calls.append(kwargs)

    monkeypatch.setattr(fulfill, "emit_event", fake_emit)
    monkeypatch.setattr(fulfill.auto_je, "create_for_doc_fulfilled", fake_je)

    await fulfill.execute_fulfill(
        FakeSession(),
        doc_entity_id="doc:SYNTH-MEMO",
        doc_state={"line_items": []},
        pick_result=PickResult(
            picks=[
                PickLine(
                    item_id="item:SYNTH-MEMO",
                    sku="SYNTH",
                    pick_qty=1.0,
                    cost_price=13.0,
                    action="full",
                )
            ]
        ),
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        doc_type="memo",
    )

    assert any(e["event_type"] == "doc.fulfilled" for e in emitted)
    assert je_calls == []



@pytest.mark.asyncio
async def test_cogs_fulfillment_invalid_timezone_fails_before_events(monkeypatch):
    import celerp.services.fulfill as fulfill

    class FakeSession:
        async def get(self, model, key):
            if model is Company:
                return SimpleNamespace(settings={"timezone": "Synthetic/Invalid"})
            return None

    emitted = []

    async def fake_emit(session, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=len(emitted))

    monkeypatch.setattr(fulfill, "emit_event", fake_emit)

    with pytest.raises(ValueError, match="invalid business timezone"):
        await fulfill.execute_fulfill(
            FakeSession(),
            doc_entity_id="doc:SYNTH-BAD-TZ",
            doc_state={"line_items": []},
            pick_result=PickResult(
                picks=[
                    PickLine(
                        item_id="item:SYNTH-BAD-TZ",
                        sku="SYNTH",
                        pick_qty=1.0,
                        cost_price=13.0,
                        action="full",
                    )
                ]
            ),
            company_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            doc_type="invoice",
        )

    assert emitted == []
