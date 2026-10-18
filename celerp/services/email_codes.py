# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Codes the Celerp relay emails and confirms, for password recovery.

The relay sends a code to an address and says whether a code is the one it sent,
for this installation (named by its instance id) and this attempt. Each address's
attempt has a secret only this process holds, so a caller without it cannot spend or
use up that code.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from enum import Enum

import httpx

from celerp.config import ensure_instance_id

log = logging.getLogger(__name__)

# The relay's rule for an address it will mail (the relay's email-codes router, through its
# signup address check, is the authority; keep this in step with it).
_MAILABLE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAILABLE_MAX = 255

# Each call's whole budget, connection included.
BUDGET_S = 3.0

# An attempt lasts as long as the relay's code, refreshed by each send. One API process
# serves recovery (see celerp.capacity), so memory holds every attempt; a restart loses
# them and the person asks for a new code.
ATTEMPT_TTL_S = 600.0
_attempts: dict[str, tuple[str, float]] = {}


def _attempt(email: str, *, start: bool) -> str | None:
    """The secret of *email*'s live attempt; on *start*, made if there is none and kept
    for another code's life."""
    now = time.monotonic()
    for key in [k for k, (_, until) in _attempts.items() if until <= now]:
        del _attempts[key]
    key = email.strip().lower()
    secret = _attempts[key][0] if key in _attempts else None
    if start:
        secret = secret or secrets.token_urlsafe(32)
        _attempts[key] = (secret, now + ATTEMPT_TTL_S)
    return secret


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


def mailable(email: str) -> bool:
    """Whether the relay would ever mail a code to *email*."""
    email = email.strip().lower()
    return bool(_MAILABLE.match(email)) and len(email) <= _MAILABLE_MAX


async def status() -> Outcome:
    """Whether the relay can send and check codes now."""
    return await _call("GET", "status")


async def send(email: str) -> Outcome:
    """Ask the relay to email a code to *email*."""
    iid = await asyncio.to_thread(ensure_instance_id)
    return await _call("POST", "send", {"email": email, "instance_id": iid,
                                        "attempt": _attempt(email, start=True)})


async def verify(email: str, code: str) -> Outcome:
    """Whether *code* is the one the relay emailed *email*; the relay spends it on a yes,
    which ends the attempt. Without a live attempt no code here can be right."""
    attempt = _attempt(email, start=False)
    if attempt is None:
        return Outcome.REFUSED
    iid = await asyncio.to_thread(ensure_instance_id)
    outcome = await _call("POST", "verify", {"email": email, "instance_id": iid, "attempt": attempt, "code": code},
                          refused=(400, 429))
    if outcome is Outcome.OK:
        _attempts.pop(email.strip().lower(), None)
    return outcome
