# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Muxed /events/stream API route.

The muxed SSE route carries every local real-time signal on one connection:
notification delivery, forced-session eviction (nonce mismatch), drain, and
clean subscriber teardown. These tests exercise each of those paths end to end
through the route so the single surviving local stream stays characterized.

The route's streaming generator is driven directly (via the StreamingResponse
body_iterator) rather than over an in-process ASGI client: the SSE generator
blocks on a shared asyncio.Queue and must be published to WHILE it is open, and
an in-process ASGI transport does not run the app concurrently with the test's
own publish, so a client-based read of a live notification deadlocks. Driving
the generator directly keeps every signal on the same event loop, so a publish
or a tick and the read that observes it interleave deterministically.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import celerp.routers.events as events_mod
from celerp.notifications.sse import _subscribers, publish, shutdown_all
from celerp.services import runtime_state
from celerp.services.auth import create_access_token
from celerp.services.session_tracker import _nonce_cache_set


def _bearer(snonce: str = "") -> tuple[str, uuid.UUID, uuid.UUID, str]:
    """Return (token, company_id, user_id, user_id_str) for a decodable token.

    get_token_claims only decodes the JWT (no DB), so a self-minted token with
    real UUID sub/company_id claims drives the stream without seeding a user row.
    """
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token, _ = create_access_token(
        subject=str(user_id), company_id=str(company_id), role="admin", snonce=snonce
    )
    return token, company_id, user_id, str(user_id)


async def _await_subscription(company_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Wait until the route has registered its subscriber queue."""
    key = f"{company_id}:{user_id}"
    for _ in range(400):
        if _subscribers.get(key):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("stream never subscribed")


async def _next_event(agen, timeout: float = 5.0) -> str:
    """Return the next SSE chunk the generator yields, bounded by a timeout."""
    return await asyncio.wait_for(agen.__anext__(), timeout=timeout)


@pytest.mark.asyncio
async def test_events_stream_invalid_bearer_is_401():
    """An undecodable bearer token is rejected with 401 before any subscription.

    get_token_claims returns None for a bad token, so the route raises 401 and
    never registers a subscriber queue or opens the stream.
    """
    before = len(_subscribers)
    with pytest.raises(HTTPException) as exc:
        await events_mod.events_stream(token="not-a-real-token")
    assert exc.value.status_code == 401
    assert len(_subscribers) == before


@pytest.mark.asyncio
async def test_events_stream_delivers_notification():
    """A notification published to the user arrives as an SSE notification event."""
    token, company_id, user_id, _ = _bearer()
    _subscribers.pop(f"{company_id}:{user_id}", None)

    resp = await events_mod.events_stream(token=token)
    agen = resp.body_iterator
    task = asyncio.create_task(agen.__anext__())
    try:
        await _await_subscription(company_id, user_id)
        await publish(company_id, user_id, {"kind": "stock", "id": "abc"})
        chunk = await asyncio.wait_for(task, timeout=5.0)
    finally:
        await agen.aclose()

    assert chunk.startswith("event: notification")
    assert '"kind": "stock"' in chunk
    assert '"id": "abc"' in chunk


@pytest.mark.asyncio
async def test_events_stream_emits_evicted_on_nonce_mismatch(session, monkeypatch):
    """When the cached session nonce no longer matches the token, the stream emits an
    evicted event and closes - this is how a forced login elsewhere signs the older
    session out over the one muxed connection."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.02)
    token, company_id, user_id, user_id_str = _bearer(snonce="client-nonce")
    _subscribers.pop(f"{company_id}:{user_id}", None)
    # Cached server-side nonce differs from the token's - the eviction condition.
    _nonce_cache_set(user_id_str, "server-nonce")

    resp = await events_mod.events_stream(token=token)
    agen = resp.body_iterator
    try:
        chunk = await _next_event(agen)
    finally:
        await agen.aclose()

    assert chunk.startswith("event: evicted")


@pytest.mark.asyncio
async def test_events_stream_emits_drain_when_draining(monkeypatch):
    """While the system is draining, the stream emits a drain event and closes so the
    client can reconnect elsewhere during a deploy."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.02)
    token, company_id, user_id, _ = _bearer(snonce="")  # no nonce -> drain-only tick path
    _subscribers.pop(f"{company_id}:{user_id}", None)
    # Prime the in-process drain cache so is_draining() reports True on the tick
    # without a DB row.
    runtime_state._drain_cache_set({"draining": True})

    resp = await events_mod.events_stream(token=token)
    agen = resp.body_iterator
    try:
        chunk = await _next_event(agen)
    finally:
        await agen.aclose()

    assert chunk.startswith("event: drain")


@pytest.mark.asyncio
async def test_events_stream_unsubscribes_on_disconnect(monkeypatch):
    """When the client disconnects, the stream's teardown removes its subscriber queue
    so a churn of reconnecting clients does not leak queues."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.02)
    # Not draining: the tick path loops and emits a keepalive, proving the stream is
    # subscribed and live before we disconnect it.
    runtime_state._drain_cache_set({"draining": False})
    token, company_id, user_id, _ = _bearer(snonce="")
    key = f"{company_id}:{user_id}"
    _subscribers.pop(key, None)

    resp = await events_mod.events_stream(token=token)
    agen = resp.body_iterator
    chunk = await _next_event(agen)
    assert chunk.startswith(": keepalive")
    assert _subscribers.get(key)

    # Closing the generator fires its finally clause, which unsubscribes.
    await agen.aclose()
    for _ in range(400):
        if not _subscribers.get(key):
            break
        await asyncio.sleep(0.005)
    assert not _subscribers.get(key)


@pytest.mark.asyncio
async def test_events_stream_closes_on_shutdown_sentinel():
    """The None shutdown sentinel closes the stream cleanly without emitting an event,
    and the subscriber is removed - this is the lifespan-shutdown path."""
    token, company_id, user_id, _ = _bearer()
    key = f"{company_id}:{user_id}"
    _subscribers.pop(key, None)

    resp = await events_mod.events_stream(token=token)
    agen = resp.body_iterator
    task = asyncio.create_task(agen.__anext__())
    await _await_subscription(company_id, user_id)

    shutdown_all()  # pushes the None sentinel to every subscriber, then clears the map
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(task, timeout=5.0)

    assert not _subscribers.get(key)
