# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Webhook handler - processes inbound webhook events from e-commerce platforms.

This module is called by the relay when it receives and validates a webhook.
The relay pushes a lightweight notification via SSE; the desktop fetches
the changed entity from the platform API.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from celerp.connectors.base import ConnectorContext, SyncDirection, entity_allowed
from celerp.connectors.sync_runner import run_sync
from celerp.capacity import MODULE_SETTLE_SECONDS
from celerp.modules.boundary import ModuleBusy, ModuleRefused
import celerp.connectors as connector_registry

log = logging.getLogger(__name__)

# A store sends a webhook once and Celerp never acknowledges it (it arrives over the
# gateway), so a delivery refused because modules were being changed is handled again
# here, MODULE_SETTLE_SECONDS later (the wait the refusal names), up to this many times.
# Past that, the next scheduled sync pulls what it named.
_BUSY_RETRIES = 3
# The deliveries waiting to be handled again, kept so none is dropped mid-wait.
_RETRYING: set[asyncio.Task] = set()

# Maps webhook topic prefixes to sync entity names
_TOPIC_ENTITY_MAP = {
    "products": "products",
    "product": "products",
    "orders": "orders",
    "order": "orders",
    "customers": "contacts",
    "customer": "contacts",
}


@dataclass
class WebhookEvent:
    """Lightweight webhook notification pushed from relay."""
    platform: str
    topic: str
    entity_id: str | None = None
    payload: dict[str, Any] | None = None


def topic_to_entity(topic: str) -> str | None:
    """Map a webhook topic (e.g. 'products/update') to a sync entity name."""
    prefix = topic.split("/")[0].split(".")[0]
    return _TOPIC_ENTITY_MAP.get(prefix)


async def handle_webhook(
    event: WebhookEvent,
    ctx: ConnectorContext,
    direction: SyncDirection = SyncDirection.BOTH,
    *,
    expected_config_id=None,
    expected_store_handle: str | None = None,
    expected_webhook_secret: str | None = None,
) -> None:
    """Handle an incoming webhook event by running a targeted inbound sync. One refused
    because modules were being changed is handled again later (_BUSY_RETRIES), without
    holding up the caller."""
    fence = {"expected_config_id": expected_config_id, "expected_store_handle": expected_store_handle,
             "expected_webhook_secret": expected_webhook_secret}
    if await _handle_once(event, ctx, direction, **fence):
        _again_later(event, ctx, direction, fence, _BUSY_RETRIES)


def _again_later(event: WebhookEvent, ctx: ConnectorContext, direction: SyncDirection,
                 fence: dict, attempts: int) -> None:
    async def again() -> None:
        await asyncio.sleep(MODULE_SETTLE_SECONDS)
        try:
            busy = await _handle_once(event, ctx, direction, **fence)
        except Exception as exc:  # noqa: BLE001 - as the gateway logs a delivery that failed
            log.warning("webhook: %s/%s for %s failed when tried again: %s",
                        event.platform, event.topic, ctx.company_id, exc)
            return
        if not busy:
            return
        if attempts > 1:
            _again_later(event, ctx, direction, fence, attempts - 1)
        else:
            log.warning("webhook: %s/%s for %s still refused while modules change; the next "
                        "scheduled sync pulls it", event.platform, event.topic, ctx.company_id)

    task = asyncio.create_task(again())
    _RETRYING.add(task)
    task.add_done_callback(_RETRYING.discard)


