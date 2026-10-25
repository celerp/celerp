# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every AI clock the app holds, in one place.

The AI service publishes its own clocks (``GET /ai/limits``, mirrored on every AI
reply as ``X-AI-Limits: D,S,G,W``); the service's config is authoritative and
``CURRENT_RELAY_DEFAULTS`` is its mirror. Each model call declares its step budget
(``X-AI-Attempt-Budget-S``) and the service never runs a step longer than that, so a
stale or missing copy of the limits only ever shortens a step. Every app clock is
derived here and ordered after the service's own end of the same step: the reply
cannot come later than upload + D + S, the app waits that plus the response latency,
and a usage row is read back only after that plus the guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from celerp.gateway.state import RELAY_CONNECT_ATTEMPTS, RELAY_CONNECT_TIMEOUT_S, relay_http_url
from celerp.services.cloud_entitlement import RELAY_ENTITLEMENT_TIMEOUT

log = logging.getLogger(__name__)

MIB = 1024 * 1024

# The reply's latency plus the service's pre-admission work (its admitting
# transaction is bounded by a 5 s lock timeout).
RESPONSE_LATENCY_S = 10.0
# The slowest upload a call is sized for (2 Mbit/s).
FLOOR_UPLOAD_BYTES_PER_S = 250_000
# Every browser-leg request: a question's start and each poll, and the limits fetch.
UI_REQUEST_S = 10.0
# One tool call of the agent.
TOOL_STEP_S = 30.0
# A question still running this long after its first model step took a gate slot
# keeps working in the background.
PROMOTE_AFTER_S = 120.0
# The one wall clock of a synchronous caller (POST /ai/query, the module API).
SYNC_WAIT_S = 120.0
# The remote-access proxy's timeout on the service (its proxy_timeout_seconds).
REMOTE_PROXY_TIMEOUT_S = 180.0
# Confirming proposed actions runs tools only, no model call.
ACTIONS_TIMEOUT_S = 150.0
# Seconds between the background passes that settle pending usage rows.
RECONCILE_INTERVAL_S = 30.0
# Pending usage rows asked about in one status read (the service's batch bound).
RECONCILE_BATCH = 50

assert SYNC_WAIT_S + UI_REQUEST_S <= REMOTE_PROXY_TIMEOUT_S - 30


@dataclass(frozen=True)
class Limits:
    attempt_deadline_s: float   # D: the longest one model step runs on the service
    settle_margin_s: float      # S: the service settles a step within this after it ends
    guard_s: float              # G: clock skew allowance
    retry_window_s: float       # W: how long a question id can be asked again
    max_body_bytes: int
    max_tokens: int


@dataclass(frozen=True)
class CallBudget:
    upload_s: float     # the request must have been sent by then, or it is aborted unsent
    declared_s: float   # sent as X-AI-Attempt-Budget-S
    total_s: float      # the whole call: connect, sign-in, upload and reply


CURRENT_RELAY_DEFAULTS = Limits(attempt_deadline_s=120.0, settle_margin_s=15.0, guard_s=5.0,
                                retry_window_s=86400.0, max_body_bytes=20 * MIB, max_tokens=4000)
# A relay from before the published limits ran 60 s per call phase, and the app
# called it with 90 s in all; it has no step replay and no status batch.
_OLDER_RELAY_LIMITS = Limits(attempt_deadline_s=60.0, settle_margin_s=8.0, guard_s=5.0,
                             retry_window_s=86400.0, max_body_bytes=20 * MIB, max_tokens=2048)

_FIELDS = ("attempt_deadline_s", "settle_margin_s", "guard_s", "retry_window_s", "max_body_bytes",
           "max_tokens")
_HEADER_FIELDS = _FIELDS[:4]


def upload_budget_s(nbytes: int) -> float:
    """Connect retries, the account sign-in, then the body at the floor rate."""
    return (RELAY_CONNECT_ATTEMPTS * RELAY_CONNECT_TIMEOUT_S + RELAY_ENTITLEMENT_TIMEOUT
            + nbytes / FLOOR_UPLOAD_BYTES_PER_S)


def call_budget(limits: Limits, nbytes: int) -> CallBudget:
    upload = upload_budget_s(nbytes)
    declared = limits.attempt_deadline_s
    return CallBudget(upload_s=upload, declared_s=declared,
                      total_s=upload + declared + limits.settle_margin_s + RESPONSE_LATENCY_S)


def reconcile_delay_s(limits: Limits, nbytes: int) -> float:
    """After this, the service has ended and settled every send of that size."""
    return call_budget(limits, nbytes).total_s + limits.guard_s


def status_timeout_s(limits: Limits) -> float:
    return limits.settle_margin_s + RESPONSE_LATENCY_S


def declared_header(budget: CallBudget) -> dict[str, str]:
    return {"X-AI-Attempt-Budget-S": f"{budget.declared_s:g}"}


def parse_limits(body: dict) -> Limits:
    """The published limits; ValueError when a field is missing or not a number."""
    if not isinstance(body, dict) or any(f not in body for f in _FIELDS):
        raise ValueError("The AI service's limits are incomplete.")
    values = {f: body[f] for f in _FIELDS}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in values.values()):
        raise ValueError("The AI service's limits are not numbers.")
    return Limits(**{f: float(v) for f, v in values.items() if f in _HEADER_FIELDS},
                  max_body_bytes=int(values["max_body_bytes"]), max_tokens=int(values["max_tokens"]))


_held: Limits | None = None


def forget_limits() -> None:
    global _held
    _held = None


async def limits() -> Limits:
    """The limits the app holds, fetched once and again whenever a reply's header
    differs. A failed fetch uses the current defaults and is retried on the next call;
    an older relay (no limits route) gets the pairing it shipped with."""
    global _held
    if _held is not None:
        return _held
    try:
        async with httpx.AsyncClient(timeout=UI_REQUEST_S) as client:
            resp = await client.get(f"{relay_http_url()}/ai/limits")
        if resp.status_code == 404:
            _held = _OLDER_RELAY_LIMITS
        elif resp.status_code == 200:
            _held = parse_limits(resp.json())
        else:
            log.info("AI limits fetch answered %s; using the current defaults", resp.status_code)
            return CURRENT_RELAY_DEFAULTS
    except (httpx.HTTPError, ValueError) as exc:
        log.info("AI limits fetch failed (%s); using the current defaults", type(exc).__name__)
        return CURRENT_RELAY_DEFAULTS
    return _held


def note_header(value: str | None) -> None:
    """A reply's X-AI-Limits header; limits that differ from the held ones are
    fetched again on the next call."""
    if value is None or _held is None:
        return
    try:
        published = tuple(float(v) for v in value.split(","))
    except ValueError:
        return
    if published != tuple(getattr(_held, f) for f in _HEADER_FIELDS):
        forget_limits()
