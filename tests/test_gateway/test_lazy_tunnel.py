# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Lazy-tunnel reaper for free instances (plan 3.1).

A free instance's relay tunnel is not persistent: it comes up while a share is
live and drops after a grace window with nothing to serve. These tests drive the
reaper's decision (`_should_reap`) and its teardown action (`_reaper_loop`)
directly, without a live WebSocket.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import asyncio
import time

import pytest

from celerp.config import settings
from celerp.gateway.client import GatewayClient, get_client, set_client


def _make_client() -> GatewayClient:
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


@pytest.fixture
def free_settings():
    """A free instance: relay-bound (token set) but no public vanity URL."""
    original = settings.celerp_public_url
    settings.celerp_public_url = ""
    yield
    settings.celerp_public_url = original


@pytest.fixture(autouse=True)
def reset_client_singleton():
    original = get_client()
    yield
    set_client(original)


@pytest.mark.asyncio
async def test_gateway_idles_out_when_no_active_share(free_settings, monkeypatch):
    """No live share and nothing served past the grace window -> the reaper drops
    the tunnel and clears the client singleton."""
    import celerp.gateway as gateway
    import celerp.gateway.client as client_mod

    async def _no_active_share() -> bool:
        return False

    monkeypatch.setattr(gateway, "has_active_share", _no_active_share)
    # Fire the reaper's timeout almost immediately instead of waiting 15 minutes.
    monkeypatch.setattr(client_mod, "_REAP_INTERVAL", 0.02)

    client = _make_client()
    # No request served recently (well past the grace window).
    client._last_request_monotonic = time.monotonic() - (client_mod._REAP_GRACE + 60)
    client._running = True
    set_client(client)

    assert await client._should_reap() is True

    await client._reaper_loop()

    assert get_client() is None
    assert client._running is False


@pytest.mark.asyncio
async def test_gateway_stays_up_across_reshare_within_grace(free_settings, monkeypatch):
    """Revoke then re-create inside the grace window must not flap the tunnel: with
    a recent request (or a fresh active share) the reaper leaves the connection up."""
    import celerp.gateway as gateway
    import celerp.gateway.client as client_mod

    share_live = False

    async def _active_share() -> bool:
        return share_live

    monkeypatch.setattr(gateway, "has_active_share", _active_share)
    monkeypatch.setattr(client_mod, "_REAP_INTERVAL", 0.02)

    client = _make_client()
    # A request was just served: inside the grace window even with no share live
    # (the gap between an old share being revoked and the new one being created).
    client._last_request_monotonic = time.monotonic()
    client._running = True
    set_client(client)

    # No active share, but recent activity -> grace holds the tunnel up.
    assert await client._should_reap() is False
    # The re-created share is now live -> still held up.
    share_live = True
    assert await client._should_reap() is False

    reaper = asyncio.ensure_future(client._reaper_loop())
    try:
        await asyncio.sleep(0.1)  # several reaper ticks
        assert get_client() is client  # never torn down
        assert client._running is True
    finally:
        client._reaper_stop.set()
        await reaper
