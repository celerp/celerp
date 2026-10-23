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

from celerp.connectors.base import ConnectorContext, SyncDirection
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


async def _first_sync_started(company_id: str, platform: str) -> bool:
    """Whether the company's connection to ``platform`` started its first sync; a
    connection with no configuration is left to the sync's own checks."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    async with get_session_ctx() as session:
        config = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id), ConnectorConfig.connector == platform))
    return config is None or config.first_sync_at is not None


async def _own_push(company_id: str, payload: dict) -> bool:
    """Whether a WooCommerce product update is the store echoing Celerp's own push:
    its modified time is the one the store returned for that push."""
    import uuid

    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.projections import Projection
    from celerp_inventory.services import external_link_for_state

    remote_id, parent_id = payload.get("id"), payload.get("parent_id")
    modified = payload.get("date_modified_gmt")
    if remote_id in (None, "") or not modified:
        return False
    is_variation = parent_id not in (None, "", 0, "0")
    product_id = str(parent_id) if is_variation else str(remote_id)
    async with get_session_ctx() as session:
        rows = (await session.execute(sa.select(Projection.state).where(
            Projection.company_id == uuid.UUID(str(company_id)),
            Projection.entity_type == "item",
            Projection.state["external_links"]["woocommerce"]["product_id"].as_string() == product_id,
        ))).scalars().all()
    for state in rows:
        link = external_link_for_state(state or {}, "woocommerce")
        if is_variation and str(link.get("variation_id") or "") != str(remote_id):
            continue
        if link.get("last_pushed_modified") == modified:
            return True
    return False


def topic_to_entity(topic: str) -> str | None:
    """Map a webhook topic (e.g. 'products/update') to a sync entity name."""
    prefix = topic.split("/")[0].split(".")[0]
    return _TOPIC_ENTITY_MAP.get(prefix)


async def handle_webhook(
    event: WebhookEvent,
    ctx: ConnectorContext,
    *,
    expected_config_id=None,
    expected_store_handle: str | None = None,
    expected_webhook_secret: str | None = None,
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
    if not await _first_sync_started(ctx.company_id, event.platform):
        # Accepted and left alone: nothing syncs before a person starts the first sync.
        log.info("webhook: %s/%s before the first sync, ignored", event.platform, event.topic)
        return
    if (event.platform == "woocommerce" and normalized_topic == "product.updated"
            and await _own_push(ctx.company_id, event.payload or {})):
        log.info("webhook: WooCommerce echo of Celerp's own product update, ignored")
        return
    if event.platform == "woocommerce" and normalized_topic == "product.deleted":
        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )
        from celerp.connectors.relay_token import ConnectRequired, fetch_context
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
                if (
                    expected_webhook_secret is not None
                    and config.webhook_secret != expected_webhook_secret
                ):
                    return
                # A deletion matters to every linked product, whichever way products sync.
                if (config.entity_directions or {}).get("products", SyncDirection.OFF.value) == SyncDirection.OFF.value:
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
        except (ConnectorOwnershipError, ConnectRequired):
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


async def dispatch_woocommerce_webhook(
    raw_body: bytes, signature: str, topic: str
) -> bool:
    """Verify a WooCommerce delivery against the current connector generation."""
    import json

    import sqlalchemy as sa

    from celerp.connectors.ownership import lock_connector_key
    from celerp.connectors.relay_token import ConnectRequired, fetch_context
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

            try:
                ctx = await fetch_context(company_id, "woocommerce")
            except ConnectRequired:
                # Syncing is paused until Connect is renewed; the delivery is accepted.
                return True
            if ctx is None:
                continue
            expected_store_handle = ctx.store_handle

        try:
            data = json.loads(raw_body or b"{}")
        except (ValueError, TypeError):
            data = {}
        event = WebhookEvent(platform="woocommerce", topic=topic, payload=data)
        await handle_webhook(
            event,
            ctx,
            expected_store_handle=expected_store_handle,
            expected_webhook_secret=secret,
        )
        return True

    return False
