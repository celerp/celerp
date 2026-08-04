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
    existing = _client.get_client()
    if existing is not None:
        if existing.is_serving(settings.gateway_token):
            return
        # A client the relay rejected (or one holding a token that has since rotated
        # out) idles in run() with a dead socket and never revives itself, so the
        # plain "already set" no-op would strand the tunnel on the stale credential.
        # stop() breaks that idle loop and its run task exits on the next tick; drop
        # the reference and rebuild below.
        existing.stop()
        _client.set_client(None)
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


async def shutdown() -> None:
    """Close the tunnel and cancel its run task, whoever started it. Safe to call
    when nothing is running."""
    global _run_task
    from celerp.gateway import client as _client

    gw = _client.get_client()
    if gw is not None:
        await gw.close()
    if _run_task is not None:
        _run_task.cancel()
        try:
            await asyncio.wait_for(_run_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _run_task = None
    _client.set_client(None)


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
