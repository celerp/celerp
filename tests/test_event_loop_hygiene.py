# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A sync test that runs its own loop leaves no unclosed loop behind.

Before each async test pytest-asyncio saves the loop ``asyncio.get_event_loop()``
returns and puts it back afterwards. On the main thread that call creates a loop when
none is set, and nobody closes it. A later sync test that runs its own loop with
``asyncio.run()`` replaces that loop, it is collected unclosed, and the
ResourceWarning fails whichever test happens to be running at that moment.
"""
from __future__ import annotations

import asyncio
import gc
import warnings


async def test_an_async_test_has_run_in_this_process():
    assert asyncio.get_running_loop() is not None


def test_a_sync_test_running_its_own_loop_drops_no_loop():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(asyncio.sleep(0))
        gc.collect()
    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []
