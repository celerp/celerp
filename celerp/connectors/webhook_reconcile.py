# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Bring the store's webhooks to the ones the connection's directions call for.

Webhook state is the "webhooks" execution resource, so one reconcile runs at a time
per connection, fenced like every other connector write. Anything else that changes
what the hooks should be (a direction, a secret, the first sync starting) only marks
them dirty, bumps ``webhooks_version`` and records a rerun.

The reconcile never deletes first: it creates or reactivates each wanted hook, reads
each one back, and only then deletes the rest. Per topic the hook kept is the lowest
id with the current secret, so two reconciles over one list keep the same hook. The
dirty mark is cleared only when the version is still the one the reconcile read."""
from __future__ import annotations

import json
import logging

import sqlalchemy as sa

from celerp.connectors.execution import (
    WEBHOOKS,
    AdmissionBusy,
    Execution,
    ExecutionBusy,
    ExecutionRefused,
    connector_execution,
    record_rerun,
)

log = logging.getLogger(__name__)


async def run_webhook_reconcile(company_id, connector_name: str) -> None:
    """Reconcile now, or record a rerun when another execution holds the webhooks.
    A connection that is gone or not yet started is left alone."""
    from celerp.connectors.ownership import ConnectorOwnershipError

    try:
        async with connector_execution(company_id, connector_name, "webhooks", [WEBHOOKS],
                                       admission_wait=0) as ex:
            await reconcile_webhooks(ex)
    except (ExecutionBusy, AdmissionBusy):
        await record_rerun(company_id, connector_name, WEBHOOKS)
    except (ExecutionRefused, ConnectorOwnershipError) as exc:
        log.info("webhook reconcile for %s skipped: %s", connector_name, exc)


async def mark_webhooks_dirty(session, config) -> None:
    """In the caller's transaction, which holds ``config`` FOR UPDATE: the hooks must be
    reconciled by the holder of the webhooks resource. The mark is the request: the
    holder runs it before it releases, the sweeper when no execution holds them."""
    config.webhooks_dirty = True
    config.webhooks_version = (config.webhooks_version or 0) + 1
    await session.flush()


async def _read(ex: Execution):
    from celerp import connectors
    from celerp.connectors.ownership import connector_owned_by_company
    from celerp.db import SessionLocal
    from celerp.models.connector_config import ConnectorConfig
    from celerp.models.connector_first_sync import ConnectorFirstSync

    connector = connectors.get(ex.connector)
    async with SessionLocal() as s:
        if not await connector_owned_by_company(s, ex.company_id, ex.connector):
            return None
        config = await s.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == ex.company_id, ConnectorConfig.connector == ex.connector))
        first_sync = await s.get(ConnectorFirstSync, (ex.company_id, ex.connector))
        if config is None:
            return None
        started = first_sync is not None and first_sync.state != "pending"
        desired = connector.webhook_topics(config.entity_directions) if started else []
        read = (config.id, config.webhooks_version, config.webhook_secret, desired)
        await s.commit()
    return read


async def reconcile_webhooks(ex: Execution) -> None:
    """Inside an execution holding the webhooks resource."""
    from celerp.db import SessionLocal
    from celerp.models.connector_config import ConnectorConfig

    ex.require(WEBHOOKS)
    read = await _read(ex)
    if read is None or ex.ctx is None:
        return
    config_id, version, secret, desired = read
    kept: dict[str, dict] = {}
    if secret:  # only a connection that signs its hooks has any to reconcile
        kept = await _reconcile_store(ex, secret, desired)
    ids = [str(kept[topic]["id"]) for topic in desired if topic in kept]
    async with SessionLocal() as s:
        await s.execute(sa.update(ConnectorConfig).where(
            ConnectorConfig.id == config_id, ConnectorConfig.webhooks_version == version,
        ).values(webhooks_dirty=False, webhook_ids_json=json.dumps(ids) if ids else None))
        await s.commit()


async def _reconcile_store(ex: Execution, secret: str, desired: list[str]) -> dict[str, dict]:
    """Create, activate and verify one hook per desired topic; delete every other
    hook delivering to Celerp. Returns the kept hook per topic."""
    from celerp import connectors
    from celerp.connectors.woocommerce import webhook_delivery_url

    connector = connectors.get(ex.connector)
    url = webhook_delivery_url()
    hooks = sorted(await connector.list_webhooks(ex.ctx, url), key=lambda h: int(h["id"]))
    kept: dict[str, dict] = {}
    for topic in desired:
        hook = next((h for h in hooks if h.get("topic") == topic and h.get("secret") == secret), None)
        if hook is None:
            hook = await connector.create_webhook(ex.ctx, url, topic, secret)
        elif hook.get("status") != "active":
            hook = await connector.activate_webhook(ex.ctx, hook["id"])
        kept[topic] = hook
    for topic, hook in kept.items():
        seen = await connector.get_webhook(ex.ctx, hook["id"])
        if seen is None or seen.get("status") != "active" or seen.get("topic") != topic:
            raise RuntimeError(f"{connector.display_name} webhook for {topic} is not active")
    keep_ids = {str(h["id"]) for h in kept.values()}
    for hook in hooks:
        if str(hook["id"]) not in keep_ids:
            await connector.delete_webhook(ex.ctx, hook["id"])
    return kept
