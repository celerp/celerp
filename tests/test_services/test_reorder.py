# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reorder points, low-stock predicate, velocity suggestion, and the alert digest.

Covers the pure predicate (backward-compatible with the old ``quantity <= 0``),
the velocity-based suggestion, and the daily-scan digest transition/idempotency
that guarantees the alert is neither spammy nor lossy.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.services.company_lock import locked_company
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.projections.engine import ProjectionEngine
from celerp.services.reorder import (
    is_below_reorder,
    is_reorder_alert,
    reorder_point_of,
    run_all_alerts,
    suggest_reorder,
)


# ── Pure predicate ────────────────────────────────────────────────────────────

def test_scan_due_gating():
    """A company is scanned once a day: on the first tick at or after the scan hour when
    none ran that UTC day, or on any tick once the last scan is more than a day old."""
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone
    from celerp.services.reorder import _scan_due, _SCAN_HOUR_UTC

    def due(now, last=None):
        settings = {} if last is None else {"reorder_last_scan_at": last.isoformat()}
        return _scan_due(SimpleNamespace(settings=settings), now)

    at_hour = datetime(2026, 7, 5, _SCAN_HOUR_UTC, 0, tzinfo=timezone.utc)
    afternoon = at_hour.replace(hour=14)
    before_hour = at_hour - timedelta(hours=1)
    # Never scanned: due from the scan hour on, not before it.
    assert due(at_hour) is True
    assert due(afternoon) is True
    assert due(before_hour) is False
    # Scanned yesterday: due at any tick from the scan hour on, not only during it.
    assert due(afternoon, at_hour - timedelta(days=1)) is True
    assert due(afternoon, afternoon - timedelta(hours=20)) is True
    # Scanned today already: not due again that day (the once-a-day debounce).
    assert due(afternoon, afternoon - timedelta(hours=3)) is False
    assert due(afternoon, afternoon) is False
    # Scanned yesterday late, ticking before today's scan hour: not due yet.
    assert due(before_hour, before_hour - timedelta(hours=5)) is False
    # Last scan more than a day old: due even before the scan hour.
    assert due(before_hour, before_hour - timedelta(hours=25)) is True
    # A stamp that cannot be read counts as no scan.
    assert _scan_due(SimpleNamespace(settings={"reorder_last_scan_at": "not a date"}), afternoon) is True


def test_new_fields_in_default_schema():
    from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
    keys = {f["key"] for f in DEFAULT_ITEM_SCHEMA}
    assert {"batch_no", "reorder_point", "reorder_qty"} <= keys
    # New numeric fields are optional (no reorder rule by default) and tooltip'd.
    by_key = {f["key"]: f for f in DEFAULT_ITEM_SCHEMA}
    for k in ("reorder_point", "reorder_qty"):
        assert by_key[k]["type"] == "number"
        assert by_key[k]["required"] is False
        assert by_key[k]["tooltip_key"] == f"field.tooltip.{k}"
    assert by_key["batch_no"]["required"] is False


def test_predicate_backward_compatible_default():
    # With no reorder_point set, is_below_reorder == the legacy `quantity <= 0`.
    assert is_below_reorder({"quantity": 0}) is True
    assert is_below_reorder({"quantity": -1}) is True
    assert is_below_reorder({"quantity": 5}) is False
    assert is_below_reorder({}) is True  # missing quantity == 0


def test_predicate_threshold_aware():
    assert is_below_reorder({"quantity": 5, "reorder_point": 10}) is True
    assert is_below_reorder({"quantity": 10, "reorder_point": 10}) is True  # at threshold
    assert is_below_reorder({"quantity": 11, "reorder_point": 10}) is False


