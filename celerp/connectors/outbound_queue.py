# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Outbound queue processor - handles failed/queued outbound pushes with retry.

When a user saves a synced entity in Celerp, the save handler queues an
outbound push. This module processes the queue with exponential backoff.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import sqlalchemy as sa

from celerp.models.connector_config import OutboundQueue

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BACKOFF_MINUTES = [1, 5, 15, 60, 240]


async def enqueue(
    company_id: str,
    connector: str,
    entity_type: str,
    entity_id: str,
    payload: dict | None = None,
) -> None:
    """Add an entity to the outbound push queue."""
    from celerp.db import get_session_ctx

    entry = OutboundQueue(
        company_id=company_id,
        connector=connector,
        entity_type=entity_type,
        entity_id=entity_id,
        payload_json=json.dumps(payload) if payload else None,
        status="pending",
        next_retry_at=datetime.now(timezone.utc),
    )
    async with get_session_ctx() as session:
        session.add(entry)
        await session.commit()


TokenFetcher = Callable[[str, str], Awaitable["ConnectorContext"]]  # noqa: F821


async def process_queue(
    company_id: str,
    token_fetcher: TokenFetcher | None = None,
) -> int:
    """Process all pending/retryable outbound queue entries. Returns count processed.

    token_fetcher: async (company_id, connector_name) -> ConnectorContext
      If not provided, entries are marked "awaiting_token" and skipped.
    """
    from celerp.db import get_session_ctx
    import celerp.connectors as connector_registry
    from celerp.connectors.sync_runner import run_sync

    now = datetime.now(timezone.utc)
    processed = 0

    async with get_session_ctx() as session:
        rows = await session.execute(
            sa.select(OutboundQueue).where(
                OutboundQueue.company_id == company_id,
                OutboundQueue.status.in_(["pending", "retrying"]),
                sa.or_(
                    OutboundQueue.next_retry_at.is_(None),
                    OutboundQueue.next_retry_at <= now,
                ),
            ).order_by(OutboundQueue.created_at)
        )
        entries = [row[0] for row in rows]

    for entry in entries:
        if token_fetcher is None:
            await _update_status(entry.id, "awaiting_token",
                                 "No token_fetcher provided - cannot process without credentials")
            log.warning(
                "outbound_queue: entry %d skipped - no token_fetcher for %s/%s",
                entry.id, entry.connector, entry.entity_type,
            )
            continue

        try:
            ctx = await token_fetcher(company_id, entry.connector)
        except Exception as exc:
            await _update_status(entry.id, "awaiting_token", f"Token fetch failed: {exc}")
            log.warning(
                "outbound_queue: entry %d token fetch failed for %s: %s",
                entry.id, entry.connector, exc,
            )
            continue

        try:
            connector = connector_registry.get(entry.connector)
            out_entity = f"{entry.entity_type}_out"

            if entry.payload_json:
                # Push using stored payload via the specific push method
                payload = json.loads(entry.payload_json)
                push_method_name = f"sync_{entry.entity_type}_out"
                push_method = getattr(connector, push_method_name, None)
                if push_method is not None:
                    result = await push_method(ctx, payload=payload)
                else:
                    log.warning(
                        "outbound_queue: connector %s has no %s method, falling back to full sync",
                        entry.connector, push_method_name,
                    )
                    result = await run_sync(connector, ctx, out_entity)
            else:
                log.warning(
                    "outbound_queue: entry %d has no payload_json, running full sync for %s/%s",
                    entry.id, entry.connector, entry.entity_type,
                )
                result = await run_sync(connector, ctx, out_entity)

            if result.ok:
                await _update_status(entry.id, "completed")
                processed += 1
            else:
                await _handle_failure(entry, result.errors)
        except Exception as exc:
            await _handle_failure(entry, [str(exc)])

    return processed


async def _update_status(entry_id: int, status: str, error: str | None = None) -> None:
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        await session.execute(
            sa.update(OutboundQueue)
            .where(OutboundQueue.id == entry_id)
            .values(status=status, error_message=error)
        )
        await session.commit()


async def _handle_failure(entry: OutboundQueue, errors: list[str] | None) -> None:
    from celerp.db import get_session_ctx

    error_msg = "; ".join(errors) if errors else "Unknown error"
    new_retry = entry.retry_count + 1

    if new_retry >= MAX_RETRIES:
        await _update_status(entry.id, "failed", error_msg)
        log.warning("outbound_queue: entry %d failed permanently after %d retries", entry.id, new_retry)
        return

    backoff_min = BACKOFF_MINUTES[min(new_retry, len(BACKOFF_MINUTES) - 1)]
    next_retry = datetime.now(timezone.utc) + timedelta(minutes=backoff_min)

    async with get_session_ctx() as session:
        await session.execute(
            sa.update(OutboundQueue)
            .where(OutboundQueue.id == entry.id)
            .values(
                status="retrying",
                retry_count=new_retry,
                next_retry_at=next_retry,
                error_message=error_msg,
            )
        )
        await session.commit()

    log.info("outbound_queue: entry %d retry %d/%d in %dm",
             entry.id, new_retry, MAX_RETRIES, backoff_min)


async def get_pending_count(company_id: str, connector: str | None = None) -> int:
    """Get count of pending/retrying entries for UI display."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        q = sa.select(sa.func.count(OutboundQueue.id)).where(
            OutboundQueue.company_id == company_id,
            OutboundQueue.status.in_(["pending", "retrying"]),
        )
        if connector:
            q = q.where(OutboundQueue.connector == connector)
        result = await session.execute(q)
        return result.scalar() or 0


async def get_failed_count(company_id: str, connector: str | None = None) -> int:
    """Get count of permanently failed entries for UI display."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        q = sa.select(sa.func.count(OutboundQueue.id)).where(
            OutboundQueue.company_id == company_id,
            OutboundQueue.status == "failed",
        )
        if connector:
            q = q.where(OutboundQueue.connector == connector)
        result = await session.execute(q)
        return result.scalar() or 0
