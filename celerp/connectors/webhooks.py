# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Webhook handler - processes inbound webhook events from e-commerce platforms.

This module is called by the relay when it receives and validates a webhook.
The relay pushes a lightweight notification via SSE; the desktop fetches
the changed entity from the platform API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from celerp.connectors.base import ConnectorContext, SyncDirection, entity_allowed
from celerp.connectors.sync_runner import run_sync
import celerp.connectors as connector_registry

log = logging.getLogger(__name__)

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
) -> None:
    """Handle an incoming webhook event by running a targeted inbound sync."""
    entity = topic_to_entity(event.topic)
    if not entity:
        log.warning("webhook: unknown topic %s for %s", event.topic, event.platform)
        return

    try:
        connector = connector_registry.get(event.platform)
    except KeyError:
        log.error("webhook: unknown platform %s", event.platform)
        return

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
                    return
                current_direction = SyncDirection(config.direction)
                if not entity_allowed(entity, current_direction):
                    return
                current_ctx = await fetch_context(
                    ctx.company_id,
                    event.platform,
                    ownership_session=guard_session,
                )
                if current_ctx is None:
                    return
                if (
                    expected_store_handle is not None
                    and current_ctx.store_handle != expected_store_handle
                ):
                    return
                await connector.handle_product_deleted(
                    current_ctx, event.payload or {}
                )
                await guard_session.commit()
        except ConnectorOwnershipError:
            return
        log.info(
            "webhook: processed targeted WooCommerce product deletion for %s",
            ctx.company_id,
        )
        return

    await run_sync(
        connector,
        ctx,
        entity,
        direction=direction,
        expected_config_id=expected_config_id,
        expected_store_handle=expected_store_handle,
    )
    log.info(
        "webhook: processed %s/%s for %s",
        event.platform,
        event.topic,
        ctx.company_id,
    )


async def dispatch_woocommerce_webhook(
    raw_body: bytes, signature: str, topic: str
) -> bool:
    """Verify a WooCommerce delivery against the current connector generation."""
    import json

    import sqlalchemy as sa

    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        lock_connector_operation,
    )
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
            ).where(ConnectorConfig.connector == "woocommerce")
        )
        candidates = [
            company_id
            for company_id, secret in rows.all()
            if secret and connector.validate_webhook(raw_body, signature, secret)
        ]

    for company_id in candidates:
        try:
            async with get_session_ctx() as guard_session:
                config = await lock_connector_operation(
                    guard_session,
                    company_id,
                    "woocommerce",
                    require_owner=True,
                )
                secret = config.webhook_secret
                if (
                    not secret
                    or not connector.validate_webhook(raw_body, signature, secret)
                ):
                    await guard_session.rollback()
                    continue
                ctx = await fetch_context(
                    company_id,
                    "woocommerce",
                    ownership_session=guard_session,
                )
                if ctx is None:
                    await guard_session.rollback()
                    continue
                direction = SyncDirection(config.direction or "both")
                expected_config_id = config.id
                expected_store_handle = ctx.store_handle
                await guard_session.commit()
        except ConnectorOwnershipError:
            continue

        try:
            data = json.loads(raw_body or b"{}")
        except (ValueError, TypeError):
            data = {}
        event = WebhookEvent(platform="woocommerce", topic=topic, payload=data)
        await handle_webhook(
            event,
            ctx,
            direction,
            expected_config_id=expected_config_id,
            expected_store_handle=expected_store_handle,
        )
        return True

    return False
