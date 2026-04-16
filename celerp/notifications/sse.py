# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""SSE pub/sub for real-time notification delivery.

In-process implementation using asyncio.Queue per subscriber.
No Redis or external message broker needed (single-process architecture).

Subscribers are keyed by "company_id:user_id". Company-wide notifications
(user_id=None) are delivered to all subscribers for that company.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from typing import Any, AsyncGenerator

log = logging.getLogger(__name__)

MAX_SUBSCRIBERS_PER_USER = 5

# key = "company_id:user_id", value = list of queues
_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def _key(company_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"{company_id}:{user_id}"


def _company_prefix(company_id: uuid.UUID) -> str:
    return f"{company_id}:"


def subscribe(company_id: uuid.UUID, user_id: uuid.UUID) -> asyncio.Queue:
    """Register a subscriber queue. Returns the queue to read from.

    Enforces MAX_SUBSCRIBERS_PER_USER. If exceeded, oldest queue is evicted.
    """
    key = _key(company_id, user_id)
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    subs = _subscribers[key]
    while len(subs) >= MAX_SUBSCRIBERS_PER_USER:
        evicted = subs.pop(0)
        # Signal the evicted subscriber to close
        evicted.put_nowait(None)
    subs.append(q)
    return q


def unsubscribe(company_id: uuid.UUID, user_id: uuid.UUID, q: asyncio.Queue) -> None:
    """Remove a subscriber queue."""
    key = _key(company_id, user_id)
    subs = _subscribers.get(key, [])
    try:
        subs.remove(q)
    except ValueError:
        pass
    if not subs:
        _subscribers.pop(key, None)


async def publish(
    company_id: uuid.UUID,
    user_id: uuid.UUID | None,
    event_data: dict[str, Any],
) -> None:
    """Publish an event to subscribers.

    If user_id is None (company-wide), publishes to all subscribers
    whose key starts with the company_id prefix.
    If user_id is set, publishes only to that user's subscribers.
    """
    prefix = _company_prefix(company_id)

    if user_id is None:
        # Company-wide: deliver to all subscribers for this company
        targets = [
            q
            for key, subs in _subscribers.items()
            if key.startswith(prefix)
            for q in subs
        ]
    else:
        targets = list(_subscribers.get(_key(company_id, user_id), []))

    for q in targets:
        try:
            q.put_nowait(event_data)
        except asyncio.QueueFull:
            log.debug("SSE subscriber queue full, dropping event")


def shutdown_all() -> None:
    """Signal all active SSE subscribers to terminate. Call during lifespan shutdown."""
    for subs in list(_subscribers.values()):
        for q in subs:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
    _subscribers.clear()


async def event_stream(
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AsyncGenerator[str, None]:
    """SSE event generator. Yields formatted SSE strings.

    Yields a keepalive comment every 30s to prevent connection timeout.
    Terminates when None is received (eviction or shutdown) or when the
    request is cancelled (client disconnect or server shutdown).
    """
    q = subscribe(company_id, user_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            except asyncio.CancelledError:
                # Request cancelled — client disconnected or server shutting down.
                return

            if event is None:
                # Eviction or shutdown signal
                return

            yield f"data: {json.dumps(event)}\n\n"
    except asyncio.CancelledError:
        # Catch CancelledError raised outside the inner try (e.g. during yield)
        return
    finally:
        unsubscribe(company_id, user_id, q)
