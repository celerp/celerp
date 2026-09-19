# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for the shared relay HTTP client helper: a short connect deadline
and a bounded reopen-on-connect-failure loop, with no retry once a request has
been sent."""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest

from celerp.gateway import state as gw_state


class _Client:
    """Stands in for httpx.AsyncClient: records each construction and hands the
    op a client whose post() pops the next scripted outcome."""

    made: list["_Client"] = []
    script: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts = 0
        _Client.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posts += 1
        outcome = _Client.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_client(monkeypatch):
    _Client.made = []
    _Client.script = []
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return _Client


async def _post(client):
    return await client.post("https://relay.test/x", json={})


def test_relay_timeout_connect_deadline_is_shorter_than_the_leg():
    timeout = gw_state.relay_timeout(8.0)
    assert timeout.connect == gw_state.RELAY_CONNECT_TIMEOUT_S
    assert timeout.connect < 8.0
    assert timeout.read == timeout.write == timeout.pool == 8.0


@pytest.mark.asyncio
async def test_connect_failure_reopens_a_fresh_client(fake_client):
    fake_client.script[:] = [httpx.ConnectTimeout("lost"), httpx.ConnectError("refused"), "response"]
    assert await gw_state.with_relay_client(8.0, _post) == "response"
    assert len(fake_client.made) == 3
    assert all(c.posts == 1 for c in fake_client.made)
    short = gw_state.RELAY_CONNECT_TIMEOUT_S
    # Every attempt but the last connects under the short deadline; the last
    # one gets the rest of the leg, so the connect phase never exceeds the leg.
    connects = [c.kwargs["timeout"].connect for c in fake_client.made]
    assert connects == [short, short, 8.0 - 2 * short]
    assert sum(connects) <= 8.0
    reads = [c.kwargs["timeout"].read for c in fake_client.made]
    assert all(0 < value <= 8.0 for value in reads)


def test_last_attempt_connect_deadline_is_never_shorter_than_the_short_one():
    last = gw_state.RELAY_CONNECT_ATTEMPTS
    assert gw_state.relay_connect_deadline(4.0, last) == gw_state.RELAY_CONNECT_TIMEOUT_S
    assert gw_state.relay_connect_deadline(10.0, last) == 10.0 - 2 * gw_state.RELAY_CONNECT_TIMEOUT_S
    assert gw_state.relay_connect_deadline(10.0, 1) == gw_state.RELAY_CONNECT_TIMEOUT_S


@pytest.mark.asyncio
async def test_last_connect_failure_is_raised_after_the_attempt_budget(fake_client):
    fake_client.script[:] = [httpx.ConnectTimeout("lost")] * gw_state.RELAY_CONNECT_ATTEMPTS
    with pytest.raises(httpx.ConnectTimeout):
        await gw_state.with_relay_client(8.0, _post)
    assert len(fake_client.made) == gw_state.RELAY_CONNECT_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("stalled"), httpx.RemoteProtocolError("cut"), ValueError("op bug")])
async def test_failures_after_the_request_is_sent_are_not_retried(fake_client, exc):
    fake_client.script[:] = [exc, "never reached"]
    with pytest.raises(type(exc)):
        await gw_state.with_relay_client(8.0, _post)
    assert len(fake_client.made) == 1


@pytest.mark.asyncio
async def test_relay_post_with_retry_reopens_inside_each_attempt(fake_client, monkeypatch):
    """The delayed retry loop keeps its shape; each of its attempts gets the
    connect reopen budget, and the first response of any status is final."""
    import asyncio

    async def _no_sleep(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    final = httpx.Response(401)
    fake_client.script[:] = [httpx.ConnectTimeout("lost"), httpx.ConnectTimeout("lost"), final]
    resp = await gw_state.relay_post_with_retry("https://relay.test/x", {})
    assert resp is final
    assert len(fake_client.made) == 3


@pytest.mark.asyncio
async def test_relay_post_with_retry_returns_none_when_every_attempt_fails(fake_client, monkeypatch):
    import asyncio

    async def _no_sleep(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    total = len(gw_state._RELAY_POST_RETRY_DELAYS) * gw_state.RELAY_CONNECT_ATTEMPTS
    fake_client.script[:] = [httpx.ConnectError("down")] * total
    assert await gw_state.relay_post_with_retry("https://relay.test/x", {}) is None
    assert len(fake_client.made) == total


@pytest.mark.asyncio
async def test_relay_client_enforces_one_wall_clock_budget_across_retries(monkeypatch):
    """Two failed connects plus a slow final response cannot exceed total_s.
    This protects the 8s relay legs from outliving their 10s local-UI caller."""
    import asyncio
    import time

    class _SlowClient:
        calls = 0

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            type(self).calls += 1
            if type(self).calls <= 2:
                await asyncio.sleep(0.03)
                raise httpx.ConnectTimeout("lost")
            await asyncio.sleep(1.0)
            return "too late"

    monkeypatch.setattr(httpx, "AsyncClient", _SlowClient)
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        await gw_state.with_relay_client(0.12, _post)
    elapsed = time.monotonic() - started

    assert _SlowClient.calls == 3
    assert elapsed < 0.35
