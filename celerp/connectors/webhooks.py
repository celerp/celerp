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
    "inventory_levels": "inventory",
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
) -> None:
    """Handle an incoming webhook event by running a targeted sync.

    Only processes inbound-relevant entities. Respects direction config.
    """
    entity = topic_to_entity(event.topic)
    if not entity:
        log.warning("webhook: unknown topic %s for %s", event.topic, event.platform)
        return

    if not entity_allowed(entity, direction):
        log.debug("webhook: %s blocked by direction=%s", entity, direction.value)
        return

    try:
        connector = connector_registry.get(event.platform)
    except KeyError:
        log.error("webhook: unknown platform %s", event.platform)
        return

    # Run a targeted incremental sync for just this entity type.
    # Pass since=None so the sync methods use the last SyncRun timestamp.
    await run_sync(connector, ctx, entity, direction=direction)
    log.info("webhook: processed %s/%s for %s", event.platform, event.topic, ctx.company_id)


async def dispatch_woocommerce_webhook(raw_body: bytes, signature: str, topic: str) -> bool:
    """Verify and process a WooCommerce webhook the relay forwarded to this instance.

    WooCommerce signs the raw body with the per-config secret (base64 HMAC-SHA256,
    sent as X-WC-Webhook-Signature). We try each configured WooCommerce company's
    secret; the first that verifies owns the event. Returns True if handled, False
    if no configured secret validated the signature (a forged delivery is ignored).
    """
    import json

    import sqlalchemy as sa

    from celerp.connectors.relay_token import fetch_context
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    if not signature:
        return False

    connector = connector_registry.get("woocommerce")

    async with get_session_ctx() as session:
        rows = await session.execute(
            sa.select(
                ConnectorConfig.company_id, ConnectorConfig.direction, ConnectorConfig.webhook_secret
            ).where(ConnectorConfig.connector == "woocommerce")
        )
        configs = rows.all()

    for company_id, direction, secret in configs:
        if not secret or not connector.validate_webhook(raw_body, signature, secret):
            continue
        ctx = await fetch_context(company_id, "woocommerce")
        if ctx is None:
            continue
        try:
            data = json.loads(raw_body or b"{}")
        except (ValueError, TypeError):
            data = {}
        event = WebhookEvent(platform="woocommerce", topic=topic, payload=data)
        await handle_webhook(event, ctx, direction=SyncDirection(direction))
        return True

    return False
