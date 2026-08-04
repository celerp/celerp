# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""ensure_running rebuilds a dead tunnel instead of stranding it.

A client the relay rejected idles in run() with a dead socket and never revives
itself. The plain "already set" no-op left it in place, so a reconnect that
persisted a fresh token never reached the relay and the settings page kept reading
the stale error. ensure_running now tears a non-serving client down and constructs
a new one on the current token.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import asyncio

import pytest

from celerp.config import settings
import celerp.gateway as gateway
import celerp.gateway.client as client_mod


class _FakeClient:
    """Stands in for GatewayClient without a live WebSocket: run() returns at once
    and is_serving() is driven by a plain token match."""

    def __init__(self, gateway_token: str, instance_id: str, gateway_url: str) -> None:
        self._token = gateway_token
        self.instance_id = instance_id
        self.stopped = False
        self.ran = False

    def is_serving(self, token: str) -> bool:
        return self._token == token

    def stop(self) -> None:
        self.stopped = True

    async def run(self) -> None:
        self.ran = True


@pytest.fixture(autouse=True)
def _reset():
    original_client = client_mod.get_client()
    original_task = gateway._run_task
    original_token = settings.gateway_token
    original_iid = settings.gateway_instance_id
    original_disconnected = settings.cloud_disconnected
    # Start every case from a known-connected baseline so the tunnel construction
    # path is exercised deterministically; a real config on the dev box may have left
    # settings.cloud_disconnected True. The disconnect case sets it True itself.
    settings.cloud_disconnected = False
    yield
    if gateway._run_task is not None and gateway._run_task is not original_task:
        gateway._run_task.cancel()
    gateway._run_task = original_task
    client_mod.set_client(original_client)
    settings.gateway_token = original_token
    settings.gateway_instance_id = original_iid
    settings.cloud_disconnected = original_disconnected


@pytest.mark.asyncio
async def test_ensure_running_noop_when_client_serving_current_token(monkeypatch):
    """A healthy client on the current token is the tunnel: ensure_running leaves it
    untouched, never stopping or rebuilding a working connection."""
    monkeypatch.setattr(client_mod, "GatewayClient", _FakeClient)
    settings.gateway_token = "tok-live"
    settings.gateway_instance_id = "iid-1"
    existing = _FakeClient("tok-live", "iid-1", "wss://relay.test")
    client_mod.set_client(existing)

    gateway.ensure_running()

    assert client_mod.get_client() is existing
    assert existing.stopped is False


@pytest.mark.asyncio
async def test_ensure_running_stays_down_while_cloud_disconnected(monkeypatch):
    """A sticky Cloud disconnect holds the tunnel down through every construction
    path. gateway_token can be present with cloud_disconnected still True: a
    GATEWAY_TOKEN env var populates settings at construction, before
    load_cloud_config, so its disconnect suppression never sees that token. The boot
    gate (paid public_url, or a free instance with a live share) and the share seam
    both call ensure_running with that token set, so the guard belongs here at the
    single construction site, not only in the config loader."""
    monkeypatch.setattr(client_mod, "GatewayClient", _FakeClient)
    settings.gateway_token = "tok-env"
    settings.gateway_instance_id = "iid-1"
    settings.cloud_disconnected = True
    client_mod.set_client(None)

    gateway.ensure_running()
    await asyncio.sleep(0)  # a constructed client's run task would start here

    assert client_mod.get_client() is None


@pytest.mark.asyncio
async def test_ensure_running_rebuilds_when_existing_client_not_serving(monkeypatch):
    """A non-serving client (rejected credential, or a token that rotated out) is
    torn down and replaced with a fresh client on the current token."""
    monkeypatch.setattr(client_mod, "GatewayClient", _FakeClient)
    settings.gateway_token = "tok-fresh"
    settings.gateway_instance_id = "iid-1"
    dead = _FakeClient("tok-stale", "iid-1", "wss://relay.test")
    client_mod.set_client(dead)

    gateway.ensure_running()
    await asyncio.sleep(0)  # let the rebuilt client's run task start

    rebuilt = client_mod.get_client()
    assert dead.stopped is True
    assert rebuilt is not dead
    assert rebuilt._token == "tok-fresh"
    assert rebuilt.ran is True
