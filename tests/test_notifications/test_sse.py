# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/notifications/sse.py - pub/sub + SSE event stream."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from celerp.notifications.sse import (
    MAX_SUBSCRIBERS_PER_USER,
    _subscribers,
    event_stream,
    publish,
    subscribe,
    unsubscribe,
)


@pytest.fixture(autouse=True)
def _clean_subscribers():
    """Clear global subscriber state between tests."""
    _subscribers.clear()
    yield
    _subscribers.clear()


_CID = uuid.uuid4()
_UID = uuid.uuid4()
_UID2 = uuid.uuid4()


# ── subscribe / unsubscribe ──────────────────────────────────────────────────

def test_subscribe_creates_queue():
    q = subscribe(_CID, _UID)
    assert q is not None
    key = f"{_CID}:{_UID}"
    assert key in _subscribers
    assert q in _subscribers[key]


def test_unsubscribe_removes_queue():
    q = subscribe(_CID, _UID)
    unsubscribe(_CID, _UID, q)
    key = f"{_CID}:{_UID}"
    assert key not in _subscribers


def test_unsubscribe_nonexistent_noop():
    q = asyncio.Queue()
    unsubscribe(_CID, _UID, q)  # should not raise


def test_max_subscribers_evicts_oldest():
    queues = [subscribe(_CID, _UID) for _ in range(MAX_SUBSCRIBERS_PER_USER)]
    assert len(_subscribers[f"{_CID}:{_UID}"]) == MAX_SUBSCRIBERS_PER_USER

    # One more should evict the first
    new_q = subscribe(_CID, _UID)
    subs = _subscribers[f"{_CID}:{_UID}"]
    assert len(subs) == MAX_SUBSCRIBERS_PER_USER
    assert queues[0] not in subs
    assert new_q in subs
    # Evicted queue should have None sentinel
    assert queues[0].get_nowait() is None


# ── publish ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_delivers_to_subscriber():
    q = subscribe(_CID, _UID)
    await publish(_CID, _UID, {"msg": "hello"})
    event = q.get_nowait()
    assert event == {"msg": "hello"}


@pytest.mark.asyncio
async def test_publish_company_wide():
    q1 = subscribe(_CID, _UID)
    q2 = subscribe(_CID, _UID2)
    await publish(_CID, None, {"msg": "company-wide"})
    assert q1.get_nowait() == {"msg": "company-wide"}
    assert q2.get_nowait() == {"msg": "company-wide"}


@pytest.mark.asyncio
async def test_publish_no_subscribers_noop():
    # Should not raise
    await publish(_CID, _UID, {"msg": "nobody listening"})


@pytest.mark.asyncio
async def test_publish_targeted_not_cross_user():
    q1 = subscribe(_CID, _UID)
    q2 = subscribe(_CID, _UID2)
    await publish(_CID, _UID, {"msg": "for uid1 only"})
    assert q1.get_nowait() == {"msg": "for uid1 only"}
    assert q2.empty()


@pytest.mark.asyncio
async def test_multiple_subscribers_same_user():
    q1 = subscribe(_CID, _UID)
    q2 = subscribe(_CID, _UID)
    await publish(_CID, _UID, {"msg": "both tabs"})
    assert q1.get_nowait() == {"msg": "both tabs"}
    assert q2.get_nowait() == {"msg": "both tabs"}


# ── event_stream ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_stream_yields_data():
    async def _produce():
        await asyncio.sleep(0.05)
        await publish(_CID, _UID, {"type": "test"})
        await asyncio.sleep(0.05)
        # Send None to terminate the stream
        key = f"{_CID}:{_UID}"
        for q in _subscribers.get(key, []):
            q.put_nowait(None)

    task = asyncio.create_task(_produce())
    events = []
    async for chunk in event_stream(_CID, _UID):
        events.append(chunk)
        if "test" in chunk:
            # Got our event, terminate
            key = f"{_CID}:{_UID}"
            for q in _subscribers.get(key, []):
                q.put_nowait(None)
            break
    await task
    assert any('"type": "test"' in e for e in events)


@pytest.mark.asyncio
async def test_event_stream_cleans_up_on_exit():
    """After generator exits, subscriber is removed."""
    gen = event_stream(_CID, _UID)
    key = f"{_CID}:{_UID}"

    # Start the generator - it subscribes on first iteration
    # Send a sentinel right away so it yields and then terminates
    async def _start_and_terminate():
        # We need to push None into the queue, but the queue doesn't exist yet.
        # Start iterating (which subscribes), then immediately push None.
        await asyncio.sleep(0.05)
        for q in _subscribers.get(key, []):
            q.put_nowait(None)

    task = asyncio.create_task(_start_and_terminate())
    async for _ in gen:
        pass  # Exhaust the generator
    await task

    # After generator cleanup, subscriber should be gone
    assert key not in _subscribers or len(_subscribers.get(key, [])) == 0
