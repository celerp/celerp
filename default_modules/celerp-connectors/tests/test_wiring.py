# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for the autonomous-sync wiring: relay token fetch, the gateway webhook
dispatch, and the reconciliation scheduler covering all connectors."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx


# ── relay_token.fetch_context ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_context_none_without_session():
    from celerp.connectors.relay_token import fetch_context
    with patch("celerp.gateway.state.get_session_token", return_value=None):
        assert await fetch_context("co", "shopify") is None


@pytest.mark.asyncio
async def test_fetch_context_builds_ctx_from_relay():
    from celerp.connectors.relay_token import fetch_context
    with patch("celerp.gateway.state.get_session_token", return_value="tok"), \
         patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers", return_value={}), \
         patch("celerp.connectors.ownership.connector_owned_by_company",
               new=AsyncMock(return_value=True)), \
         respx.mock:
        respx.get("https://relay.test/tokens/shopify/access-token").mock(
            return_value=httpx.Response(200, json={"access_token": "shpat_x", "store_handle": "s.myshopify.com"}))
        ctx = await fetch_context("co-1", "shopify")
    assert ctx is not None
    assert ctx.company_id == "co-1"
    assert ctx.access_token == "shpat_x"
    assert ctx.store_handle == "s.myshopify.com"


@pytest.mark.asyncio
async def test_fetch_context_xero_reads_connected_organisation():
    from celerp.connectors.relay_token import fetch_context
    with patch("celerp.gateway.state.get_session_token", return_value="tok"), \
         patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers", return_value={}), \
         patch("celerp.connectors.ownership.connector_owned_by_company",
               new=AsyncMock(return_value=True)), \
         respx.mock:
        respx.get("https://relay.test/tokens/xero/context").mock(
            return_value=httpx.Response(200, json={"platform": "xero", "store_handle": "tenant-1"}))
        ctx = await fetch_context("co-1", "xero")
    assert ctx is not None
    assert ctx.access_token == ""
    assert ctx.store_handle == "tenant-1"


@pytest.mark.asyncio
async def test_fetch_context_none_on_relay_error():
    from celerp.connectors.relay_token import fetch_context
    with patch("celerp.gateway.state.get_session_token", return_value="tok"), \
         patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers", return_value={}), \
         patch("celerp.connectors.ownership.connector_owned_by_company",
               new=AsyncMock(return_value=True)), \
         respx.mock:
        respx.get("https://relay.test/tokens/shopify/access-token").mock(return_value=httpx.Response(404))
        assert await fetch_context("co-1", "shopify") is None


@pytest.mark.asyncio
async def test_fetch_context_raises_when_relay_requires_update():
    from celerp.connectors.relay_token import ConnectorUpgradeRequired, fetch_context
    with patch("celerp.gateway.state.get_session_token", return_value="tok"), \
         patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers", return_value={}), \
         patch("celerp.connectors.ownership.connector_owned_by_company",
               new=AsyncMock(return_value=True)), \
         respx.mock:
        respx.get("https://relay.test/tokens/xero/context").mock(return_value=httpx.Response(426))
        with pytest.raises(ConnectorUpgradeRequired, match="Update Celerp to continue syncing Xero."):
            await fetch_context("co-1", "xero")


# ── gateway webhook dispatch ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_shopify_webhook_triggers_handle_webhook():
    from celerp.gateway.client import GatewayClient
    gw = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://x")

    sess = MagicMock()
    res = MagicMock()
    res.all = MagicMock(return_value=[("co-1",)])
    sess.execute = AsyncMock(return_value=res)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)

    hw = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.relay_token.fetch_context", new=AsyncMock(return_value=MagicMock())), \
         patch("celerp.connectors.webhooks.handle_webhook", new=hw):
        await gw._handle_shopify_webhook({"topic": "orders/create", "data": {"id": 1}})

    hw.assert_awaited_once()
    event = hw.await_args[0][0]
    assert event.platform == "shopify"
    assert event.topic == "orders/create"


# ── scheduler reconciles all connectors (not just DAILY) ─────────────────────

