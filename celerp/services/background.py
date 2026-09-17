# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Background tasks that outlive the request or handler that started them."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine

_BG_TASKS: set[asyncio.Task] = set()


def spawn_background(coro: Coroutine) -> asyncio.Task:
    """Run coro as a task on the running loop and return it.

    The task is held in a module-level set until it finishes so the event
    loop cannot garbage-collect it mid-run; callers that only need
    fire-and-forget can ignore the return value.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task
