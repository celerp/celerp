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

from celerp.connectors.base import SyncDirection
from celerp.models.connector_config import ConnectorConfig

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600  # check every hour

TokenFetcher = Callable[[str, str], Awaitable["ConnectorContext"]]  # noqa: F821


def latest_scheduled_run(now: datetime, hour: int) -> datetime:
    """The most recent daily_sync_hour occurrence at or before now (UTC)."""
    today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return today if today <= now else today - timedelta(days=1)


def daily_sync_due(last_daily_sync_at: datetime | None, hour: int, now: datetime) -> bool:
    """A connector is due when its latest scheduled occurrence has not run yet.

    The configured hour is when a run is scheduled, not the only hour it may run:
    an instance that was off or asleep at that hour catches up at the next check.
    """
    if last_daily_sync_at is None:
        return True
    return last_daily_sync_at.replace(tzinfo=timezone.utc) < latest_scheduled_run(now, hour)


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
    from celerp.connectors.sync_runner import run_connector_sync

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
        if not daily_sync_due(config.last_daily_sync_at, config.daily_sync_hour, now):
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

        log.info("daily_scheduler: running %s", config.connector)

        try:
            ctx = await token_fetcher(company_id, config.connector)
        except Exception as exc:
            log.warning("daily_scheduler: token fetch failed for %s: %s", config.connector, exc)
            continue

        direction = SyncDirection(config.direction)
        try:
            entity_results = await run_connector_sync(
                connector,
                ctx,
                direction=direction,
                reconcile=True,
            )
        except Exception as exc:
            log.error("daily_scheduler: sync error %s: %s", config.connector, exc)
            entity_results = []

        # Only mark the connector synced (advancing the daily clock) if at least one
        # entity made progress. On a total failure (e.g. a transient outage) we leave
        # last_daily_sync_at where it was, so the connector stays due and retries at the
        # next hourly check.
        if not any((r.created or r.updated or not r.errors) for r in entity_results):
            log.warning("daily_scheduler: all entities failed for %s — will retry when next due", config.connector)
            continue

        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )

        async with get_session_ctx() as session:
            try:
                current = await lock_connector_operation(
                    session,
                    company_id,
                    config.connector,
                    require_owner=True,
                )
            except ConnectorOwnershipError:
                await session.rollback()
                continue
            if current.id != config.id:
                await session.rollback()
                continue
            current.last_daily_sync_at = now
            await session.commit()
        synced.append(config.connector)

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
    from celerp.config import ensure_instance_id
    from celerp.db import get_session_ctx

    legacy_id = ensure_instance_id()
    async with get_session_ctx() as session:
        rows = await session.execute(sa.select(ConnectorConfig.company_id).distinct())
        return [str(r[0]) for r in rows if str(r[0]) != legacy_id]


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
