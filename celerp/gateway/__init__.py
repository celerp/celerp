# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
# celerp.gateway is a protected BSL internal — 3P modules must not import it.
"""Core gateway lifecycle.

The relay tunnel is lazy for a free instance: it comes up only while at least one
share link is live and drops after a grace window with nothing to serve. This module
owns the single place the client is constructed (`ensure_running`), the teardown
(`shutdown`), and the share-existence check the boot gate and the reaper share.

MIT default modules must not import this package; they reach the tunnel through the
`celerp.services.relay_share` seam instead.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# The run() task owned by ensure_running(), so shutdown() can cancel whichever caller
# (boot gate, auto-activate, or a share-create) brought the tunnel up.
_run_task: asyncio.Task | None = None


def ensure_running() -> None:
    """Bring the relay tunnel up if it is not already. Idempotent: a no-op when a
    client is already set. The single construction site for GatewayClient; the boot
    gate, auto-activate, the reaper's restart path, and the docs share seam all route
    here. A no-op without a configured gateway_token (self-hosted, never contacts the
    relay)."""
    global _run_task
    from celerp.config import settings
    from celerp.gateway import client as _client

    if not settings.gateway_token:
        return
    if settings.cloud_disconnected:
        # A sticky Cloud disconnect holds the tunnel down through every construction
        # path - boot gate, auto-activate, the reaper's restart, the docs share seam -
        # no matter how gateway_token got set. A GATEWAY_TOKEN env var populates
        # settings at construction, before load_cloud_config, so that loader's
        # disconnect suppression never sees the token; the guard has to live here at
        # the single construction site too. Reconnect clears the flag before it reaches
        # here, so a deliberate reconnect is unaffected.
        return
    if _client.get_client() is not None:
        return
    import uuid

    instance_id = settings.gateway_instance_id or str(uuid.uuid4())
    gw = _client.GatewayClient(
        gateway_token=settings.gateway_token,
        instance_id=instance_id,
        gateway_url=settings.gateway_url,
    )
    _client.set_client(gw)
    _run_task = asyncio.create_task(gw.run())
    log.info("Gateway client started (instance_id=%s)", instance_id)


async def shutdown(*, expected_client=None) -> bool:
    """Close exactly the current gateway generation, or refuse a stale request."""
    global _run_task
    from celerp.gateway import client as _client

    gw = _client.get_client()
    if expected_client is not None and gw is not expected_client:
        return False
    run_task = _run_task
    if gw is not None:
        await gw.close()
    if run_task is not None:
        run_task.cancel()
        try:
            await asyncio.wait_for(run_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if _run_task is run_task:
        _run_task = None
    if _client.get_client() is gw:
        _client.set_client(None)
    return True


async def has_active_share() -> bool:
    """True if any share link currently resolves (not revoked, not expired). One
    SELECT; drives the boot-time lazy-tunnel gate and the reaper's teardown check."""
    from sqlalchemy import select

    from celerp.db import SessionLocal
    from celerp.models.share import DocShareToken, active_filter

    async with SessionLocal() as session:
        row = (await session.execute(
            select(DocShareToken.id).where(active_filter()).limit(1)
        )).first()
    return row is not None
