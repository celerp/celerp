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

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.projections.engine import ProjectionEngine
from celerp.services.reorder import (
    is_below_reorder,
    is_reorder_alert,
    reorder_point_of,
    run_reorder_scan,
    suggest_reorder,
)


# ── Pure predicate ────────────────────────────────────────────────────────────

def test_scan_due_gating():
    """The daily loop runs at most once per company per day, at the configured UTC hour."""
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone
    from celerp.services.reorder import _scan_due, _SCAN_HOUR_UTC
    at_hour = datetime(2026, 7, 5, _SCAN_HOUR_UTC, 0, tzinfo=timezone.utc)
    off_hour = at_hour.replace(hour=(_SCAN_HOUR_UTC + 1) % 24)
    # Wrong hour -> never due.
    assert _scan_due(SimpleNamespace(settings={}), off_hour) is False
    # Right hour, never scanned -> due.
    assert _scan_due(SimpleNamespace(settings={}), at_hour) is True
    # Right hour but scanned <23h ago -> not due (debounce).
    recent = (at_hour - timedelta(hours=2)).isoformat()
    assert _scan_due(SimpleNamespace(settings={"reorder_last_scan_at": recent}), at_hour) is False
    # Right hour, scanned >23h ago -> due again.
    old = (at_hour - timedelta(hours=25)).isoformat()
    assert _scan_due(SimpleNamespace(settings={"reorder_last_scan_at": old}), at_hour) is True


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
    notif = await run_reorder_scan(session, co)
    assert notif is not None
    assert notif.category == "inventory"
    assert notif.action_url == "/inventory?filter=low_stock"
    assert notif.title.startswith("1 item")
    assert "Low" in notif.body and "High" not in notif.body
    assert await _notif_count(session, co.id) == 1

    # Second run, no change: no new notification (idempotent).
    assert await run_reorder_scan(session, co) is None
    assert await _notif_count(session, co.id) == 1

    # Rise back above the reorder point -> no alert, latch cleared (re-arm).
    await _emit(session, co.id, low, "item.quantity.adjusted", {"new_qty": 10})
    await ProjectionEngine.rebuild(session)
    assert await run_reorder_scan(session, co) is None
    assert await _notif_count(session, co.id) == 1
    assert co.settings.get("reorder_alerted_ids") == []

    # Drop below again -> alerts again (proves re-arm).
    await _emit(session, co.id, low, "item.quantity.adjusted", {"new_qty": 1})
    await ProjectionEngine.rebuild(session)
    notif2 = await run_reorder_scan(session, co)
    assert notif2 is not None
    assert await _notif_count(session, co.id) == 2


@pytest.mark.asyncio
async def test_alert_priority_high_at_or_below_zero(session):
    co = await _company(session, "ZeroCo")
    await _emit(session, co.id, "item:z", "item.created", {"sku": "Z", "name": "Z", "quantity": 0, "reorder_point": 5})
    await ProjectionEngine.rebuild(session)
    notif = await run_reorder_scan(session, co)
    assert notif is not None and notif.priority == "high"


@pytest.mark.asyncio
async def test_alert_disabled_setting_is_noop(session):
    co = await _company(session, "OffCo")
    co.settings = {"reorder_alerts_enabled": False}
    session.add(co)
    await session.flush()
    await _emit(session, co.id, "item:o", "item.created", {"sku": "O", "name": "O", "quantity": 0, "reorder_point": 5})
    await ProjectionEngine.rebuild(session)
    assert await run_reorder_scan(session, co) is None
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

    n = await run_all_alerts(session, co)
    assert n is not None and "Expiring soon" in n.body and "Milk" in n.body
    assert await run_all_alerts(session, co) is None  # idempotent

    await _emit(session, co.id, "item:e", "item.quantity.adjusted", {"new_qty": 0})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co) is None
    assert co.settings.get("expiring_alerted_ids") == []  # re-armed

    await _emit(session, co.id, "item:e", "item.quantity.adjusted", {"new_qty": 5})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co) is not None


@pytest.mark.asyncio
async def test_overdue_invoice_alert(session):
    from celerp.services.reorder import run_all_alerts
    co = await _company(session, "OvdCo")
    await _emit_doc(session, co.id, "doc:i1", "doc.created", {
        "doc_type": "invoice", "ref_id": "INV-1", "total": 100.0,
        "due_date": "2020-01-01", "status": "awaiting_payment"})
    await ProjectionEngine.rebuild(session)

    n = await run_all_alerts(session, co)
    assert n is not None and "Overdue invoices" in n.body and "INV-1" in n.body
    assert await run_all_alerts(session, co) is None  # idempotent


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

    n = await run_all_alerts(session, co)
    assert n is not None
    assert await _notif_count(session, co.id) == 1  # ONE combined digest
    assert "Low stock" in n.body and "Expiring soon" in n.body and "Overdue invoices" in n.body


@pytest.mark.asyncio
async def test_disabled_check_is_skipped(session):
    from datetime import date, timedelta
    from celerp.services.reorder import run_all_alerts
    co = await _company(session, "DisCo")
    co.settings = {"expiring_alerts_enabled": False}
    session.add(co)
    await session.flush()
    soon = (date.today() + timedelta(days=5)).isoformat()
    await _emit(session, co.id, "item:e", "item.created", {"sku": "E", "name": "Exp", "quantity": 5, "expires_at": soon})
    await ProjectionEngine.rebuild(session)
    assert await run_all_alerts(session, co) is None  # only expiring would fire; it's off
