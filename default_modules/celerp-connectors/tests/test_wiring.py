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
         respx.mock:
        respx.get("https://relay.test/tokens/shopify/access-token").mock(
            return_value=httpx.Response(200, json={"access_token": "shpat_x", "store_handle": "s.myshopify.com"}))
        ctx = await fetch_context("co-1", "shopify")
    assert ctx is not None
    assert ctx.company_id == "co-1"
    assert ctx.access_token == "shpat_x"
    assert ctx.store_handle == "s.myshopify.com"


@pytest.mark.asyncio
async def test_fetch_context_none_on_relay_error():
    from celerp.connectors.relay_token import fetch_context
    with patch("celerp.gateway.state.get_session_token", return_value="tok"), \
         patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
         patch("celerp.gateway.state.relay_session_headers", return_value={}), \
         respx.mock:
        respx.get("https://relay.test/tokens/shopify/access-token").mock(return_value=httpx.Response(404))
        assert await fetch_context("co-1", "shopify") is None


# ── gateway webhook dispatch ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_shopify_webhook_triggers_handle_webhook():
    from celerp.gateway.client import GatewayClient
    gw = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://x")

    sess = MagicMock()
    res = MagicMock()
    res.all = MagicMock(return_value=[("co-1", "both")])
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


@pytest.mark.asyncio
async def test_handle_shopify_webhook_skips_when_no_token():
    from celerp.gateway.client import GatewayClient
    gw = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://x")

    sess = MagicMock()
    res = MagicMock()
    res.all = MagicMock(return_value=[("co-1", "both")])
    sess.execute = AsyncMock(return_value=res)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)

    hw = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.relay_token.fetch_context", new=AsyncMock(return_value=None)), \
         patch("celerp.connectors.webhooks.handle_webhook", new=hw):
        await gw._handle_shopify_webhook({"topic": "orders/create", "data": {}})

    hw.assert_not_awaited()  # no relay token → skip (reconcile backstops)


# ── scheduler reconciles all connectors (not just DAILY) ─────────────────────

@pytest.mark.asyncio
async def test_scheduler_reconciles_realtime_config():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs

    config = MagicMock()
    config.connector = "shopify"
    config.direction = "both"
    config.id = 7
    config.last_daily_sync_at = None                       # never synced → due
    config.daily_sync_hour = datetime.now(timezone.utc).hour  # this hour → due

    sess = MagicMock()
    sess.execute = AsyncMock(return_value=[(config,)])     # select returns the config row
    sess.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)

    run_sync_mock = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_sync", new=run_sync_mock):
        synced = await check_and_run_daily_syncs("co-1", token_fetcher=AsyncMock(return_value=MagicMock()))

    assert "shopify" in synced                # realtime connector was reconciled
    assert run_sync_mock.await_count >= 1


def _sched_config(**over):
    c = MagicMock()
    c.connector = over.get("connector", "shopify")
    c.direction = "both"
    c.id = 7
    c.last_daily_sync_at = over.get("last_daily_sync_at", None)
    c.daily_sync_hour = over.get("daily_sync_hour", datetime.now(timezone.utc).hour)
    return c


def _sched_session(config):
    sess = MagicMock()
    sess.execute = AsyncMock(return_value=[(config,)])
    sess.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_scheduler_skips_recently_synced():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    recent = datetime.now(timezone.utc) - timedelta(hours=1)   # < 23h ago → not due
    cm = _sched_session(_sched_config(last_daily_sync_at=recent))
    run = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_sync", new=run):
        synced = await check_and_run_daily_syncs("co", token_fetcher=AsyncMock())
    assert synced == [] and run.await_count == 0


@pytest.mark.asyncio
async def test_scheduler_skips_wrong_hour():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    off_hour = (datetime.now(timezone.utc).hour + 1) % 24
    cm = _sched_session(_sched_config(daily_sync_hour=off_hour))
    run = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_sync", new=run):
        synced = await check_and_run_daily_syncs("co", token_fetcher=AsyncMock())
    assert synced == [] and run.await_count == 0


@pytest.mark.asyncio
async def test_scheduler_skips_unknown_connector():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    cm = _sched_session(_sched_config(connector="bogus"))
    with patch("celerp.db.get_session_ctx", return_value=cm):
        synced = await check_and_run_daily_syncs("co", token_fetcher=AsyncMock())
    assert synced == []


@pytest.mark.asyncio
async def test_scheduler_due_but_no_token_fetcher_skips():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    cm = _sched_session(_sched_config())               # due
    with patch("celerp.db.get_session_ctx", return_value=cm):
        synced = await check_and_run_daily_syncs("co", token_fetcher=None)
    assert synced == []                                # logged, not marked synced


@pytest.mark.asyncio
async def test_scheduler_skips_on_token_fetch_error():
    from celerp.connectors.daily_scheduler import check_and_run_daily_syncs
    cm = _sched_session(_sched_config())
    run = AsyncMock()
    with patch("celerp.db.get_session_ctx", return_value=cm), \
         patch("celerp.connectors.sync_runner.run_sync", new=run):
        synced = await check_and_run_daily_syncs(
            "co", token_fetcher=AsyncMock(side_effect=RuntimeError("relay down")))
    assert synced == [] and run.await_count == 0       # token fetch failed → no sync


@pytest.mark.asyncio
async def test_distinct_company_ids():
    from celerp.connectors.daily_scheduler import _distinct_company_ids
    sess = MagicMock()
    sess.execute = AsyncMock(return_value=[("co-1",), ("co-2",)])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("celerp.db.get_session_ctx", return_value=cm):
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
            await daily_scheduler.scheduler_loop_all(token_fetcher=AsyncMock())
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
            await daily_scheduler.scheduler_loop("co-1", token_fetcher=AsyncMock())
    assert check.await_count == 1
