# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The app end of the chunked tunnel (the relay's wire contract, whose reference app
end is TunnelApp in the cloud's tests/test_tunnel_chunked.py).

The hello asks for the chunked tunnel; a hello_ack that grants it carries the relay's
limits, kept on the gateway state for this connection only. A proxied request marked
stream=1 on such a connection answers a body above chunk_bytes as response_start,
response_chunk frames and response_end, holding at most `window` unacknowledged chunks
across every stream on the connection, and reading the local body only as credit
allows. HEAD answers with its start and an end of no chunks. A request body the relay sends in chunks is
put back together before it reaches the local app.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

import celerp.gateway.state as gw_state
from celerp.gateway.client import GatewayClient

LIMITS = {"chunk_bytes": 1024, "frame_max": 4096, "window": 3,
          "media_max_bytes": 64 * 1024, "media_streams": 4}


@pytest.fixture
def client(monkeypatch):
    c = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.example/ws/connect")
    c.sent = []
    c.frame_sizes = []

    async def _send(ws, msg):
        c.frame_sizes.append(len(json.dumps(msg)))
        c.sent.append(msg)
    monkeypatch.setattr(GatewayClient, "_send", staticmethod(_send))
    c._ws = object()
    yield c
    gw_state.set_tunnel_limits(None)


class _Local:
    """The local app behind the gateway: answers from a handler and counts how many
    body bytes the gateway has pulled from each response."""

    def __init__(self, monkeypatch, handler):
        self.requests: list[httpx.Request] = []
        self.pulled = 0
        local = self

        def _transport_handler(request: httpx.Request):
            local.requests.append(request)
            return handler(request, local)

        class _Client(httpx.AsyncClient):
            def __init__(self, *a, **k):
                k["transport"] = httpx.MockTransport(_transport_handler)
                super().__init__(*a, **k)
        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def body(self, data: bytes, piece: int = 256, fail_after: int | None = None):
        """An async body that records each piece as the gateway pulls it."""
        local = self

        async def gen():
            for i in range(0, len(data), piece):
                if fail_after is not None and i >= fail_after:
                    raise httpx.ReadError("local app went away")
                local.pulled += len(data[i:i + piece])
                yield data[i:i + piece]
        return gen()


async def _ack_hello(client, tunnel=True):
    payload = {"instance_id": "i", "session_token": "s"}
    if tunnel:
        payload["tunnel"] = {"chunked": 1, "limits": dict(LIMITS)}
    await client._dispatch({"type": "hello_ack", "payload": payload})


def _of(client, rid, kind=None):
    return [m for m in client.sent if m["payload"].get("id") == rid and (kind is None or m["type"] == kind)]


async def _settle(cond, timeout=5.0):
    async def _wait():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(_wait(), timeout)


def _request(rid, path="/media/x", method="GET", stream=True, **extra):
    p = {"id": rid, "method": method, "path": path, "query": "", "headers": {}, "body_b64": "", **extra}
    if stream:
        p["stream"] = 1
    return {"type": "http.request", "payload": p}


async def _ack_all(client, rid):
    chunks = _of(client, rid, "http.response_chunk")
    if chunks:
        await client._dispatch({"type": "http.stream_ack",
                                "payload": {"id": rid, "seq": chunks[-1]["payload"]["seq"]}})


def test_hello_asks_for_the_chunked_tunnel(client):
    assert client._build_hello_payload("v1", "1.0.0")["tunnel"] == {"chunked": 1}


@pytest.mark.asyncio
async def test_hello_ack_limits_are_kept_for_this_connection_only(client):
    await _ack_hello(client)
    assert gw_state.get_tunnel_limits()["media_max_bytes"] == LIMITS["media_max_bytes"]
    # A reconnect whose hello_ack grants no chunked tunnel leaves no limit behind.
    await _ack_hello(client, tunnel=False)
    assert gw_state.get_tunnel_limits() == {}


@pytest.mark.asyncio
async def test_a_large_body_streams_in_chunks_under_the_frame_limit(client, monkeypatch):
    image = bytes(range(256)) * 20  # 5120 bytes: five chunks of 1024
    local = _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-type": "image/jpeg", "content-length": str(len(image))},
        content=app.body(image)))
    await _ack_hello(client)
    client.sent.clear()
    client.frame_sizes.clear()
    await client._dispatch(_request("r1"))
    while not _of(client, "r1", "http.response_end"):
        await _settle(lambda: len(_of(client, "r1", "http.response_chunk")) >= 1)
        await asyncio.sleep(0.02)
        await _ack_all(client, "r1")
    start = _of(client, "r1", "http.response_start")[0]["payload"]
    assert start["status"] == 200 and start["length"] == len(image)
    assert ["content-type", "image/jpeg"] in start["headers"]
    chunks = [m["payload"] for m in _of(client, "r1", "http.response_chunk")]
    assert [c["seq"] for c in chunks] == list(range(5))
    assert b"".join(base64.b64decode(c["b64"]) for c in chunks) == image
    assert _of(client, "r1", "http.response_end")[0]["payload"]["chunks"] == 5
    assert not _of(client, "r1", "http.response")
    assert max(client.frame_sizes) < LIMITS["frame_max"]
    assert local.pulled == len(image)


