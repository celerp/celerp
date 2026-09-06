# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Truthful capacity constants and two bounded local UI transports.

One API worker owns one request DB pool, so the pool size and the web UI's
interactive connection ceiling must come from one shared source and stay equal.
A separate, smaller bulk transport carries the few long local operations so they
cannot starve interactive page traffic of connections.
"""
from __future__ import annotations

import contextlib

import httpx
import pytest

import celerp.capacity as capacity
import ui.api_client as api
from ui.api_client import APIError


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


def _raising_client(exc):
    """A drop-in for _client/_anon_client whose context entry raises *exc*."""
    @contextlib.asynccontextmanager
    async def _cm(*_a, **_k):
        raise exc
        yield  # pragma: no cover - unreachable, keeps this an async generator

    return _cm


@pytest.mark.asyncio
async def test_pool_saturation_maps_to_503_not_504(monkeypatch):
    # A pool-acquire timeout means every interactive slot is busy: the app is
    # saturated. It must surface as 503 (retryable), distinct from a genuine
    # upstream 504 timeout - PoolTimeout subclasses TimeoutException, so the
    # order of the except branches is what makes this correct.
    monkeypatch.setattr(api, "_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._api_client("tok") as _c:
            pass
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_upstream_timeout_still_maps_to_504(monkeypatch):
    # A non-pool timeout (the request itself was slow) keeps its 504 meaning.
    monkeypatch.setattr(api, "_client", _raising_client(httpx.ReadTimeout("slow")))
    with pytest.raises(APIError) as exc:
        async with api._api_client("tok") as _c:
            pass
    assert exc.value.status == 504


@pytest.mark.asyncio
async def test_anon_pool_saturation_maps_to_503(monkeypatch):
    monkeypatch.setattr(api, "_anon_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._anon_api_client() as _c:
            pass
    assert exc.value.status == 503


# --- Section 2: the pool-acquire bound is real on every interactive client ---


def test_client_pool_timeout_is_two_seconds():
    # The plain authenticated wrapper factory must carry the finite pool-acquire
    # bound, not an unbounded default that lets a saturated pool hang.
    assert api._client("tok").timeout.pool == 2.0


def test_anon_client_pool_timeout_is_two_seconds():
    assert api._anon_client().timeout.pool == 2.0


def test_local_client_float_timeout_forces_pool_two_seconds():
    c = api._local_client("tok", timeout=7.0)
    assert c.timeout.pool == 2.0


def test_local_client_preconstructed_timeout_forces_pool_but_keeps_the_rest():
    # A caller that hands in a fully specified httpx.Timeout must still get the
    # local pool bound forced, while connect/read/write are preserved exactly.
    t = httpx.Timeout(connect=1.0, read=3.0, write=4.0, pool=30.0)
    c = api._local_client("tok", timeout=t)
    assert c.timeout.pool == 2.0
    assert c.timeout.connect == 1.0
    assert c.timeout.read == 3.0
    assert c.timeout.write == 4.0


@pytest.mark.asyncio
async def test_ai_api_client_uses_interactive_transport_and_pool_bound():
    async with api._ai_api_client("tok", "sess") as c:
        assert c._transport is api._get_transport()
        assert c.timeout.pool == 2.0


@pytest.mark.asyncio
async def test_bulk_api_client_uses_bulk_transport_and_pool_bound():
    async with api._bulk_api_client("tok") as c:
        assert c._transport is api._get_bulk_transport()
        assert c.timeout.pool == 2.0


@pytest.mark.asyncio
async def test_ai_pool_saturation_maps_to_503(monkeypatch):
    monkeypatch.setattr(api, "_local_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._ai_api_client("tok", "sess") as _c:
            pass
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_bulk_pool_saturation_maps_to_503(monkeypatch):
    monkeypatch.setattr(api, "_local_client", _raising_client(httpx.PoolTimeout("pool")))
    with pytest.raises(APIError) as exc:
        async with api._bulk_api_client("tok") as _c:
            pass
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_bulk_read_timeout_maps_to_504(monkeypatch):
    monkeypatch.setattr(api, "_local_client", _raising_client(httpx.ReadTimeout("slow")))
    with pytest.raises(APIError) as exc:
        async with api._bulk_api_client("tok") as _c:
            pass
    assert exc.value.status == 504


def test_configured_client_has_a_finite_pool_bound():
    # The concrete proof that a saturated pool fails fast rather than hanging: the
    # client the factory actually builds carries a finite 2.0 second pool-acquire
    # timeout, which is exactly the bound httpx applies when every connection is
    # busy. (A live-socket saturation drive is left to the manual gate in 14A;
    # httpcore's pool-acquire wait is not deterministically observable in-process.)
    for c in (api._client("tok"), api._anon_client(), api._local_client("tok")):
        assert c.timeout.pool == 2.0
        assert c.timeout.pool is not None


def test_bulk_and_interactive_transports_are_distinct():
    assert api._get_transport() is not api._get_bulk_transport()
