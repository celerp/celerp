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
import contextlib
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import celerp.routers.events as events_mod
from celerp.models.accounting import UserCompany
from celerp.models.auth import UserAuthState
from celerp.models.company import Company, User
from celerp.notifications.sse import _subscribers, publish, shutdown_all
from celerp.services import runtime_state
from celerp.services.auth import create_access_token
from celerp.services.session_tracker import _nonce_cache_set


async def _bearer(session, snonce: str = "stream-nonce") -> tuple[str, uuid.UUID, uuid.UUID, str]:
    """Seed a real active user + company + membership and return a valid v2 bearer.

    The stream now fully validates the token up front through
    ``validate_access_token``, which loads the user, an active ``UserCompany``
    membership and the company from the DB and checks per-user nonce equality.
    So a synthetic token with no DB rows would 401 before opening: the helper
    persists (and commits, so the route's own short-lived session sees them) an
    active user, an active company, an active admin membership, and a
    ``UserAuthState`` whose nonce matches the token's ``snonce``. The in-process
    nonce cache is primed to the same value so the live-stream poll finds no
    mismatch. Tests that force an eviction rotate BOTH the DB nonce and the cache
    to a different value after calling this.

    Returns ``(token, company_id, user_id, user_id_str)``.
    """
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session.add(Company(id=company_id, name="Stream Co", slug=f"stream-{company_id}"))
    session.add(User(id=user_id, email=f"stream-{user_id}@test.local", name="Stream User"))
    # Flush the parent rows before the membership and auth state so their foreign
    # keys resolve against committed users/companies rows.
    await session.flush()
    session.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
    session.add(UserAuthState(user_id=user_id, nonce=snonce))
    await session.commit()
    token, _ = create_access_token(
        subject=str(user_id), company_id=str(company_id), role="admin", snonce=snonce
    )
    _nonce_cache_set(str(user_id), snonce)
    return token, company_id, user_id, str(user_id)


async def _rotate_nonce(session, user_id: uuid.UUID, new_nonce: str) -> None:
    """Rotate a seeded user's server nonce in the DB and the cache, revoking the
    token that was minted against the old nonce. Mirrors what invalidate_sessions
    does elsewhere (logout, force-login, security-sensitive account changes)."""
    row = await session.get(UserAuthState, user_id)
    row.nonce = new_nonce
    await session.commit()
    _nonce_cache_set(str(user_id), new_nonce)


@pytest_asyncio.fixture(autouse=True)
async def _route_session_on_test_transaction(session, monkeypatch):
    """Route the stream's own short-lived DB sessions into the test transaction.

    The route validates the token up front and polls the nonce through
    ``celerp.db.SessionLocal`` (a fresh session on its own connection). The test
    seeds the user/company/membership through the ``session`` fixture, whose outer
    transaction is never committed to disk, so a separate connection cannot see
    the seed and validation would 401. Binding ``SessionLocal`` to a
    savepoint-joined factory on the shared connection - the same technique the
    ``client`` fixture uses for middleware - routes every route session into the
    one transaction, so it sees the seed and its own commits stay inside the
    rolled-back transaction. Applies to the direct-drive tests only, which is
    every test in this module.
    """
    import celerp.db as _db

    factory = async_sessionmaker(
        bind=await session.connection(), class_=AsyncSession,
        expire_on_commit=False, join_transaction_mode="create_savepoint")
    monkeypatch.setattr(_db, "SessionLocal", factory)
    yield


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

    validate_access_token raises 401 for a bad token, so the route never
    registers a subscriber queue or opens the stream.
    """
    before = len(_subscribers)
    with pytest.raises(HTTPException) as exc:
        await events_mod.events_stream(token="not-a-real-token")
    assert exc.value.status_code == 401
    assert len(_subscribers) == before


@pytest.mark.asyncio
async def test_events_stream_rejects_pre_v2_refresh_and_missing_nonce_at_creation():
    """A pre-v2 token, a refresh token, and a missing-nonce access token are each
    rejected 401 before any subscription: validate_access_token runs the strict
    v2 access-token contract, so none of them decodes to usable claims."""
    from jose import jwt as _jwt
    from celerp.config import settings
    from celerp.services.auth import create_refresh_token

    uid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    # Pre-v2 shape: no auth_ver/type/snonce.
    pre_v2 = _jwt.encode(
        {"sub": uid, "company_id": cid, "role": "admin", "jti": uid, "exp": 9999999999},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    # A real v2 refresh token (type=refresh) presented as a stream bearer.
    refresh = create_refresh_token(uid, cid, "admin", snonce="n")
    # A v2 access token with an empty snonce.
    missing_nonce, _ = create_access_token(subject=uid, company_id=cid, role="admin", snonce="")

    before = len(_subscribers)
    for bad in (pre_v2, refresh, missing_nonce):
        with pytest.raises(HTTPException) as exc:
            await events_mod.events_stream(token=bad)
        assert exc.value.status_code == 401
    assert len(_subscribers) == before


@pytest.mark.asyncio
async def test_events_stream_rejects_revoked_token_before_stream_creation(session, monkeypatch):
    """A valid v2 access token whose nonce has since been rotated (revoked) must be
    rejected before the stream is created, not merely evicted on a later poll tick.

    The route validates fully up front (validate_access_token), so a revoked token
    yields 401 with no subscriber ever registered."""
    token, company_id, user_id, user_id_str = await _bearer(session, snonce="client-nonce")
    # Rotate the server nonce (DB + cache) so the token is now revoked.
    await _rotate_nonce(session, user_id, "rotated-different-nonce")

    before = len(_subscribers)
    with pytest.raises(HTTPException) as exc:
        await events_mod.events_stream(token=token)
    assert exc.value.status_code == 401
    assert len(_subscribers) == before, "a revoked token must not open a subscription"


@pytest.mark.asyncio
async def test_events_stream_delivers_notification(session):
    """A notification published to the user arrives as an SSE notification event."""
    token, company_id, user_id, _ = await _bearer(session)
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
    token, company_id, user_id, user_id_str = await _bearer(session, snonce="client-nonce")
    _subscribers.pop(f"{company_id}:{user_id}", None)

    # The token validates and the stream opens; the server nonce is then rotated
    # (a forced login elsewhere), so the live poll finds the mismatch and evicts.
    resp = await events_mod.events_stream(token=token)
    await _rotate_nonce(session, user_id, "server-nonce")
    agen = resp.body_iterator
    try:
        chunk = await _next_event(agen)
    finally:
        await agen.aclose()

    assert chunk.startswith("event: evicted")


@pytest.mark.asyncio
async def test_events_stream_emits_drain_when_draining(session, monkeypatch):
    """While the system is draining, the stream emits a drain event and closes so the
    client can reconnect elsewhere during a deploy."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.02)
    # A validated v2 stream (nonce matches, no eviction) must still surface drain on
    # its poll tick. The poll checks drain on every tick, not only on a nonce cache
    # miss, so a matching nonce does not suppress it.
    token, company_id, user_id, _ = await _bearer(session)
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


def _start_flood(company_id: uuid.UUID, user_id: uuid.UUID, stop: asyncio.Event) -> asyncio.Task:
    """Publish notifications to the user as fast as the loop allows until stop is set,
    keeping the subscriber queue continuously non-empty."""

    async def _flood() -> None:
        while not stop.is_set():
            await publish(company_id, user_id, {"kind": "noise"})
            await asyncio.sleep(0)

    return asyncio.create_task(_flood())


async def _drain_until(agen, prefix: str, kick: asyncio.Task, deadline_s: float = 4.0) -> bool:
    """Read stream chunks (beginning with the already-scheduled kick read) until one
    starts with prefix or a wall-clock deadline passes. Returns whether it was seen.

    The bound is wall-clock, not a chunk count: the poll fires on an absolute time
    deadline, so the proof is that eviction/drain arrives within a bounded window of
    continuous flooding, never that it arrives within N reads (a tight read loop can
    drain thousands of notifications in well under one poll interval)."""
    loop = asyncio.get_event_loop()
    stop_at = loop.time() + deadline_s
    chunk = await asyncio.wait_for(kick, timeout=2.0)
    if chunk.startswith(prefix):
        return True
    while loop.time() < stop_at:
        chunk = await _next_event(agen, timeout=2.0)
        if chunk.startswith(prefix):
            return True
    return False


@pytest.mark.asyncio
async def test_events_stream_evicts_under_notification_flood(session, monkeypatch):
    """A forced-login eviction must reach an already-open tab even while notifications
    stream in faster than the security tick. The nonce poll runs on an absolute
    deadline, so a steady notification flow cannot starve it."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.05)
    token, company_id, user_id, user_id_str = await _bearer(session, snonce="client-nonce")
    _subscribers.pop(f"{company_id}:{user_id}", None)

    # The token validates and the stream opens; the server nonce is then rotated
    # so the eviction condition becomes true under the flood.
    resp = await events_mod.events_stream(token=token)
    await _rotate_nonce(session, user_id, "server-nonce")
    agen = resp.body_iterator
    kick = asyncio.create_task(agen.__anext__())
    stop = asyncio.Event()
    try:
        await _await_subscription(company_id, user_id)
        for _ in range(20):
            await publish(company_id, user_id, {"kind": "noise"})
        flood = _start_flood(company_id, user_id, stop)
        try:
            assert await _drain_until(agen, "event: evicted", kick), \
                "eviction starved by a steady notification stream"
        finally:
            stop.set()
            flood.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flood
    finally:
        await agen.aclose()


@pytest.mark.asyncio
async def test_events_stream_drains_under_notification_flood(session, monkeypatch):
    """Drain must reach an open tab under a notification flood too, so a deploy can move
    streaming clients rather than being starved by their own event traffic."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.05)
    # Validated stream (nonce matches); drain must reach it under a flood because the
    # drain check runs on every poll tick, not only the cache-miss path.
    token, company_id, user_id, _ = await _bearer(session)
    _subscribers.pop(f"{company_id}:{user_id}", None)
    runtime_state._drain_cache_set({"draining": True})

    resp = await events_mod.events_stream(token=token)
    agen = resp.body_iterator
    kick = asyncio.create_task(agen.__anext__())
    stop = asyncio.Event()
    try:
        await _await_subscription(company_id, user_id)
        for _ in range(20):
            await publish(company_id, user_id, {"kind": "noise"})
        flood = _start_flood(company_id, user_id, stop)
        try:
            assert await _drain_until(agen, "event: drain", kick), \
                "drain starved by a steady notification stream"
        finally:
            stop.set()
            flood.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flood
    finally:
        await agen.aclose()


@pytest.mark.asyncio
async def test_events_stream_unsubscribes_on_disconnect(session, monkeypatch):
    """When the client disconnects, the stream's teardown removes its subscriber queue
    so a churn of reconnecting clients does not leak queues."""
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.02)
    # Not draining: the tick path loops and emits a keepalive, proving the stream is
    # subscribed and live before we disconnect it.
    runtime_state._drain_cache_set({"draining": False})
    token, company_id, user_id, _ = await _bearer(session)
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
async def test_events_stream_closes_on_shutdown_sentinel(session):
    """The None shutdown sentinel closes the stream cleanly without emitting an event,
    and the subscriber is removed - this is the lifespan-shutdown path."""
    token, company_id, user_id, _ = await _bearer(session)
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


@pytest.mark.asyncio
async def test_events_stream_releases_subscription_on_normal_completion(session, monkeypatch):
    """A stream that ends by returning from its loop (normal completion, not a client
    disconnect) releases its subscriber queue through the same teardown.

    Green at merge-base by design: subscribe/unsubscribe and the stream's
    finally-clause unsubscribe are unchanged on this branch, so this characterizes
    pre-existing handling rather than a behavior change - it claims no red-first
    evidence. It complements test_events_stream_unsubscribes_on_disconnect, which
    exercises the GeneratorExit teardown via aclose(); here the generator instead
    reaches a return on the evicted path and runs on to StopAsyncIteration, proving the
    normal-completion control path also releases the queue. A leak here would exhaust
    MAX_SUBSCRIBERS_PER_USER and start evicting live sessions.
    """
    monkeypatch.setattr(events_mod, "_STREAM_TICK_SECONDS", 0.02)
    token, company_id, user_id, user_id_str = await _bearer(session, snonce="client-nonce")
    key = f"{company_id}:{user_id}"
    _subscribers.pop(key, None)

    # The token validates and the stream opens; the server nonce is then rotated so the
    # stream returns on the evicted path on its next tick, exercising normal completion.
    resp = await events_mod.events_stream(token=token)
    await _rotate_nonce(session, user_id, "server-nonce")
    agen = resp.body_iterator

    chunk = await _next_event(agen)
    assert chunk.startswith("event: evicted")
    # The queue is still registered here: the generator is suspended right after the
    # yield, before it executes the return that follows.
    assert _subscribers.get(key)

    # Advancing past the yield runs the return, its finally clause, and raises
    # StopAsyncIteration - WITHOUT aclose(). That is the normal-completion release path
    # under test.
    task = asyncio.create_task(agen.__anext__())
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(task, timeout=5.0)

    assert not _subscribers.get(key)
