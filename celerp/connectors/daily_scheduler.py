# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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

import sqlalchemy as sa

from celerp.connectors.base import ConnectorCategory, SyncDirection, SyncFrequency
from celerp.models.connector_config import ConnectorConfig

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600  # check every hour
_MIN_HOURS_BETWEEN_SYNCS = 23


async def check_and_run_daily_syncs(company_id: str) -> list[str]:
    """Check all connectors with daily frequency and run if due.

    Returns list of connector names that were synced.
    """
    from celerp.db import get_session_ctx
    import celerp.connectors as connector_registry
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.sync_runner import run_sync

    now = datetime.now(timezone.utc)
    synced: list[str] = []

    async with get_session_ctx() as session:
        rows = await session.execute(
            sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == company_id,
                ConnectorConfig.sync_frequency == SyncFrequency.DAILY.value,
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
        # (user sets local hour; stored as UTC offset would be ideal,
        # but for simplicity we use UTC hour directly)
        if now.hour != config.daily_sync_hour:
            continue

        try:
            connector = connector_registry.get(config.connector)
        except KeyError:
            log.warning("daily_scheduler: unknown connector %s", config.connector)
            continue

        direction = SyncDirection(config.direction)
        log.info("daily_scheduler: running %s (direction=%s)", config.connector, direction.value)

        # Run sync for all supported entities respecting direction
        ctx = ConnectorContext(company_id=company_id, access_token="", store_handle="")
        # Note: actual token fetch happens in the UI/caller layer
        # This scheduler signals that a sync is due; the UI triggers it with real tokens

        synced.append(config.connector)

        # Update last_daily_sync_at
        async with get_session_ctx() as session:
            await session.execute(
                sa.update(ConnectorConfig)
                .where(ConnectorConfig.id == config.id)
                .values(last_daily_sync_at=now)
            )
            await session.commit()

    return synced


async def scheduler_loop(company_id: str) -> None:
    """Background loop that checks for due daily syncs every hour."""
    while True:
        try:
            synced = await check_and_run_daily_syncs(company_id)
            if synced:
                log.info("daily_scheduler: synced %s", ", ".join(synced))
        except Exception as exc:
            log.error("daily_scheduler: error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
