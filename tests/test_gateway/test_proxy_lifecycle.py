# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Proxied-request lifecycle on the gateway client: cancel, deadline, generation
fencing, and per-request client isolation over one shared bounded transport.

A relayed request is fire-and-forget with a fixed 180s cap, no way to abort an
in-flight proxy task, and no way to drop a response that arrived after the
connection had already been rebuilt. Each request also gets its own httpx client
wrapper so no cookie state carries between visitors, while the underlying
connection pool is shared and bounded. These tests pin that contract:

  * `http.cancel` cancels the keyed in-flight proxy task for that request id.
  * `http.request` carrying `timeout_ms` cancels past that deadline and emits an
    error response rather than hanging to the fixed cap.
  * a response tagged with a prior connection generation is ignored after a
    reconnect, so a slow response from a dead socket can't clobber the live one.
  * each proxied request constructs its own AsyncClient, but they all drive the
    one shared bounded transport, whose per-request __aexit__ never tears the
    pool down; the transport is closed exactly once on shutdown.
"""

from __future__ import annotations

import asyncio

import pytest

from celerp.gateway.client import GatewayClient


@pytest.fixture
def client():
    return GatewayClient(
        gateway_token="test-gateway-token",
        instance_id="test-instance-id",
        gateway_url="wss://relay.celerp.com/ws/connect",
    )


def _keyed_tasks(client) -> dict:
    """The in-flight proxy tasks keyed by request id, whatever attribute holds them."""
    for name in ("_inflight", "_proxy_tasks", "_inflight_tasks", "_requests"):
        val = getattr(client, name, None)
        if isinstance(val, dict):
            return val
    return {}


@pytest.mark.asyncio
async def test_gateway_http_cancel_aborts_inflight(client, monkeypatch):
    """An http.cancel frame cancels the keyed in-flight proxy task for its id."""
    started = asyncio.Event()

    async def _never_finishes(payload):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(client, "_handle_proxy_request", _never_finishes)

    async def _noop_send(ws, msg):
        pass
    monkeypatch.setattr(client.__class__, "_send", staticmethod(_noop_send))
    client._ws = object()

    await client._dispatch({"type": "http.request",
                            "payload": {"id": "req-1", "method": "GET", "path": "/x"}})
    await asyncio.wait_for(started.wait(), timeout=1)

    keyed = _keyed_tasks(client)
    assert "req-1" in keyed, "an in-flight proxy task must be keyed by its request id"
    task = keyed["req-1"]

    await client._dispatch({"type": "http.cancel", "payload": {"id": "req-1"}})
    await asyncio.sleep(0)
    assert task.cancelled() or task.done(), "http.cancel must cancel the keyed in-flight task"


@pytest.mark.asyncio
async def test_gateway_timeout_ms_deadline(client, monkeypatch):
    """A proxied request carrying timeout_ms is cancelled past that deadline and an
    error response is emitted, rather than hanging to the fixed 180s cap."""
    import httpx

    class _HangingClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, *a, **k):
            await asyncio.sleep(3600)  # never returns within the deadline

    monkeypatch.setattr(httpx, "AsyncClient", _HangingClient)

    sent = []

    async def _capture_send(ws, msg):
        sent.append(msg)
    monkeypatch.setattr(client.__class__, "_send", staticmethod(_capture_send))
    client._ws = object()

    await asyncio.wait_for(
        client._handle_proxy_request({
            "id": "req-timeout", "method": "GET", "path": "/slow",
            "query": "", "headers": {}, "body_b64": "", "timeout_ms": 50,
        }),
        timeout=5,
    )

    assert sent, "a request past its deadline must emit a response, not hang silently"
    payload = sent[-1]["payload"]
    assert payload["id"] == "req-timeout"
    assert payload["status"] >= 500, (
        f"a deadline overrun must surface as an error status, got {payload['status']}")


@pytest.mark.asyncio
async def test_gateway_stale_generation_dropped(client, monkeypatch):
    """A response tagged with a prior connection generation is dropped after a
    reconnect: a slow response from a dead socket must not be sent on the new one."""
    import httpx

    resume = asyncio.Event()

    class _SlowClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, *a, **k):
            await resume.wait()

            class _R:
                status_code = 200
                content = b"stale"
                headers = httpx.Headers()
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _SlowClient)

    sent = []

    async def _capture_send(ws, msg):
        sent.append(msg)
    monkeypatch.setattr(client.__class__, "_send", staticmethod(_capture_send))
    client._ws = object()

    gen_before = getattr(client, "_generation", None)
    assert gen_before is not None, "the client must tag proxy work with a connection generation"

    task = asyncio.create_task(client._handle_proxy_request({
        "id": "req-stale", "method": "GET", "path": "/x",
        "query": "", "headers": {}, "body_b64": "",
    }))
    await asyncio.sleep(0.05)

    # Reconnect happens while the request is still in flight: bump the generation.
    client._generation = gen_before + 1
    resume.set()
    await asyncio.wait_for(task, timeout=5)

    stale = [m for m in sent
             if m.get("type") == "http.response" and m["payload"].get("id") == "req-stale"]
    assert not stale, "a response from a superseded connection generation must be dropped"


@pytest.mark.asyncio
async def test_gateway_shared_transport_isolates_each_request(client, monkeypatch):
    """Two proxied requests each construct their OWN AsyncClient (so no cookie
    state carries between visitors), but both drive the IDENTICAL shared bounded
    transport. A per-request client's __aexit__ must not tear the shared pool
    down, and close() must close the transport exactly once."""
    import httpx

    constructed = []
    transports = []

    class _CountingClient:
        def __init__(self, *a, **k):
            constructed.append(self)
            transports.append(k.get("transport"))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, *a, **k):
            class _R:
                status_code = 200
                content = b"ok"
                headers = httpx.Headers()
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _CountingClient)

    async def _noop_send(ws, msg):
        pass
    monkeypatch.setattr(client.__class__, "_send", staticmethod(_noop_send))
    client._ws = object()

    for i in range(2):
        await client._handle_proxy_request({
            "id": f"req-{i}", "method": "GET", "path": "/x",
            "query": "", "headers": {}, "body_b64": "",
        })

    # A fresh client per request: no shared cookie jar can survive between visitors.
    assert len(constructed) == 2, (
        f"the gateway must build one AsyncClient per request, built {len(constructed)}")

    # Both requests drove the one shared, bounded transport (identity, not equality).
    assert transports[0] is transports[1], (
        "both per-request clients must drive the identical shared transport object")
    shared = transports[0]
    assert isinstance(shared, httpx.AsyncHTTPTransport), (
        f"the shared transport must be an httpx transport, got {type(shared)}")
    assert shared is client._http_transport, (
        "the transport passed to each client must be the gateway's shared transport")

    # The shared transport carries the explicit 32/8 connection bounds. httpx
    # folds Limits into the transport's pool, so read the pool back.
    pool = shared._pool
    assert pool._max_connections == 32, (
        f"the shared transport must cap at 32 connections, got {pool._max_connections}")
    assert pool._max_keepalive_connections == 8, (
        f"the shared transport must keep 8 connections alive, got "
        f"{pool._max_keepalive_connections}")

    # A per-request client's __aexit__ must NOT close the shared pool: the no-op
    # aclose/__aexit__ on the transport is what guarantees the pool outlives it.
    closed = {"n": 0}
    orig_shutdown = shared.shutdown_pool

    async def _counting_shutdown():
        closed["n"] += 1
        await orig_shutdown()
    monkeypatch.setattr(shared, "shutdown_pool", _counting_shutdown)

    async with httpx.AsyncClient(transport=shared) as _c:
        pass
    # httpx.AsyncClient.__aexit__ calls transport.aclose(), which is a no-op here;
    # it must never route to shutdown_pool.
    assert closed["n"] == 0, (
        "a per-request client leaving its context must not shut the shared pool down")

    # close() shuts the shared transport down exactly once, and a second close()
    # is a harmless no-op (the field is nulled first).
    await client.close()
    assert closed["n"] == 1, (
        f"close() must shut the shared transport down exactly once, did {closed['n']}")
    await client.close()
    assert closed["n"] == 1, (
        f"a second close() must not shut the transport down again, did {closed['n']}")


class _AbnormalWS:
    """A fake gateway websocket whose message iteration raises a disconnect-style
    exception mid-stream, exactly as a silently-dropped socket does at runtime.

    `async with websockets.connect(...)` yields this object; `async for raw in ws`
    then enters __anext__ and raises ConnectionClosedError before any frame is read.
    """

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc


def _drive_abnormal_disconnect(client, monkeypatch):
    """Patch out the network so _connect_and_serve runs the real async-with / async-for
    path against a socket that drops abnormally on the first iteration. The teardown
    statements (line 360/363/370) are siblings of the async-with, not in a finally, so
    at HEAD the raised ConnectionClosedError skips them."""
    import websockets
    from websockets.exceptions import ConnectionClosedError

    def _fake_connect(*a, **k):
        return _AbnormalWS(ConnectionClosedError(None, None))

    monkeypatch.setattr(websockets, "connect", _fake_connect)

    # The hello handshake sends before the async-for; make it a no-op so the drive
    # reaches the iteration that raises.
    async def _noop_send(ws, msg):
        pass
    monkeypatch.setattr(client.__class__, "_send", staticmethod(_noop_send))

    from celerp import config as _cfg
    monkeypatch.setattr(_cfg, "read_config", lambda: {})


@pytest.mark.asyncio
async def test_gateway_teardown_runs_on_abnormal_disconnect(client, monkeypatch):
    """When the message stream raises a disconnect-style exception mid-serve, the
    connection teardown must still run: the generation is fenced (bumped), `_ws` is
    cleared, and status returns to inactive. At HEAD these are siblings of the
    async-with rather than in a finally, so the exception skips them and the state
    is left dangling."""
    from websockets.exceptions import ConnectionClosedError

    _drive_abnormal_disconnect(client, monkeypatch)

    gen_before = client._generation

    # The abnormal disconnect propagates out of _connect_and_serve at HEAD (no finally
    # swallows it); run()'s backoff loop is what catches it in production. Either way
    # the observable post-disconnect state must be the fenced/torn-down one.
    with pytest.raises(ConnectionClosedError):
        await client._connect_and_serve()

    assert client._generation == gen_before + 1, (
        "an abnormal disconnect must bump the connection generation so in-flight "
        f"handlers keyed to the old generation are fenced (was {gen_before}, "
        f"now {client._generation})")
    assert client._ws is None, (
        "an abnormal disconnect must clear _ws; a dangling dead socket must not survive")
    assert client._relay_status == "inactive", (
        f"an abnormal disconnect must return status to inactive, got {client._relay_status!r}")


@pytest.mark.asyncio
async def test_gateway_cancels_inflight_tasks_on_abnormal_disconnect(client, monkeypatch):
    """An in-flight proxy task keyed to the pre-disconnect generation must be cancelled
    by teardown when the socket drops abnormally: once the socket that carried it is
    dead, its response can never be delivered, so leaving it running leaks a task and
    an unfenced handler. At HEAD teardown is skipped, so the task keeps running."""
    from websockets.exceptions import ConnectionClosedError

    _drive_abnormal_disconnect(client, monkeypatch)

    started = asyncio.Event()

    async def _never_finishes():
        started.set()
        await asyncio.sleep(3600)

    inflight = asyncio.create_task(_never_finishes())
    await asyncio.wait_for(started.wait(), timeout=1)
    client._inflight["req-inflight"] = inflight

    try:
        with pytest.raises(ConnectionClosedError):
            await client._connect_and_serve()

        # Give any teardown-scheduled cancellation a turn to take effect.
        await asyncio.sleep(0)
        assert inflight.cancelled() or (inflight.done() and inflight.exception() is not None), (
            "an in-flight proxy task keyed to the dead connection must be cancelled on "
            "abnormal disconnect; at HEAD teardown is skipped so it is still running")
    finally:
        if not inflight.done():
            inflight.cancel()
            try:
                await inflight
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_gateway_send_self_terminates_on_wedged_socket(client, monkeypatch):
    """A websocket send that never drains must not hang forever. The send is bounded by a
    deadline: on a wedged socket it raises TimeoutError so the proxy task is freed and the
    socket can be rebuilt, rather than pinning a relay slot indefinitely.

    Observable: send_message returns control (raises) on its own deadline, well before the
    outer guard. At head there is no deadline, so send_message never returns and only the
    outer wait_for trips - which the elapsed-time assertion catches."""
    import time
    import celerp.gateway.client as gw

    # Shrink the deadline so the test is fast; the behaviour under test is that a deadline
    # fires, not its production seconds value. raising=False so that without a deadline the
    # send still hangs and the elapsed assertion below is what fails - the red is the hang,
    # never a missing symbol.
    monkeypatch.setattr(gw, "_SEND_DEADLINE", 0.1, raising=False)

    entered = asyncio.Event()

    class _WedgedWS:
        async def send(self, _data):
            entered.set()
            await asyncio.Event().wait()  # the frame never drains

    client._ws = _WedgedWS()

    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.send_message("hello_ack"), timeout=5)
    elapsed = time.monotonic() - t0

    assert entered.is_set(), "the send must have been attempted"
    assert elapsed < 2.0, (
        f"a wedged send must self-terminate on its own ~0.1s deadline, not hang to the "
        f"outer 5s guard; took {elapsed:.2f}s")
