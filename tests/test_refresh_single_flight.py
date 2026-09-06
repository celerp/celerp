# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Cancellation-safe single-flight JWT refresh in ui.api_client.

Several concurrent authenticated requests can each hit the sliding-window
refresh path carrying the same refresh cookie. Each used to run its own upstream
POST /auth/token/refresh, so N waiters meant N upstream calls under load. The
single-flight coordinator collapses concurrent identical presentations into one
upstream POST whose result every waiter receives, shields the shared task from
one waiter's cancellation, keeps a successful pair for a short coalescing grace,
and never caches a failure.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

import ui.api_client as api


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """Clear the coordinator's module-level maps before and after each test."""
    api._reset_refresh_state_for_tests()
    yield
    api._reset_refresh_state_for_tests()


def _install_counting_upstream(monkeypatch, *, result=("A1", "R1"), fail=False, delay=0.0):
    """Replace the raw upstream POST with a counter. Returns a dict with 'calls'."""
    state = {"calls": 0}

    async def _fake_post(refresh_token: str):
        state["calls"] += 1
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise api.APIError(401, "refresh rejected")
        return result

    monkeypatch.setattr(api, "_refresh_upstream", _fake_post)
    return state


@pytest.mark.asyncio
async def test_refresh_single_flight_one_upstream(monkeypatch):
    state = _install_counting_upstream(monkeypatch, result=("acc", "ref"), delay=0.05)
    results = await asyncio.gather(*[api.refresh_access_token("tok") for _ in range(8)])
    assert state["calls"] == 1
    assert all(r == ("acc", "ref") for r in results)


@pytest.mark.asyncio
async def test_refresh_failure_coalesces_and_clears(monkeypatch):
    state = _install_counting_upstream(monkeypatch, fail=True, delay=0.05)
    results = await asyncio.gather(
        *[api.refresh_access_token("tok") for _ in range(5)],
        return_exceptions=True,
    )
    assert state["calls"] == 1
    assert all(isinstance(r, api.APIError) for r in results)
    # No success/failure cache remains: a later independent call retries upstream.
    state2 = _install_counting_upstream(monkeypatch, result=("acc2", "ref2"))
    again = await api.refresh_access_token("tok")
    assert again == ("acc2", "ref2")
    assert state2["calls"] == 1


@pytest.mark.asyncio
async def test_refresh_waiter_cancellation_does_not_cancel_shared_task(monkeypatch):
    state = _install_counting_upstream(monkeypatch, result=("acc", "ref"), delay=0.2)
    # One waiter that we cancel, plus survivors that must still complete.
    cancelled = asyncio.ensure_future(api.refresh_access_token("tok"))
    survivors = [asyncio.ensure_future(api.refresh_access_token("tok")) for _ in range(3)]
    await asyncio.sleep(0.02)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    done = await asyncio.gather(*survivors)
    assert state["calls"] == 1
    assert all(r == ("acc", "ref") for r in done)


@pytest.mark.asyncio
async def test_refresh_success_grace_then_expiry(monkeypatch):
    state = _install_counting_upstream(monkeypatch, result=("acc", "ref"))
    first = await api.refresh_access_token("tok")
    assert first == ("acc", "ref")
    # An immediate straggler reuses the cached pair without a second upstream POST.
    straggler = await api.refresh_access_token("tok")
    assert straggler == ("acc", "ref")
    assert state["calls"] == 1
    # After the coalescing grace expires, a fresh burst creates exactly one new POST.
    monkeypatch.setattr(api, "_REFRESH_GRACE_SECONDS", 0.05)
    await asyncio.sleep(0.08)
    state2 = _install_counting_upstream(monkeypatch, result=("acc3", "ref3"))
    burst = await asyncio.gather(*[api.refresh_access_token("tok") for _ in range(4)])
    assert state2["calls"] == 1
    assert all(r == ("acc3", "ref3") for r in burst)