@pytest.mark.asyncio
async def test_scheduler_reconciles_realtime_config():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs

    config = MagicMock()
    config.connector = "shopify"
    config.id = 7
    config.last_daily_sync_at = None                       # never synced → due
    config.daily_sync_hour = datetime.now(timezone.utc).hour  # this hour → due

    sess = MagicMock()
    sess.execute = AsyncMock(return_value=[(config,)])     # select returns the config row
    sess.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)

    from celerp.connectors.base import SyncEntity, SyncResult
    run_sync_mock = AsyncMock(
        return_value=[SyncResult(entity=SyncEntity.PRODUCTS, created=1)]
    )
    lock = AsyncMock(return_value=config)
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync", new=run_sync_mock), \
         patch("celerp.connectors.ownership.lock_connector_operation", new=lock), \
         patch("celerp.connectors.ownership.lock_connection_row", new=AsyncMock(return_value=config)):
        synced = await check_and_run_daily_syncs("co-1")

    assert "shopify" in synced                # realtime connector was reconciled
    run_sync_mock.assert_awaited_once()
    connector, _ctx = run_sync_mock.await_args.args
    assert connector.name == "shopify"
    lock.assert_awaited_once_with(
        sess, "co-1", "shopify", require_owner=True
    )
    assert config.last_daily_sync_at is not None


@pytest.mark.asyncio
async def test_scheduler_does_not_stamp_reconnected_generation():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    from celerp.connectors.base import SyncEntity, SyncResult

    config = _sched_config()
    cm = _sched_session(config)
    replacement = MagicMock(id=8)
    replacement.last_daily_sync_at = None
    run_sync_mock = AsyncMock(
        return_value=[SyncResult(entity=SyncEntity.PRODUCTS, created=1)]
    )
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync", new=run_sync_mock), \
         patch(
             "celerp.connectors.ownership.lock_connector_operation",
             new=AsyncMock(return_value=replacement),
         ), \
         patch("celerp.connectors.ownership.lock_connection_row", new=AsyncMock(return_value=replacement)):
        synced = await check_and_run_daily_syncs("co-1")

    assert synced == []
    assert replacement.last_daily_sync_at is None


def _sched_config(**over):
    c = MagicMock()
    c.connector = over.get("connector", "shopify")
    c.id = 7
    c.last_daily_sync_at = over.get("last_daily_sync_at", None)
    c.daily_sync_hour = over.get("daily_sync_hour", datetime.now(timezone.utc).hour)
    return c


def _sched_session(config):
    sess = MagicMock()
    sess.execute = AsyncMock(return_value=[(config,)])
    sess.commit = AsyncMock()
    sess.rollback = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_scheduler_skips_when_latest_occurrence_already_ran():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs, latest_scheduled_run
    hour = (datetime.now(timezone.utc).hour - 3) % 24
    ran = latest_scheduled_run(datetime.now(timezone.utc), hour) + timedelta(minutes=5)
    cm = _sched_session(_sched_config(daily_sync_hour=hour, last_daily_sync_at=ran))
    run = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync", new=run):
        synced = await check_and_run_daily_syncs("co")
    assert synced == [] and run.await_count == 0


@pytest.mark.asyncio
async def test_scheduler_catches_up_a_missed_daily_hour():
    """An instance that was off at its configured hour runs at the next check."""
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs, latest_scheduled_run
    hour = (datetime.now(timezone.utc).hour - 3) % 24
    before = latest_scheduled_run(datetime.now(timezone.utc), hour) - timedelta(hours=2)
    config = _sched_config(daily_sync_hour=hour, last_daily_sync_at=before)
    cm = _sched_session(config)
    result = MagicMock(created=1, updated=0, errors=[])
    run = AsyncMock(return_value=[result])
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync", new=run), \
         patch("celerp.connectors.ownership.lock_connector_operation", new=AsyncMock(return_value=config)), \
         patch("celerp.connectors.ownership.lock_connection_row", new=AsyncMock(return_value=config)):
        synced = await check_and_run_daily_syncs("co")
    assert run.await_count == 1
    assert synced == ["shopify"]


def test_daily_sync_due_follows_the_latest_occurrence():
    from celerp.connectors.daily_scheduler import daily_sync_due
    now = datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc)
    # Scheduled 02:00 today; last ran yesterday 02:05 -> today's run is outstanding.
    assert daily_sync_due(datetime(2026, 9, 24, 2, 5), 2, now)
    # Already ran after today's 02:00.
    assert not daily_sync_due(datetime(2026, 9, 25, 2, 5), 2, now)
    # Scheduled 23:00: the latest occurrence is yesterday 23:00.
    assert not daily_sync_due(datetime(2026, 9, 24, 23, 10), 23, now)
    assert daily_sync_due(datetime(2026, 9, 24, 22, 50), 23, now)
    assert daily_sync_due(None, 23, now)


@pytest.mark.asyncio
async def test_scheduler_skips_unknown_connector():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    cm = _sched_session(_sched_config(connector="bogus"))
    with patch("celerp.db.get_session_ctx", return_value=cm):
        synced = await check_and_run_daily_syncs("co")
    assert synced == []