@pytest.mark.asyncio
async def test_unacknowledged_chunks_never_pass_the_window_and_the_body_is_read_only_as_credit_allows(
        client, monkeypatch):
    image = b"x" * (LIMITS["chunk_bytes"] * 10)
    local = _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-length": str(len(image))}, content=app.body(image)))
    await _ack_hello(client)
    await client._dispatch(_request("r1"))
    await _settle(lambda: len(_of(client, "r1", "http.response_chunk")) == LIMITS["window"])
    await asyncio.sleep(0.1)
    assert len(_of(client, "r1", "http.response_chunk")) == LIMITS["window"]
    # Memory bound: the gateway has pulled at most the window plus one chunk of the body.
    assert local.pulled <= (LIMITS["window"] + 1) * LIMITS["chunk_bytes"]
    await client._dispatch({"type": "http.stream_ack", "payload": {"id": "r1", "seq": 0}})
    await _settle(lambda: len(_of(client, "r1", "http.response_chunk")) == LIMITS["window"] + 1)
    await asyncio.sleep(0.05)
    assert len(_of(client, "r1", "http.response_chunk")) == LIMITS["window"] + 1


@pytest.mark.asyncio
async def test_concurrent_streams_share_one_window_and_an_unrelated_request_still_answers(
        client, monkeypatch):
    image = b"y" * (LIMITS["chunk_bytes"] * 4)

    def handler(req, app):
        if req.url.path == "/small":
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")
        return httpx.Response(200, headers={"content-length": str(len(image))}, content=app.body(image))
    _Local(monkeypatch, handler)
    await _ack_hello(client)
    await client._dispatch(_request("a"))
    await client._dispatch(_request("b"))
    chunks = lambda: [m for m in client.sent if m["type"] == "http.response_chunk"]  # noqa: E731
    await _settle(lambda: len(chunks()) == LIMITS["window"])
    await asyncio.sleep(0.1)
    assert len(chunks()) == LIMITS["window"], "two streams together hold one window"
    # The window is full; an ordinary request on the same connection is not held by it.
    await client._dispatch(_request("s", path="/small"))
    await _settle(lambda: bool(_of(client, "s", "http.response")))
    assert base64.b64decode(_of(client, "s", "http.response")[0]["payload"]["body_b64"]) == b"{}"
    for _ in range(40):
        if _of(client, "a", "http.response_end") and _of(client, "b", "http.response_end"):
            break
        await _ack_all(client, "a")
        await _ack_all(client, "b")
        await asyncio.sleep(0.02)
    for rid in ("a", "b"):
        body = b"".join(base64.b64decode(m["payload"]["b64"]) for m in _of(client, rid, "http.response_chunk"))
        assert body == image


@pytest.mark.asyncio
async def test_a_small_body_and_a_legacy_tunnel_stay_one_response(client, monkeypatch):
    image = b"z" * (LIMITS["chunk_bytes"] * 3)
    _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-length": str(len(image) if req.url.path == "/big" else 2)},
        content=image if req.url.path == "/big" else b"ok"))
    await _ack_hello(client)
    await client._dispatch(_request("small", path="/ok"))
    await _settle(lambda: bool(_of(client, "small", "http.response")))
    # A request the relay did not mark for streaming is answered whole.
    await client._dispatch(_request("plain", path="/big", stream=False))
    await _settle(lambda: bool(_of(client, "plain", "http.response")))
    # And on a connection whose hello_ack granted no chunked tunnel.
    await _ack_hello(client, tunnel=False)
    await client._dispatch(_request("legacy", path="/big"))
    await _settle(lambda: bool(_of(client, "legacy", "http.response")))
    assert base64.b64decode(_of(client, "legacy", "http.response")[0]["payload"]["body_b64"]) == image
    assert not [m for m in client.sent if m["type"].startswith("http.response_")]


@pytest.mark.asyncio
async def test_a_large_body_without_a_declared_length_still_streams_in_chunks(client, monkeypatch):
    image = b"q" * (LIMITS["chunk_bytes"] * 2 + 7)
    _Local(monkeypatch, lambda req, app: httpx.Response(200, content=app.body(image)))
    await _ack_hello(client)
    await client._dispatch(_request("r"))
    for _ in range(40):
        if _of(client, "r", "http.response_end"):
            break
        await _ack_all(client, "r")
        await asyncio.sleep(0.02)
    assert _of(client, "r", "http.response_start")[0]["payload"]["length"] == len(image)
    body = b"".join(base64.b64decode(m["payload"]["b64"]) for m in _of(client, "r", "http.response_chunk"))
    assert body == image and max(client.frame_sizes) < LIMITS["frame_max"]


