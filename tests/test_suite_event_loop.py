# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Outside an async test no event loop is current, so asking for one never makes a
loop that nothing closes: a sync test's asyncio.run() would replace it, and its
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


def test_no_loop_is_current_in_a_sync_test():
    with pytest.raises(RuntimeError):
        _current_loop()


def test_asyncio_run_in_a_sync_test_leaves_nothing_unclosed():
    asyncio.run(asyncio.sleep(0))
    with pytest.raises(RuntimeError):
        _current_loop()
    gc.collect()
