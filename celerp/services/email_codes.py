# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Codes the Celerp relay emails and confirms, for password recovery.

The relay sends a code to an address and says whether a code is the one it sent,
for this installation (named by its instance id).
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

import httpx

from celerp.config import ensure_instance_id

log = logging.getLogger(__name__)

# Each call's whole budget, connection included.
BUDGET_S = 3.0


class Outcome(str, Enum):
    OK = "ok"
    # The relay answered no: a wrong, expired, used or exhausted code, or too many.
    REFUSED = "refused"
    # No usable answer: unreachable, too slow, or an answer it should never give.
    UNAVAILABLE = "unavailable"


def _outcome(response: httpx.Response, *, refused: tuple[int, ...]) -> Outcome:
    if response.status_code in refused:
        return Outcome.REFUSED
    if response.status_code != 200:
        return Outcome.UNAVAILABLE
    try:
        body = response.json()
    except ValueError:
        return Outcome.UNAVAILABLE
    return Outcome.OK if isinstance(body, dict) and body.get("ok") is True else Outcome.UNAVAILABLE


async def _call(method: str, path: str, body: dict | None = None, *, refused: tuple[int, ...] = ()) -> Outcome:
    from celerp.gateway import state

    url = f"{state.relay_http_url()}/email-codes/{path}"

    async def _op(client: httpx.AsyncClient) -> Outcome:
        return _outcome(await client.request(method, url, json=body), refused=refused)

    try:
        return await state.with_relay_client(BUDGET_S, _op)
    except httpx.HTTPError as exc:
        log.warning("Relay email-codes %s unavailable: %s", path, type(exc).__name__)
        return Outcome.UNAVAILABLE


async def status() -> Outcome:
    """Whether the relay can send and check codes now."""
    return await _call("GET", "status")


async def send(email: str) -> Outcome:
    """Ask the relay to email a code to *email*."""
    iid = await asyncio.to_thread(ensure_instance_id)
    return await _call("POST", "send", {"email": email, "instance_id": iid},
                       refused=(400, 429))


async def verify(email: str, code: str) -> Outcome:
    """Whether *code* is the one the relay emailed *email*; the relay spends it on a yes."""
    iid = await asyncio.to_thread(ensure_instance_id)
    return await _call("POST", "verify", {"email": email, "instance_id": iid, "code": code},
                       refused=(400, 429))
