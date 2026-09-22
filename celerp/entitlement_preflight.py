# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Pre-boot entitlement preflight for the packaged app.

Electron spawns this one-shot before it decides whether to open a previously
selected external database. It refreshes the subscription entitlement from the
relay and reports a tri-state exit code so the boot path never silently forks
data between an external and a local store:

    RENEWED (0)     the refreshed flags allow external; continue external.
    EXPIRED (2)     the subscription lapsed; fall back to local.
    UNREACHABLE (3) the relay could not authoritatively answer; ask, never guess.

Only a well-formed HTTP 200 body is authoritative. Every transport error,
timeout, 5xx, 401/403, malformed body, or failed locked write maps to
UNREACHABLE, so a rotated credential or a half-refreshed config never reads as
an expiry and never diverts the running database.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from celerp.config import load_cloud_config, settings
from celerp.config_store import merge_packaged_config

log = logging.getLogger(__name__)

RENEWED = 0
EXPIRED = 2
UNREACHABLE = 3

# Boot-path bounds: short per-attempt timeout, at most two attempts, well under
# the gateway's own (0, 5, 30) reconnect ladder, which would stall startup.
_PREFLIGHT_TIMEOUT_S = 5.0
_PREFLIGHT_ATTEMPTS = 2


async def _request_subscription():
    """Fetch the current subscription entitlement from the relay.

    Exchanges the instance token for a short-lived bearer, then GETs
    /billing/subscription, reusing one client for both. Returns the httpx
    Response of any status on the first attempt that produced one, or None when
    every attempt hit a transport error. Only transport failures are retried; a
    response of any status is final and interpreted by the caller.
    """
    import httpx

    from celerp.gateway.state import fetch_relay_bearer, relay_http_url

    url = f"{relay_http_url()}/billing/subscription"
    for attempt in range(_PREFLIGHT_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_PREFLIGHT_TIMEOUT_S) as client:
                bearer = await fetch_relay_bearer(client)
                return await client.get(
                    url, headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError:
            if attempt + 1 >= _PREFLIGHT_ATTEMPTS:
                return None
    return None


from celerp.gateway.state import valid_feature_flags


def run_preflight() -> int:
    """Refresh the entitlement and return one of RENEWED / EXPIRED / UNREACHABLE."""
    load_cloud_config()
    if not settings.gateway_token:
        # An install that never associated has no entitlement to confirm; there
        # is nothing to refresh, so fall back to local without a relay call.
        return EXPIRED

    try:
        resp = asyncio.run(_request_subscription())
    except Exception:
        return UNREACHABLE
    if resp is None or getattr(resp, "status_code", None) != 200:
        return UNREACHABLE

    try:
        body = resp.json()
    except Exception:
        return UNREACHABLE
    if not isinstance(body, dict):
        return UNREACHABLE

    flags = body.get("feature_flags")
    if not valid_feature_flags(flags):
        return UNREACHABLE

    # The nested grace inside feature_flags is the authoritative contract value.
    # The relay also mirrors it at the top level; when both are present they must
    # agree, else the body is internally inconsistent and cannot be trusted.
    nested_grace = flags.get("grace_period_ends")
    top_level_grace = body.get("grace_period_ends")
    if not _valid_grace(top_level_grace):
        return UNREACHABLE
    if (nested_grace is not None and top_level_grace is not None
            and nested_grace != top_level_grace):
        return UNREACHABLE

    # Persist the refreshed entitlement through the one locked writer. The
    # canonical shape already carries grace_period_ends nested where every
    # reader expects it. A failed write is UNREACHABLE, never a half-refreshed
    # RENEWED.
    if not merge_packaged_config({"feature_flags": dict(flags)}):
        return UNREACHABLE

    from celerp.gateway.state import grace_ends_in_future

    external_allowed = bool(flags.get("external_db")) or grace_ends_in_future(nested_grace)
    return RENEWED if external_allowed else EXPIRED


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run_preflight())


if __name__ == "__main__":
    main()