# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Gateway quota status client.

Reads AI quota status from the relay for display and tier checks. Quota is
enforced by the gateway as a query runs; this module only reports it.
"""

from __future__ import annotations

import logging

import httpx

from celerp.config import settings
from celerp.gateway.state import (
    get_session_token,
    relay_http_url,
    relay_session_headers,
)

# Alias so tests / internal callers import this from here rather than reaching
# into celerp.gateway.state directly.
_relay_http_url = relay_http_url

log = logging.getLogger(__name__)


async def get_subscription_tier() -> str | None:
    """Fetch the subscription tier for this instance from the relay.

    Returns the tier string (e.g. "cloud", "ai", "team") or None if:
      - gateway is not configured (local install)
      - relay is unreachable
      - subscription is not active

    Never raises — callers treat None as "no restriction".
    """
    status = await get_quota_status()
    return status.get("tier") if status else None


async def get_quota_status() -> dict | None:
    """Fetch full AI quota status from the relay.

    Returns dict with keys: allowed, used, limit, topup_credits, resets_at, tier.
    Returns None if gateway not configured or relay unreachable.
    """
    if not settings.gateway_token or not get_session_token():
        return None

    url = f"{relay_http_url()}/quota/ai/status"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers=relay_session_headers())
        if r.status_code == 200:
            return r.json()
        log.warning("Quota status returned %s", r.status_code)
    except Exception as exc:
        log.warning("Failed to fetch quota status: %s", exc)
    return None