def test_reorder_alert_excludes_unset_zero():
    # An unconfigured item at qty 0 is "out of stock", NOT a reorder alert.
    assert is_reorder_alert({"quantity": 0}) is False
    assert is_reorder_alert({"quantity": 0, "reorder_point": 5}) is True
    assert is_reorder_alert({"quantity": 6, "reorder_point": 5}) is False
    assert reorder_point_of({"reorder_point": "7"}) == 7.0
    assert reorder_point_of({"reorder_point": None}) == 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _emit(session, cid, eid, event_type, data):
    await emit_event(
        session, company_id=cid, entity_id=eid, entity_type="item",
        event_type=event_type, data=data, actor_id=None, location_id=None,
        source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


async def _company(session, name):
    cid = uuid.uuid4()
    co = Company(id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={})
    session.add(co)
    await session.flush()
    return co


async def _notif_count(session, cid):
    """Notices stored for the company once the transaction commits (a notice is its transaction's last write)."""
    await session.commit()
    return (await session.execute(
        select(func.count()).select_from(Notification).where(Notification.company_id == cid)
    )).scalar() or 0


# ── Velocity suggestion ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_suggest_from_velocity(session):
    co = await _company(session, "VelCo")
    eid = "item:vel-1"
    await _emit(session, co.id, eid, "item.created", {"sku": "V", "name": "A", "quantity": 100})
    # 90 units consumed over the window -> avg_daily_out = 1/day.
    await _emit(session, co.id, eid, "item.consumed", {"quantity_consumed": 90})
    sugg = await suggest_reorder(session, co.id, eid)
    assert sugg["reorder_qty"] == 14   # ceil(1 * REVIEW_DAYS=14)
    assert sugg["reorder_point"] == 7  # ceil(1 * LEAD_DAYS=7) + safety 0


@pytest.mark.asyncio
async def test_stock_given_back_no_longer_counts_as_used(session):
    co = await _company(session, "BackCo")
    eid = "item:back-1"
    await _emit(session, co.id, eid, "item.created", {"sku": "K", "name": "A", "quantity": 100})
    await _emit(session, co.id, eid, "item.consumed", {"quantity_consumed": 180})
    # An ordinary count adjustment is not usage either way.
    await _emit(session, co.id, eid, "item.quantity.adjusted", {"new_qty": 50})
    await _emit(session, co.id, eid, "item.quantity.adjusted", {"new_qty": 140, "quantity_returned": 90})
    assert await suggest_reorder(session, co.id, eid) == {"reorder_point": 7, "reorder_qty": 14}
    await _emit(session, co.id, eid, "item.quantity.adjusted", {"new_qty": 230, "quantity_returned": 90})
    assert await suggest_reorder(session, co.id, eid) == {"reorder_point": None, "reorder_qty": None}


@pytest.mark.asyncio
async def test_suggest_no_history_is_blank(session):
    co = await _company(session, "BlankCo")
    eid = "item:blank-1"
    await _emit(session, co.id, eid, "item.created", {"sku": "B", "name": "A", "quantity": 5})
    sugg = await suggest_reorder(session, co.id, eid)
    assert sugg == {"reorder_point": None, "reorder_qty": None}  # never fabricated


# ── Alert digest: transition + idempotency + re-arm ───────────────────────────

@pytest.mark.asyncio
async def test_alert_transition_idempotency_and_rearm(session):
    co = await _company(session, "AlertCo")
    low, high = "item:low", "item:high"
    await _emit(session, co.id, low, "item.created", {"sku": "L", "name": "Low", "quantity": 2, "reorder_point": 5})
    await _emit(session, co.id, high, "item.created", {"sku": "H", "name": "High", "quantity": 10, "reorder_point": 5})
    await ProjectionEngine.rebuild(session)

    # First run: exactly one digest listing the below item; latch records it.
    notif = await run_all_alerts(session, co.id)
    assert notif is not None
    assert notif.category == "inventory"
    assert notif.action_url == "/dashboard"
    assert notif.title.startswith("1 alert")
    assert "Low stock" in notif.body and "Low" in notif.body and "High" not in notif.body
    assert await _notif_count(session, co.id) == 1

    # Second run, no change: no new notification (idempotent).
    assert await run_all_alerts(session, co.id) is None
    assert await _notif_count(session, co.id) == 1

    # Rise back above the reorder point -> no alert, latch cleared (re-arm).
    await _emit(session, co.id, low, "item.quantity.adjusted", {"new_qty": 10})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co.id) is None
    assert await _notif_count(session, co.id) == 1
    assert co.settings.get("reorder_alerted_ids") == []

    # Drop below again -> alerts again (proves re-arm).
    await _emit(session, co.id, low, "item.quantity.adjusted", {"new_qty": 1})
    await ProjectionEngine.rebuild(session)
    notif2 = await run_all_alerts(session, co.id)
    assert notif2 is not None
    assert await _notif_count(session, co.id) == 2