async def _handle_once(
    event: WebhookEvent,
    ctx: ConnectorContext,
    direction: SyncDirection,
    *,
    expected_config_id,
    expected_store_handle: str | None,
    expected_webhook_secret: str | None,
) -> bool:
    """Handle the event once. True when it was refused because modules were being changed,
    so handling it again later can succeed."""
    entity = topic_to_entity(event.topic)
    if not entity:
        log.warning("webhook: unknown topic %s for %s", event.topic, event.platform)
        return False

    try:
        connector = connector_registry.get(event.platform)
    except KeyError:
        log.error("webhook: unknown platform %s", event.platform)
        return False

    deletion_busy = False

    normalized_topic = event.topic.replace("/", ".").lower()
    if event.platform == "woocommerce" and normalized_topic == "product.deleted":
        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )
        from celerp.connectors.relay_token import fetch_context
        from celerp.db import get_session_ctx

        try:
            async with get_session_ctx() as guard_session:
                config = await lock_connector_operation(
                    guard_session,
                    ctx.company_id,
                    event.platform,
                    require_owner=True,
                )
                if expected_config_id is not None and config.id != expected_config_id:
                    return False
                if (
                    expected_webhook_secret is not None
                    and config.webhook_secret != expected_webhook_secret
                ):
                    return False
                current_direction = SyncDirection(config.direction)
                if not entity_allowed(entity, current_direction):
                    return False
                current_ctx = await fetch_context(
                    ctx.company_id,
                    event.platform,
                    ownership_session=guard_session,
                )
                if current_ctx is None:
                    return False
                if (
                    expected_store_handle is not None
                    and current_ctx.store_handle != expected_store_handle
                ):
                    return False
                await connector.handle_product_deleted(
                    current_ctx, event.payload or {}
                )
                await guard_session.commit()
        except ConnectorOwnershipError:
            return False
        except ModuleRefused as exc:
            # Refused before anything changed. The product sync below records the
            # refusal on the store's sync history; one refused as busy is handled again,
            # and once Inventory runs the daily reconcile pass finds the product the
            # store no longer returns.
            deletion_busy = isinstance(exc, ModuleBusy)
        else:
            log.info(
                "webhook: processed targeted WooCommerce product deletion for %s",
                ctx.company_id,
            )
            return False

    result = await run_sync(
        connector,
        ctx,
        entity,
        direction=direction,
        # WooCommerce names only the deleted order; the reconciliation pass
        # finds every imported order the store no longer returns.
        reconcile=event.platform == "woocommerce" and normalized_topic == "order.deleted",
        expected_config_id=expected_config_id,
        expected_store_handle=expected_store_handle,
        expected_webhook_secret=expected_webhook_secret,
    )
    log.info(
        "webhook: processed %s/%s for %s",
        event.platform,
        event.topic,
        ctx.company_id,
    )
    return deletion_busy or result.busy


async def dispatch_woocommerce_webhook(
    raw_body: bytes, signature: str, topic: str
) -> bool:
    """Verify a WooCommerce delivery against the current connector generation."""
    import json

    import sqlalchemy as sa

    from celerp.connectors.ownership import lock_connector_key
    from celerp.connectors.relay_token import fetch_context
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    if not signature:
        return False

    connector = connector_registry.get("woocommerce")

    async with get_session_ctx() as session:
        rows = await session.execute(
            sa.select(
                ConnectorConfig.company_id,
                ConnectorConfig.webhook_secret,
                ConnectorConfig.direction,
            ).where(ConnectorConfig.connector == "woocommerce")
        )
        candidates = [
            row[0]
            for row in rows.all()
            if row[1]
            and connector.validate_webhook(raw_body, signature, row[1])
        ]

    for company_id in candidates:
        async with get_session_ctx() as guard_session:
            await lock_connector_key(guard_session, "woocommerce")
            current_rows = await guard_session.execute(
                sa.select(
                    ConnectorConfig.company_id,
                    ConnectorConfig.webhook_secret,
                    ConnectorConfig.direction,
                )
                .where(
                    ConnectorConfig.connector == "woocommerce",
                    ConnectorConfig.company_id == str(company_id),
                )
                .with_for_update()
            )
            current = next(
                (
                    row
                    for row in current_rows.all()
                    if str(row[0]) == str(company_id)
                ),
                None,
            )
            if current is None:
                continue
            secret = current[1]
            if (
                not secret
                or not connector.validate_webhook(raw_body, signature, secret)
            ):
                continue

            ctx = await fetch_context(company_id, "woocommerce")
            if ctx is None:
                continue
            direction = SyncDirection(current[2] or "both")
            expected_store_handle = ctx.store_handle

        try:
            data = json.loads(raw_body or b"{}")
        except (ValueError, TypeError):
            data = {}
        event = WebhookEvent(platform="woocommerce", topic=topic, payload=data)
        await handle_webhook(
            event,
            ctx,
            direction,
            expected_store_handle=expected_store_handle,
            expected_webhook_secret=secret,
        )
        return True

    return False
