# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Gateway generation ownership and construction regressions."""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import ast
import asyncio
from pathlib import Path

import pytest

from celerp.config import settings
import celerp.gateway as gateway
import celerp.gateway.client as client_mod
import celerp.gateway.state as gw_state


class _FakeClient:
    """GatewayClient stand-in without a live WebSocket."""

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
    original_session = gw_state.get_session_token()
    settings.cloud_disconnected = False
    yield
    if gateway._run_task is not None and gateway._run_task is not original_task:
        gateway._run_task.cancel()
    gateway._run_task = original_task
    client_mod.set_client(original_client)
    gw_state.set_session_token(original_session)
    settings.gateway_token = original_token
    settings.gateway_instance_id = original_iid
    settings.cloud_disconnected = original_disconnected


@pytest.mark.asyncio
async def test_ensure_running_noop_when_client_serving_current_token(monkeypatch):
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
    monkeypatch.setattr(client_mod, "GatewayClient", _FakeClient)
    settings.gateway_token = "tok-env"
    settings.gateway_instance_id = "iid-1"
    settings.cloud_disconnected = True
    client_mod.set_client(None)

    gateway.ensure_running()
    await asyncio.sleep(0)

    assert client_mod.get_client() is None


@pytest.mark.asyncio
async def test_ensure_running_never_replaces_an_existing_generation(monkeypatch):
    """Replacement is async lifecycle work, never a synchronous overlap."""
    monkeypatch.setattr(client_mod, "GatewayClient", _FakeClient)
    settings.gateway_token = "tok-fresh"
    settings.gateway_instance_id = "iid-1"
    existing = _FakeClient("tok-stale", "iid-1", "wss://relay.test")
    client_mod.set_client(existing)

    gateway.ensure_running()

    assert client_mod.get_client() is existing
    assert existing.stopped is False


@pytest.mark.asyncio
async def test_ensure_running_replaces_only_finished_owned_generation(monkeypatch):
    """A share created in the task-done callback gap still gets a fresh tunnel."""
    monkeypatch.setattr(client_mod, "GatewayClient", _FakeClient)
    settings.gateway_token = "tok-fresh"
    settings.gateway_instance_id = "iid-1"
    existing = _FakeClient("tok-old", "iid-1", "wss://relay.test")
    client_mod.set_client(existing)

    async def _done():
        return None

    finished = asyncio.create_task(_done())
    await finished
    gateway._run_task = finished

    gateway.ensure_running()

    replacement = client_mod.get_client()
    assert replacement is not existing
    assert replacement._token == "tok-fresh"
    assert gateway._run_task is not finished


@pytest.mark.asyncio
async def test_shutdown_cannot_clear_a_successor_generation():
    """A teardown that blocks in close cannot erase a successor installed meanwhile."""
    close_started = asyncio.Event()
    close_release = asyncio.Event()

    class _BlockingClient:
        def __init__(self):
            self.retired = False

        def retire(self):
            self.retired = True

        async def close(self):
            close_started.set()
            await close_release.wait()

    old = _BlockingClient()
    client_mod.set_client(old)
    old_run = asyncio.create_task(asyncio.Event().wait())
    gateway._run_task = old_run
    gw_state.set_session_token("old-session")

    shutting_down = asyncio.create_task(gateway.shutdown())
    await asyncio.wait_for(close_started.wait(), timeout=1)
    assert old.retired is True
    assert client_mod.get_client() is None
    assert gateway._run_task is None

    successor = _BlockingClient()
    successor_run = asyncio.create_task(asyncio.Event().wait())
    client_mod.set_client(successor)
    gateway._run_task = successor_run
    gw_state.set_session_token("new-session")

    close_release.set()
    await asyncio.wait_for(shutting_down, timeout=1)

    assert client_mod.get_client() is successor
    assert gateway._run_task is successor_run
    assert gw_state.get_session_token() == "new-session"
    assert not successor_run.cancelled()
    successor_run.cancel()


def test_gateway_client_has_one_production_construction_site():
    """All production construction routes through the package lifecycle owner."""
    root = Path(__file__).resolve().parents[2] / "celerp"
    hits = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "GatewayClient"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "GatewayClient"
            )
            for node in ast.walk(tree)
        ):
            hits.append(path.relative_to(root).as_posix())
    assert hits == ["gateway/__init__.py"]