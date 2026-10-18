# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Codes the Celerp relay emails and confirms, for password recovery.

The relay sends a code to an address and says whether a code is the one it sent,
for this installation (named by its instance id) and this attempt. Each recovery is an
attempt of its own, held by its requester through a handle, with a secret only this
process holds, so no one else can spend or use up its code.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Attempt:
    """One requester's recovery of one address: the handle they hold it by, the address
    (lowercased) and the secret the relay ties its code to."""
    handle: str
    email: str
    secret: str = field(repr=False)


_attempts: dict[str, tuple[Attempt, float]] = {}


def live(handle: str | None, email: str) -> Attempt | None:
    """The live attempt *handle* names, if it is a recovery of *email*."""
    now = time.monotonic()
    for key in [k for k, (_, until) in _attempts.items() if until <= now]:
        del _attempts[key]
    held = _attempts.get(handle or "")
    return held[0] if held and held[0].email == email.strip().lower() else None


def attempt(email: str, handle: str | None = None) -> Attempt:
    """The requester's attempt at recovering *email*: the one *handle* names when it is
    live and for this address, else a new one; kept for another code's life."""
    found = live(handle, email) or Attempt(secrets.token_urlsafe(24), email.strip().lower(),
                                           secrets.token_urlsafe(32))
    _attempts[found.handle] = (found, time.monotonic() + ATTEMPT_TTL_S)
    return found


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


async def send(email: str, attempt: Attempt) -> Outcome:
    """Ask the relay to email a code to *email* for *attempt*."""
    iid = await asyncio.to_thread(ensure_instance_id)
    return await _call("POST", "send", {"email": email, "instance_id": iid, "attempt": attempt.secret})


async def verify(email: str, attempt: Attempt, code: str) -> Outcome:
    """Whether *code* is the one the relay emailed *email* for *attempt*; the relay spends
    it on a yes, which ends the attempt unless its handle holds another by then."""
    iid = await asyncio.to_thread(ensure_instance_id)
    outcome = await _call("POST", "verify", {"email": email, "instance_id": iid, "attempt": attempt.secret,
                                             "code": code}, refused=(400, 429))
    if outcome is Outcome.OK and _attempts.get(attempt.handle, (None,))[0] == attempt:
        del _attempts[attempt.handle]
    return outcome
