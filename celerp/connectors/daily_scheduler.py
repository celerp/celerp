# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Daily sync scheduler for accounting connectors.

Lightweight scheduler that checks on startup and periodically whether
any accounting connector is due for a daily sync. Runs entirely on the
desktop - no relay involvement.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import sqlalchemy as sa

from celerp.connectors.base import ConnectorCategory, SyncDirection, SyncFrequency
from celerp.models.connector_config import ConnectorConfig

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600  # check every hour
_MIN_HOURS_BETWEEN_SYNCS = 23

TokenFetcher = Callable[[str, str], Awaitable["ConnectorContext"]]  # noqa: F821


async def check_and_run_daily_syncs(
    company_id: str,
    token_fetcher: TokenFetcher | None = None,
) -> list[str]:
    """Check all connectors with daily frequency and run if due.

    token_fetcher: async (company_id, connector_name) -> ConnectorContext
      If not provided, due connectors are logged as warnings but NOT marked synced.

    Returns list of connector names that were successfully synced.
    """
    from celerp.db import get_session_ctx
    import celerp.connectors as connector_registry
    from celerp.connectors.sync_runner import run_sync

    now = datetime.now(timezone.utc)
    synced: list[str] = []

    async with get_session_ctx() as session:
        # Reconcile EVERY enabled connector once a day — including realtime
        # (webhook) ones — so a daily incremental pass backstops any webhooks
        # missed while the instance/tunnel was offline (idempotency keys make
        # webhook + reconcile converge to a single write).
        rows = await session.execute(
            sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == company_id,
            )
        )
        configs = [row[0] for row in rows]

    for config in configs:
        # Check if enough time has passed since last daily sync
        if config.last_daily_sync_at:
            elapsed = now - config.last_daily_sync_at.replace(tzinfo=timezone.utc)
            if elapsed < timedelta(hours=_MIN_HOURS_BETWEEN_SYNCS):
                continue

        # Check if current UTC hour matches configured hour
        if now.hour != config.daily_sync_hour:
            continue

        try:
            connector = connector_registry.get(config.connector)
        except KeyError:
            log.warning("daily_scheduler: unknown connector %s", config.connector)
            continue

        if token_fetcher is None:
            log.warning(
                "daily_scheduler: %s is due for sync but no token_fetcher provided - skipping",
                config.connector,
            )
            continue

        direction = SyncDirection(config.direction)
        log.info("daily_scheduler: running %s (direction=%s)", config.connector, direction.value)

        try:
            ctx = await token_fetcher(company_id, config.connector)
        except Exception as exc:
            log.warning("daily_scheduler: token fetch failed for %s: %s", config.connector, exc)
            continue

        # Run sync for all supported entities respecting direction
        for entity_enum in connector.supported_entities:
            try:
                await run_sync(connector, ctx, entity_enum.value, direction=direction)
            except Exception as exc:
                log.error("daily_scheduler: sync error %s/%s: %s", config.connector, entity_enum.value, exc)

        synced.append(config.connector)

        # Update last_daily_sync_at only after actually running
        async with get_session_ctx() as session:
            await session.execute(
                sa.update(ConnectorConfig)
                .where(ConnectorConfig.id == config.id)
                .values(last_daily_sync_at=now)
            )
            await session.commit()

    return synced


async def scheduler_loop(company_id: str, token_fetcher: TokenFetcher | None = None) -> None:
    """Background loop that checks for due daily syncs every hour."""
    while True:
        try:
            synced = await check_and_run_daily_syncs(company_id, token_fetcher=token_fetcher)
            if synced:
                log.info("daily_scheduler: synced %s", ", ".join(synced))
        except Exception as exc:
            log.error("daily_scheduler: error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


async def _distinct_company_ids() -> list[str]:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        rows = await session.execute(sa.select(ConnectorConfig.company_id).distinct())
        return [r[0] for r in rows]


async def scheduler_loop_all(token_fetcher: TokenFetcher | None = None) -> None:
    """Reconciliation backstop: hourly, run any due daily syncs for every company
    that has a connector configured. Started from the API lifespan."""
    while True:
        try:
            for company_id in await _distinct_company_ids():
                synced = await check_and_run_daily_syncs(company_id, token_fetcher=token_fetcher)
                if synced:
                    log.info("daily_scheduler: synced %s for %s", ", ".join(synced), company_id)
        except Exception as exc:
            log.error("daily_scheduler: loop error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
