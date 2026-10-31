# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Bring the store's webhooks to the ones the connection's directions call for.

Webhook state is the "webhooks" execution resource, so one reconcile runs at a time
per connection, fenced like every other connector write. Anything else that changes
what the hooks should be (a direction, a secret, the first sync starting) only marks
them dirty, bumps ``webhooks_version`` and records a rerun.

The store never returns a hook's secret, so the connection records each hook it created
or adopted with the fingerprint of the secret it last gave it (ConnectorConfig.webhook_hooks),
right after the store acknowledged the change. Per topic the hook kept is the recorded one,
else the lowest Celerp hook (one a run created before it could record it is adopted, never
duplicated); a kept hook given another secret, or disabled by the store, is updated in place.
Every kept hook is read back before any other is deleted. The dirty mark is cleared only
when the version is still the one the reconcile read."""
from __future__ import annotations

import hashlib
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


def secret_fingerprint(secret: str) -> str:
    """What the connection records of the secret a hook was given."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


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
        read = (config.id, config.webhooks_version, config.webhook_secret, desired, config.webhook_hooks)
        await s.commit()
    return read


async def _record_hook(ex: Execution, config_id: int, hook_id, fingerprint: str | None) -> None:
    """Record one hook the store acknowledged (or, with no fingerprint, one it deleted)
    before the reconcile makes its next store request."""
    from celerp.connectors.ownership import lock_connection_row
    from celerp.db import SessionLocal

    async with SessionLocal() as s:
        config = await lock_connection_row(s, ex.company_id, ex.connector)
        if config is not None and config.id == config_id:
            hooks = config.webhook_hooks
            if fingerprint is None:
                hooks.pop(str(hook_id), None)
            else:
                hooks[str(hook_id)] = fingerprint
            config.webhook_hooks = hooks
        await s.commit()


async def reconcile_webhooks(ex: Execution) -> None:
    """Inside an execution holding the webhooks resource."""
    from celerp.connectors.ownership import lock_connection_row
    from celerp.db import SessionLocal

    ex.require(WEBHOOKS)
    read = await _read(ex)
    if read is None or ex.ctx is None:
        return
    config_id, version, secret, desired, recorded = read
    if secret:  # only a connection that signs its hooks has any to reconcile
        await _reconcile_store(ex, config_id, secret, desired, recorded)
    async with SessionLocal() as s:
        config = await lock_connection_row(s, ex.company_id, ex.connector)
        if config is not None and config.id == config_id and config.webhooks_version == version:
            config.webhooks_dirty = False
        await s.commit()


async def _reconcile_store(ex: Execution, config_id: int, secret: str, desired: list[str],
                           recorded: dict[str, str | None]) -> None:
    """Keep, adopt, update or create one hook per desired topic, recording each
    acknowledged change; verify them; then delete every other Celerp hook on the store
    and every recorded one not kept (a recorded hook the store lost reads as deleted)."""
    from celerp import connectors
    from celerp.connectors.woocommerce import webhook_delivery_url

    connector = connectors.get(ex.connector)
    url = webhook_delivery_url()
    fingerprint = secret_fingerprint(secret)
    hooks = sorted(await connector.list_webhooks(ex.ctx, url), key=lambda h: int(h["id"]))
    kept: dict[str, dict] = {}
    for topic in desired:
        same = [h for h in hooks if h.get("topic") == topic]
        hook = next((h for h in same if str(h["id"]) in recorded), same[0] if same else None)
        if hook is None:
            hook = await connector.create_webhook(ex.ctx, url, topic, secret)
        elif recorded.get(str(hook["id"])) != fingerprint or hook.get("status") != "active":
            hook = await connector.update_webhook(ex.ctx, hook["id"], secret)
        else:
            kept[topic] = hook
            continue
        await _record_hook(ex, config_id, hook["id"], fingerprint)
        kept[topic] = hook
    for topic, hook in kept.items():
        seen = await connector.get_webhook(ex.ctx, hook["id"])
        if (seen is None or seen.get("status") != "active" or seen.get("topic") != topic
                or seen.get("delivery_url") != url):
            raise RuntimeError(f"{connector.display_name} webhook for {topic} is not active")
    keep_ids = {str(h["id"]) for h in kept.values()}
    retired = [str(h["id"]) for h in hooks] + sorted(set(recorded) - {str(h["id"]) for h in hooks})
    for hook_id in retired:
        if hook_id not in keep_ids:
            await connector.delete_webhook(ex.ctx, hook_id)
            await _record_hook(ex, config_id, hook_id, None)
