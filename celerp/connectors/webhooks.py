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
from typing import Any, Callable

import sqlalchemy as sa

from celerp.connectors.sync_runner import record_refused_run, run_sync
from celerp.modules.boundary import ModuleBusy, ModuleRefused
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
    company_id,
    *,
    expected_webhook_secret: str | None = None,
    store_matches: Callable[[str | None], bool] | None = None,
) -> None:
    """Handle an incoming webhook event by running a targeted inbound sync in a
    webhook execution. A delivery that finds the resource busy, or no free
    execution slot, or the first sync still running, records a rerun; a
    delivery before the first sync started, or for a connection that changed
    since it was verified, is dropped and logged. One refused because modules were
    being changed records a rerun too: a store sends a webhook once, and the rerun
    sweeper pulls what it named once the change has settled. It never raises to the
    store."""
    from celerp.connectors.execution import (
        AdmissionBusy,
        ExecutionBusy,
        ExecutionRefused,
        connector_execution,
        execution_resource,
        first_sync_state,
        incomplete,
        record_rerun,
        refusal_drops_work,
    )
    from celerp.connectors.ownership import ConnectorOwnershipError
    from celerp.connectors.relay_token import ConnectorUpgradeRequired, ConnectRequired
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    entity = topic_to_entity(event.topic)
    if not entity:
        log.warning("webhook: unknown topic %s for %s", event.topic, event.platform)
        return

    try:
        connector = connector_registry.get(event.platform)
    except KeyError:
        log.error("webhook: unknown platform %s", event.platform)
        return

    company_id = str(company_id)
    normalized_topic = event.topic.replace("/", ".").lower()
    async with get_session_ctx() as session:
        state = await first_sync_state(session, company_id, event.platform)
    if state in (None, "pending"):
        # Accepted and left alone: the first sync reads the whole store.
        log.info("webhook: %s/%s before the first sync, ignored", event.platform, event.topic)
        return

    resource = execution_resource("entity", entity)
    try:
        async with connector_execution(company_id, connector, "webhook", [resource], admission_wait=0) as ex:
            async with get_session_ctx() as session:
                config = await session.scalar(sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id, ConnectorConfig.connector == event.platform))
                secret = config.webhook_secret
            if expected_webhook_secret is not None and secret != expected_webhook_secret:
                log.info("webhook: %s reconnected since the delivery was verified, dropped", event.platform)
                return
            if store_matches is not None and not store_matches(ex.ctx.store_handle):
                log.info("webhook: %s delivery for another store, dropped", event.platform)
                return
            handled = rerun_needed = False
            if event.platform == "woocommerce" and normalized_topic == "product.deleted":
                try:
                    # A deletion matters to every linked product, whichever way products sync.
                    await connector.handle_product_deleted(ex.ctx, event.payload or {})
                    handled = True
                except ModuleRefused as exc:
                    # Refused before anything changed: recorded on the store's sync history
                    # in the refusal's words. Once Inventory runs, the daily reconcile pass
                    # finds the product the store no longer returns.
                    await record_refused_run(company_id, event.platform, entity, exc)
                    handled = True
                    rerun_needed = isinstance(exc, ModuleBusy)
            if not handled:
                result = await run_sync(
                    connector, company_id, entity, purpose="webhook",
                    # WooCommerce names only the deleted order; the reconciliation pass
                    # finds every imported order the store no longer returns.
                    reconcile=event.platform == "woocommerce" and normalized_topic == "order.deleted",
                )
                rerun_needed = rerun_needed or incomplete([result]) is not None
    except (ExecutionBusy, AdmissionBusy):
        await record_rerun(company_id, event.platform, resource)
        log.info("webhook: %s %s busy, rerun recorded", event.platform, resource)
        return
    except ExecutionRefused as exc:
        if not refusal_drops_work(exc.reason):
            await record_rerun(company_id, event.platform, resource)
        log.info("webhook: %s/%s not synced: %s", event.platform, event.topic, exc)
        return
    except (ConnectorOwnershipError, ConnectRequired, ConnectorUpgradeRequired) as exc:
        log.info("webhook: %s/%s not synced: %s", event.platform, event.topic, exc)
        return
    if rerun_needed:
        # Refused while modules change, or ran and failed: not delivered. Recorded once the
        # execution has released, so the sweeper runs it after the change settles or the
        # failure's cause clears, rather than the holder at once.
        await record_rerun(company_id, event.platform, resource)
        log.info("webhook: %s %s not completed, rerun recorded", event.platform, resource)
        return
    log.info("webhook: processed %s/%s for %s", event.platform, event.topic, company_id)


async def dispatch_woocommerce_webhook(
    raw_body: bytes, signature: str, topic: str
) -> bool:
    """Verify a WooCommerce delivery and sync it for the company whose secret signed it.

    Step A reads the candidates and checks the signature in its own short
    transaction and holds nothing after it; the webhook execution's guard then
    requires the same secret (a reconnect in between drops the delivery)."""
    import json

    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    if not signature:
        return False

    connector = connector_registry.get("woocommerce")

    async with get_session_ctx() as session:
        rows = (await session.execute(
            sa.select(ConnectorConfig.company_id, ConnectorConfig.webhook_secret)
            .where(ConnectorConfig.connector == "woocommerce")
        )).all()
    signed = next(((company_id, secret) for company_id, secret in rows
                   if secret and connector.validate_webhook(raw_body, signature, secret)), None)
    if signed is None:
        return False
    try:
        data = json.loads(raw_body or b"{}")
    except (ValueError, TypeError):
        data = {}
    event = WebhookEvent(platform="woocommerce", topic=topic, payload=data)
    await handle_webhook(event, signed[0], expected_webhook_secret=signed[1])
    return True
