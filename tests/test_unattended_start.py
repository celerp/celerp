# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A background loop started unattended and cancelled before its first step (the app shut
down at once) leaves no coroutine behind that was never awaited."""
from __future__ import annotations

import asyncio
import gc
import warnings

from celerp.events.engine import unattended


def test_a_loop_cancelled_before_its_first_step_leaves_no_unawaited_coroutine():
    started = []

    async def loop():
        started.append(True)

    async def main():
        task = asyncio.create_task(unattended(loop))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(main())
        gc.collect()
    assert started == []
    assert [w for w in caught if "never awaited" in str(w.message)] == []


def test_an_unattended_loop_runs_marked_automatic():
    from celerp.events import engine

    async def loop():
        return engine._UNATTENDED.get()

    assert asyncio.run(unattended(loop)) is True
    assert engine._UNATTENDED.get() is False