@pytest.mark.asyncio
async def test_refresh_no_raw_token_retained_as_key(monkeypatch):
    """The in-process key is the SHA-256 digest of the token, never the raw token."""
    _install_counting_upstream(monkeypatch, result=("acc", "ref"), delay=0.05)
    task = asyncio.ensure_future(api.refresh_access_token("super-secret-refresh-token"))
    await asyncio.sleep(0.01)
    keys = list(api._refresh_inflight.keys())
    assert keys, "an inflight entry should exist while the POST is in flight"
    for k in keys:
        assert isinstance(k, bytes) and len(k) == 32
        assert b"super-secret-refresh-token" not in k
    await task


@pytest.mark.asyncio
async def test_close_shared_client_clears_refresh_state(monkeypatch):
    _install_counting_upstream(monkeypatch, result=("acc", "ref"), delay=0.3)
    task = asyncio.ensure_future(api.refresh_access_token("tok"))
    await asyncio.sleep(0.02)
    assert api._refresh_inflight, "an inflight task should exist"
    await api.close_shared_client()
    assert not api._refresh_inflight
    assert not api._refresh_success
    with pytest.raises((asyncio.CancelledError, api.APIError)):
        await task


# --- Section 3: the shared task owns its own cleanup ---


def _blocking_upstream(monkeypatch, gate: asyncio.Event, *, fail: bool, result=("acc", "ref")):
    """Upstream that blocks on *gate* before completing. Returns a call counter."""
    state = {"calls": 0}

    async def _fake(refresh_token: str):
        state["calls"] += 1
        await gate.wait()
        if fail:
            raise api.APIError(401, "refresh rejected")
        return result

    monkeypatch.setattr(api, "_refresh_upstream", _fake)
    return state


@pytest.mark.asyncio
async def test_all_waiters_cancel_then_upstream_fails_self_cleans(monkeypatch):
    gate = asyncio.Event()
    state = _blocking_upstream(monkeypatch, gate, fail=True)
    waiters = [asyncio.ensure_future(api.refresh_access_token("tok")) for _ in range(3)]
    await asyncio.sleep(0.02)
    assert api._refresh_inflight, "the shared task should be inflight"
    for w in waiters:
        w.cancel()
    for w in waiters:
        with pytest.raises(asyncio.CancelledError):
            await w
    # Release the shared task; with every waiter gone it must still clean itself.
    gate.set()
    await asyncio.sleep(0.05)
    assert not api._refresh_inflight, "the task, not a waiter, owns cleanup"
    assert not api._refresh_success
    assert state["calls"] == 1
    # A later independent request performs a fresh upstream request, not a replay.
    gate2 = asyncio.Event()
    gate2.set()
    state2 = _blocking_upstream(monkeypatch, gate2, fail=False, result=("acc2", "ref2"))
    again = await api.refresh_access_token("tok")
    assert again == ("acc2", "ref2")
    assert state2["calls"] == 1


@pytest.mark.asyncio
async def test_orphan_failure_emits_no_unretrieved_task_warning(monkeypatch):
    seen = []
    loop = asyncio.get_running_loop()
    prev = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx.get("message", "")))
    try:
        gate = asyncio.Event()
        _blocking_upstream(monkeypatch, gate, fail=True)
        w = asyncio.ensure_future(api.refresh_access_token("tok"))
        await asyncio.sleep(0.02)
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        gate.set()
        await asyncio.sleep(0.05)
        # Force the failed orphan to be collected and its exception state inspected.
        import gc

        gc.collect()
        await asyncio.sleep(0.01)
    finally:
        loop.set_exception_handler(prev)
    assert not any("never retrieved" in m for m in seen), seen


@pytest.mark.asyncio
async def test_expired_success_entries_for_other_hashes_are_pruned(monkeypatch):
    _install_counting_upstream(monkeypatch, result=("acc", "ref"))
    other = api._refresh_key("a-different-refresh-token")
    api._refresh_success[other] = (
        time.monotonic() - (api._REFRESH_GRACE_SECONDS + 1.0),
        ("stale", "stale"),
    )
    await api.refresh_access_token("tok")
    # The unrelated expired digest entry is pruned during the next refresh, not
    # left to accumulate for a token that is never presented again.
    assert other not in api._refresh_success
