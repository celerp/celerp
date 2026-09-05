# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The gateway send deadline must work on Python 3.10, the package's floor.

pyproject sets requires-python >=3.10, but asyncio.timeout only exists on 3.11+. A
send deadline built on asyncio.timeout raises AttributeError on 3.10 instead of
bounding the send, so every gateway send fails on the minimum supported interpreter.
The deadline must use asyncio.wait_for, which exists on 3.10.

This drives the real _send against a stalled websocket under a 3.10-like runtime
(asyncio.timeout removed) and asserts it surfaces TimeoutError - the honest deadline
behaviour - never an AttributeError from a missing 3.11 primitive. It asserts the
observable outcome of the send, not the name of the timeout primitive in the source.
"""
from __future__ import annotations

import asyncio

import pytest

import celerp.gateway.client as gw


class _StalledWS:
    """A websocket whose send never settles - a backpressured or half-open peer."""

    async def send(self, _data: str) -> None:
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_send_bounds_on_py310_runtime(monkeypatch):
    """On a 3.10-like runtime (asyncio.timeout absent), a send to a stalled peer must
    still hit its deadline and raise TimeoutError. On the broken code the missing
    asyncio.timeout raises AttributeError before the deadline can fire."""
    # Simulate Python 3.10: asyncio.timeout does not exist there.
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    # Shrink the deadline so the test does not wait the full production window.
    monkeypatch.setattr(gw, "_SEND_DEADLINE", 0.05)

    with pytest.raises(TimeoutError):
        await gw.GatewayClient._send(_StalledWS(), {"type": "ping", "id": "x"})
