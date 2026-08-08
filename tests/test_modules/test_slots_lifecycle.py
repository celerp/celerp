# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""fire_lifecycle logs a failing hook at ERROR (non-fatal to boot).

A lifecycle hook that raises must never block its siblings or crash boot, but
its failure has to be visible: logged at ERROR so a recurrence surfaces as an
alert instead of vanishing into a WARNING no one reads.
"""

from __future__ import annotations

import logging

import pytest

from celerp.modules import slots


async def _boom(**kwargs):
    raise RuntimeError("hook exploded")


@pytest.fixture(autouse=True)
def clear_slots():
    slots.clear()
    yield
    slots.clear()


@pytest.mark.asyncio
async def test_hook_failure_logged_error(caplog):
    """A raising hook is swallowed (fire_lifecycle does not propagate) but its
    failure is logged at ERROR level, with the underlying exception message."""
    slots.register("on_modules_ready", {"handler": f"{__name__}:_boom", "_module": "test-mod"})

    with caplog.at_level(logging.WARNING, logger="celerp.modules.slots"):
        await slots.fire_lifecycle("on_modules_ready", session=None)  # must not raise

    matching = [
        r for r in caplog.records
        if r.name == "celerp.modules.slots" and "hook exploded" in r.getMessage()
    ]
    assert matching, "the failing hook was not logged at all"
    assert all(r.levelno == logging.ERROR for r in matching), (
        f"expected the hook failure at ERROR, got {[r.levelname for r in matching]}"
    )