@pytest.mark.asyncio
async def test_alert_digest_is_high_priority(session):
    co = await _company(session, "ZeroCo")
    await _emit(session, co.id, "item:z", "item.created", {"sku": "Z", "name": "Z", "quantity": 0, "reorder_point": 5})
    await ProjectionEngine.rebuild(session)
    notif = await run_all_alerts(session, co.id)
    assert notif is not None and notif.priority == "high"


@pytest.mark.asyncio
async def test_below_reorder_but_in_stock_is_medium_priority(session):
    # Below the reorder point but still on hand (qty > 0): a heads-up, not out of
    # stock, so the digest is "medium" and raises no desktop push.
    co = await _company(session, "SoftCo")
    await _emit(session, co.id, "item:s", "item.created", {"sku": "S", "name": "S", "quantity": 2, "reorder_point": 5})
    await ProjectionEngine.rebuild(session)
    notif = await run_all_alerts(session, co.id)
    assert notif is not None and notif.priority == "medium"


@pytest.mark.asyncio
async def test_low_stock_disabled_setting_is_noop(session):
    co = await locked_company(session, (await _company(session, "OffCo")).id)
    co.settings = {"reorder_alerts_enabled": False}
    session.add(co)
    await session.flush()
    await _emit(session, co.id, "item:o", "item.created", {"sku": "O", "name": "O", "quantity": 0, "reorder_point": 5})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co.id) is None
    assert await _notif_count(session, co.id) == 0


# ── Proactive alerts: expiring, overdue, combined digest ──────────────────────

async def _emit_doc(session, cid, eid, event_type, data):
    await emit_event(
        session, company_id=cid, entity_id=eid, entity_type="doc",
        event_type=event_type, data=data, actor_id=None, location_id=None,
        source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


@pytest.mark.asyncio
async def test_expiring_alert_and_rearm(session):
    from datetime import date, timedelta
    from celerp.services.reorder import run_all_alerts
    co = await _company(session, "ExpCo")
    soon = (date.today() + timedelta(days=10)).isoformat()
    await _emit(session, co.id, "item:e", "item.created", {"sku": "E", "name": "Milk", "quantity": 5, "expires_at": soon})
    await ProjectionEngine.rebuild(session)

    n = await run_all_alerts(session, co.id)
    assert n is not None and "Expiring soon" in n.body and "Milk" in n.body
    assert await run_all_alerts(session, co.id) is None  # idempotent

    await _emit(session, co.id, "item:e", "item.quantity.adjusted", {"new_qty": 0})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co.id) is None
    assert co.settings.get("expiring_alerted_ids") == []  # re-armed

    await _emit(session, co.id, "item:e", "item.quantity.adjusted", {"new_qty": 5})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co.id) is not None


@pytest.mark.asyncio
async def test_overdue_invoice_alert(session):
    from celerp.services.reorder import run_all_alerts
    co = await _company(session, "OvdCo")
    await _emit_doc(session, co.id, "doc:i1", "doc.created", {
        "doc_type": "invoice", "ref_id": "INV-1", "total": 100.0,
        "due_date": "2020-01-01", "status": "awaiting_payment"})
    await ProjectionEngine.rebuild(session)

    n = await run_all_alerts(session, co.id)
    assert n is not None and "Overdue invoices" in n.body and "INV-1" in n.body
    assert await run_all_alerts(session, co.id) is None  # idempotent


@pytest.mark.asyncio
async def test_combined_digest_is_one_notification(session):
    from datetime import date, timedelta
    from celerp.services.reorder import run_all_alerts
    co = await _company(session, "AllCo")
    await _emit(session, co.id, "item:lo", "item.created", {"sku": "L", "name": "Low", "quantity": 2, "reorder_point": 5})
    soon = (date.today() + timedelta(days=5)).isoformat()
    await _emit(session, co.id, "item:ex", "item.created", {"sku": "X", "name": "Exp", "quantity": 9, "expires_at": soon})
    await _emit_doc(session, co.id, "doc:i", "doc.created", {
        "doc_type": "invoice", "ref_id": "INV-9", "total": 50.0,
        "due_date": "2020-01-01", "status": "awaiting_payment"})
    await ProjectionEngine.rebuild(session)

    n = await run_all_alerts(session, co.id)
    assert n is not None
    assert await _notif_count(session, co.id) == 1  # ONE combined digest
    assert "Low stock" in n.body and "Expiring soon" in n.body and "Overdue invoices" in n.body


@pytest.mark.asyncio
async def test_one_failing_company_does_not_stop_the_daily_scan(committed_engine, monkeypatch):
    """The daily scan keeps going after a company whose scan fails: every other due company is still
    scanned and stamped that day."""
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import celerp.db
    from celerp.services import reorder

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    ids = [uuid.uuid4(), uuid.uuid4()]
    async with factory() as s:
        for cid in ids:
            s.add(Company(id=cid, name="ScanCo", slug=f"scan-{cid.hex[:8]}", settings={}))
        await s.commit()

    scanned: list = []

    async def _detect(session, company):
        scanned.append(company.id)
        if len(scanned) == 1:
            raise RuntimeError("scan failed")
        return []

    @asynccontextmanager
    async def _session_ctx():
        async with factory() as s:
            yield s

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(reorder, "_CHECKS", (reorder._AlertCheck(None, "test_alerted_ids", "Test", _detect, str),))
    monkeypatch.setattr(reorder, "_scan_due", lambda company, now: True)
    monkeypatch.setattr(celerp.db, "get_session_ctx", _session_ctx)
    monkeypatch.setattr(reorder.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await reorder.reorder_alert_loop()

    assert sorted(scanned) == sorted(ids)
    async with factory() as s:
        stamped = [(await s.get(Company, cid)).settings.get("reorder_last_scan_at") for cid in scanned]
    assert stamped[0] is None and stamped[1] is not None


async def _notices_after_tick(committed_engine, monkeypatch, now, settings: dict, loops: int = 1) -> int:
    """Run ``loops`` alert loops side by side for one tick at ``now`` over a company with one
    alerting item, and return how many notices the company got."""
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import celerp.db
    from celerp.services import reorder

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="TickCo", slug=f"tick-{cid.hex[:8]}", settings=settings))
        await s.commit()

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    async def _detect(session, company):
        return [{"entity_id": "item:low", "name": "Low"}]

    @asynccontextmanager
    async def _session_ctx():
        async with factory() as s:
            yield s

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(reorder, "_CHECKS", (reorder._AlertCheck(None, "test_alerted_ids", "Test", _detect, str),))
    monkeypatch.setattr(reorder, "datetime", _Clock)
    monkeypatch.setattr(celerp.db, "get_session_ctx", _session_ctx)
    monkeypatch.setattr(reorder.asyncio, "sleep", _stop)
    results = await asyncio.gather(*(reorder.reorder_alert_loop() for _ in range(loops)), return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    async with factory() as s:
        return await _notif_count(s, cid)


@pytest.mark.asyncio
async def test_a_scan_due_since_yesterday_fires_on_an_afternoon_tick(committed_engine, monkeypatch):
    """An app that is not running during the scan hour still gets its daily alerts: the last
    scan was yesterday and the first tick today is at 14:00 UTC."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    yesterday = {"reorder_last_scan_at": (now - timedelta(days=1)).isoformat()}
    assert await _notices_after_tick(committed_engine, monkeypatch, now, yesterday) == 1


@pytest.mark.asyncio
async def test_a_scan_earlier_today_holds_the_next_until_tomorrow(committed_engine, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    earlier = {"reorder_last_scan_at": (now - timedelta(hours=3)).isoformat()}
    assert await _notices_after_tick(committed_engine, monkeypatch, now, earlier) == 0


@pytest.mark.asyncio
async def test_two_loops_on_the_same_tick_send_one_notice(committed_engine, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    yesterday = {"reorder_last_scan_at": (now - timedelta(days=1)).isoformat()}
    assert await _notices_after_tick(committed_engine, monkeypatch, now, yesterday, loops=2) == 1
