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

# Outbound entity -> connector method. A connector "supports" an outbound entity when it
# overrides the base (which raises NotImplementedError); we only dispatch those, so the
# scheduler never emits a failed run for a push a connector doesn't implement.
_OUTBOUND_ENTITY_METHODS = {
    "products_out": "sync_products_out",
    "invoices_out": "sync_invoices_out",
    "inventory_out": "sync_inventory_out",
}


def _supported_outbound(connector) -> list[str]:
    from celerp.connectors.base import ConnectorBase
    return [
        entity for entity, method in _OUTBOUND_ENTITY_METHODS.items()
        if getattr(type(connector), method, None) is not getattr(ConnectorBase, method, None)
    ]

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

        log.info("daily_scheduler: running %s", config.connector)

        try:
            ctx = await token_fetcher(company_id, config.connector)
        except Exception as exc:
            log.warning("daily_scheduler: token fetch failed for %s: %s", config.connector, exc)
            continue

        # Sync every supported entity, honouring the configured direction: inbound
        # entities pull from the platform; outbound (*_out) entities push back the ones
        # the connector actually implements. run_sync enforces the direction gate too.
        direction = SyncDirection(config.direction)
        entities = [e.value for e in connector.supported_entities]
        if direction in (SyncDirection.BOTH, SyncDirection.OUTBOUND):
            entities += _supported_outbound(connector)
        entity_results = []
        for entity in entities:
            try:
                entity_results.append(await run_sync(connector, ctx, entity, direction=direction))
            except Exception as exc:
                log.error("daily_scheduler: sync error %s/%s: %s", config.connector, entity, exc)

        # Only mark the connector synced (advancing the daily clock) if at least one
        # entity made progress. On a total failure (e.g. a transient outage) we leave
        # last_daily_sync_at unset, so the connector stays "due" and retries at the next
        # tick that lands on its configured hour (the next day, or sooner on app restart).
        if not any((r.created or r.updated or not r.errors) for r in entity_results):
            log.warning("daily_scheduler: all entities failed for %s — will retry when next due", config.connector)
            continue

        synced.append(config.connector)
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
