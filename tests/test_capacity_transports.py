# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Truthful capacity constants and two bounded local UI transports.

One API worker owns one request DB pool, so the pool size and the web UI's
interactive connection ceiling must come from one shared source and stay equal.
A separate, smaller bulk transport carries the few long local operations so they
cannot starve interactive page traffic of connections.
"""
from __future__ import annotations

import httpx

import celerp.capacity as capacity
import ui.api_client as api


def test_capacity_constants_are_the_pool_budget():
    assert capacity.REQUEST_DB_POOL_SIZE == 10
    assert capacity.REQUEST_DB_MAX_OVERFLOW == 5


def test_db_engine_pool_uses_capacity_constants():
    import inspect

    import celerp.db as db

    # The engine reads the shared constants, not literals, so its pool can never
    # drift from the UI's interactive ceiling. Under NullPool (test mode) the pool
    # has no fixed size to introspect, so assert the sized pool only when present
    # and always assert the source wires the constants through.
    pool = db.engine.pool
    size = getattr(pool, "size", None)
    if callable(size):
        assert pool.size() == capacity.REQUEST_DB_POOL_SIZE
        assert pool._max_overflow == capacity.REQUEST_DB_MAX_OVERFLOW
    src = inspect.getsource(db)
    assert "pool_size=REQUEST_DB_POOL_SIZE" in src
    assert "max_overflow=REQUEST_DB_MAX_OVERFLOW" in src
    # The stale multi-worker pool arithmetic must not survive.
    assert "gui_workers" not in src
    assert "2 API" not in src


def _limits_of(transport: httpx.AsyncHTTPTransport) -> httpx.Limits:
    # The transport keeps its pool on _pool; the configured limits are readable
    # from the pool's max-connection attributes.
    pool = transport._pool
    return pool


def test_interactive_transport_ceiling_is_pool_size():
    transport = api._get_transport()
    pool = _limits_of(transport)
    assert pool._max_connections == capacity.REQUEST_DB_POOL_SIZE


def test_bulk_transport_is_separate_and_small():
    interactive = api._get_transport()
    bulk = api._get_bulk_transport()
    assert bulk is not interactive
    assert _limits_of(bulk)._max_connections == 2


def test_local_client_factory_preserves_redirect_choice():
    # The factory must not force follow_redirects: a proxy route that inspects a
    # raw redirect status needs follow_redirects=False preserved.
    no_follow = api._local_client(follow_redirects=False)
    try:
        assert no_follow.follow_redirects is False
    finally:
        pass
    follow = api._local_client(follow_redirects=True)
    assert follow.follow_redirects is True


def test_local_client_bulk_uses_bulk_transport():
    c = api._local_client(bulk=True)
    assert c._transport is api._get_bulk_transport()
    interactive = api._local_client(bulk=False)
    assert interactive._transport is api._get_transport()