@pytest.mark.asyncio
async def test_head_answers_with_its_start_and_no_chunks_and_never_reads_the_body(client, monkeypatch):
    size = LIMITS["chunk_bytes"] * 5
    local = _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-type": "image/png", "content-length": str(size)}, content=app.body(b"h" * size)))
    await _ack_hello(client)
    await client._dispatch(_request("h", method="HEAD"))
    await _settle(lambda: bool(_of(client, "h")))
    await asyncio.sleep(0.05)
    frames = _of(client, "h")
    assert [m["type"] for m in frames] == ["http.response_start", "http.response_end"]
    assert frames[0]["payload"]["length"] == size and frames[1]["payload"]["chunks"] == 0
    assert local.requests[0].method == "HEAD" and local.pulled == 0


@pytest.mark.asyncio
async def test_a_local_read_failure_mid_body_aborts_the_stream(client, monkeypatch):
    image = b"f" * (LIMITS["chunk_bytes"] * 3)
    _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-length": str(len(image))},
        content=app.body(image, fail_after=LIMITS["chunk_bytes"])))
    await _ack_hello(client)
    await client._dispatch(_request("r"))
    await _settle(lambda: bool(_of(client, "r", "http.response_abort")))
    assert not _of(client, "r", "http.response_end")


@pytest.mark.asyncio
async def test_a_cancelled_stream_gives_its_credit_back(client, monkeypatch):
    image = b"c" * (LIMITS["chunk_bytes"] * 8)
    _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-length": str(len(image))}, content=app.body(image)))
    await _ack_hello(client)
    await client._dispatch(_request("one"))
    await _settle(lambda: len(_of(client, "one", "http.response_chunk")) == LIMITS["window"])
    await client._dispatch({"type": "http.cancel", "payload": {"id": "one"}})
    await client._dispatch(_request("two"))
    await _settle(lambda: len(_of(client, "two", "http.response_chunk")) == LIMITS["window"])


@pytest.mark.asyncio
async def test_a_finished_stream_the_relay_abandons_gives_its_credit_back(client, monkeypatch):
    image = b"d" * (LIMITS["chunk_bytes"] * 2)
    _Local(monkeypatch, lambda req, app: httpx.Response(
        200, headers={"content-length": str(len(image))}, content=app.body(image)))
    await _ack_hello(client)
    await client._dispatch(_request("one"))
    await _settle(lambda: bool(_of(client, "one", "http.response_end")))
    # The store left before reading: no ack comes, the relay's cancel does.
    await client._dispatch({"type": "http.cancel", "payload": {"id": "one"}})
    await client._dispatch(_request("two"))
    await _settle(lambda: bool(_of(client, "two", "http.response_end")))


@pytest.mark.asyncio
async def test_a_chunked_request_body_reaches_the_local_app_whole(client, monkeypatch):
    local = _Local(monkeypatch, lambda req, app: httpx.Response(200, content=b"ok"))
    await _ack_hello(client)
    body = bytes(range(256)) * 9
    start = {"id": "u", "method": "POST", "path": "/upload", "query": "", "headers": {},
             "stream": 1, "length": len(body)}
    await client._dispatch({"type": "http.request_start", "payload": start})
    for seq, i in enumerate(range(0, len(body), 1000)):
        await client._dispatch({"type": "http.request_chunk", "payload": {
            "id": "u", "seq": seq, "b64": base64.b64encode(body[i:i + 1000]).decode()}})
    await client._dispatch({"type": "http.request_end", "payload": {"id": "u", "chunks": 3}})
    await _settle(lambda: bool(_of(client, "u", "http.response")))
    assert local.requests[0].content == body


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["gap", "short", "count"])
async def test_a_broken_chunked_request_body_is_refused_and_never_forwarded(client, monkeypatch, fault):
    local = _Local(monkeypatch, lambda req, app: httpx.Response(200, content=b"ok"))
    await _ack_hello(client)
    body = b"b" * 2000
    start = {"id": "u", "method": "POST", "path": "/upload", "query": "", "headers": {},
             "stream": 1, "length": len(body) + (1 if fault == "short" else 0)}
    await client._dispatch({"type": "http.request_start", "payload": start})
    seqs = [0, 2] if fault == "gap" else [0, 1]
    for seq, piece in zip(seqs, (body[:1000], body[1000:])):
        await client._dispatch({"type": "http.request_chunk", "payload": {
            "id": "u", "seq": seq, "b64": base64.b64encode(piece).decode()}})
    await client._dispatch({"type": "http.request_end", "payload": {
        "id": "u", "chunks": 3 if fault == "count" else 2}})
    await _settle(lambda: bool(_of(client, "u", "http.response")))
    assert _of(client, "u", "http.response")[0]["payload"]["status"] == 400
    assert local.requests == []