@pytest.mark.asyncio
async def test_scheduler_skips_on_a_failed_sync():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    config = _sched_config()
    cm = _sched_session(config)
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync",
               new=AsyncMock(side_effect=RuntimeError("relay down"))):
        synced = await check_and_run_daily_syncs("co")
    assert synced == [] and config.last_daily_sync_at is None


@pytest.mark.asyncio
async def test_scheduler_stays_due_when_relay_requires_update():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    from celerp.connectors.relay_token import ConnectorUpgradeRequired
    config = _sched_config()
    cm = _sched_session(config)
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync",
               new=AsyncMock(side_effect=ConnectorUpgradeRequired("Update Celerp"))):
        synced = await check_and_run_daily_syncs("co")
    assert synced == []
    assert config.last_daily_sync_at is None           # still due next check


@pytest.mark.asyncio
async def test_distinct_company_ids():
    from celerp.connectors.daily_scheduler import _distinct_company_ids
    sess = MagicMock()
    sess.execute = AsyncMock(return_value=[("co-1",), ("legacy-iid",), ("co-2",)])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.config.ensure_instance_id", return_value="legacy-iid"):
        assert await _distinct_company_ids() == ["co-1", "co-2"]


@pytest.mark.asyncio
async def test_scheduler_loop_all_runs_each_company_then_exits():
    """One pass reconciles every configured company; the loop exits when the
    inter-cycle sleep is interrupted."""
    import asyncio
    from celerp.connectors import daily_scheduler

    check = AsyncMock(return_value=["shopify"])
    with patch.object(daily_scheduler, "_distinct_company_ids",
                      new=AsyncMock(return_value=["co-1", "co-2"])), \
         patch.object(daily_scheduler, "check_and_run_daily_syncs", new=check), \
         patch("celerp.connectors.daily_scheduler.asyncio.sleep",
               new=AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await daily_scheduler.scheduler_loop_all()
    assert check.await_count == 2                       # one run per company


@pytest.mark.asyncio
async def test_scheduler_loop_single_company_then_exits():
    import asyncio
    from celerp.connectors import daily_scheduler

    check = AsyncMock(return_value=[])
    with patch.object(daily_scheduler, "check_and_run_daily_syncs", new=check), \
         patch("celerp.connectors.daily_scheduler.asyncio.sleep",
               new=AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await daily_scheduler.scheduler_loop("co-1")
    assert check.await_count == 1


@pytest.mark.parametrize("connector,expected", [
    ("shopify", {"products_out", "inventory_out"}),
    ("woocommerce", {"products_out", "inventory_out"}),
    ("quickbooks", {"invoices_out"}),
    ("xero", {"invoices_out"}),
])
def test_supported_outbound_entities_are_detected(connector, expected):
    """Every outbound push a connector implements is carried by one of its records, so
    the scheduler dispatches it. Regression: Shopify's inventory push was named
    `sync_inventory` (not `sync_inventory_out`), so it was classified as the inbound
    'inventory' entity and never ran."""
    import celerp.connectors as registry
    from celerp.connectors.base import OUTBOUND_ENTITIES
    from celerp.connectors.sync_runner import implements

    c = registry.get(connector)
    declared = {e for flow in c.entity_flows.values() for e in flow.outbound}
    assert declared == expected
    assert {e for e in OUTBOUND_ENTITIES if implements(c, e)} == expected


@pytest.mark.parametrize("products,expected_inbound,expected_outbound", [
    ("inbound", True, False),
    ("outbound", False, True),
    ("both", True, True),
    ("off", False, False),
])
def test_sync_plan_honours_direction(products, expected_inbound, expected_outbound):
    import celerp.connectors as registry
    from celerp.connectors.sync_runner import sync_plan
    plan = sync_plan(registry.get("woocommerce"),
                     {"products": products, "stock": "off", "orders": "off", "customers": "off"})
    assert ("products" in plan) is expected_inbound
    assert ("products_out" in plan) is expected_outbound


@pytest.mark.asyncio
async def test_scheduler_runs_the_reconciliation_pass():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    from celerp.connectors.base import SyncEntity, SyncResult

    config = _sched_config(connector="woocommerce")
    cm = _sched_session(config)
    run = AsyncMock(return_value=[SyncResult(entity=SyncEntity.PRODUCTS, created=0)])
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_connector_sync", new=run), \
         patch(
             "celerp.connectors.ownership.lock_connector_operation",
             new=AsyncMock(return_value=config),
         ), \
         patch("celerp.connectors.ownership.lock_connection_row", new=AsyncMock(return_value=config)):
        await check_and_run_daily_syncs("co")

    assert run.await_args.kwargs["reconcile"] is True
