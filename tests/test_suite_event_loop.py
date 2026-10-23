# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Outside an async test the only loop that can be current is the suite's own session
loop, which is closed when the run ends. Asking for the current loop never makes a
new one that nothing closes: a sync test's asyncio.run() would replace it, and its
sockets would be reported unclosed in a later, unrelated test."""

from __future__ import annotations

import asyncio
import gc
import warnings

import pytest


def _current_loop():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return asyncio.get_event_loop()


async def test_an_async_test_runs_on_a_loop():
    assert asyncio.get_running_loop() is not None


def _session_loop(request):
    defs = request._fixturemanager.getfixturedefs("_session_scoped_runner", request.node)
    cached = defs[-1].cached_result if defs else None
    return cached[0].get_loop() if cached else None


def test_a_loop_current_in_a_sync_test_is_the_session_loop(request):
    try:
        loop = _current_loop()
    except RuntimeError:
        return
    assert loop is _session_loop(request)
    assert not loop.is_closed()


def test_asyncio_run_in_a_sync_test_leaves_nothing_unclosed():
    asyncio.run(asyncio.sleep(0))
    with pytest.raises(RuntimeError):
        _current_loop()
    gc.collect()
