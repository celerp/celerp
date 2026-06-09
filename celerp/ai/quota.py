# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Gateway quota client.

Calls /quota/ai/consume on the relay to enforce per-instance AI query limits.
If the gateway is not configured, quota checks are skipped (self-hosted installs with no
cloud subscription run unlimited locally - the gate lives in the cloud).
"""

from __future__ import annotations

import logging

import httpx

from celerp.config import settings
from celerp.gateway.state import (
    get_session_token,
    relay_http_url,
    relay_session_headers,
    relay_subscribe_url,
)

# Private aliases so tests (and any internal callers) can import these from
# this module rather than reaching into celerp.gateway.state directly.
_relay_http_url = relay_http_url


def _subscribe_url() -> str:
    """Return the celerp.com subscribe URL anchored to the AI section."""
    return relay_subscribe_url(anchor="ai")

log = logging.getLogger(__name__)

# Quota bypass monitoring: count consecutive relay failures
_unconfirmed: int = 0
_UNCONFIRMED_THRESHOLD: int = 10


async def check_ai_quota(credits: int = 1) -> None:
    """Consume AI quota credits from the relay.

    credits: number of credits to consume (default 1 for pure text queries).
    Raises HTTPException(402) if quota exceeded.
    Raises nothing if gateway is not configured (local-only install).
    """
    if not settings.gateway_token or not get_session_token():
        # No gateway → no quota enforcement (local install)
        log.debug("Quota check skipped: gateway not configured.")
        return

    url = f"{relay_http_url()}/quota/ai/consume"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(url, headers=relay_session_headers())
    except Exception as exc:
        # Network failure - allow through (don't block user on relay outage)
        global _unconfirmed
        _unconfirmed += 1
        log.warning("Quota check failed (%d unconfirmed): %s. Allowing query.", _unconfirmed, exc)
        if _unconfirmed >= _UNCONFIRMED_THRESHOLD:
            log.error("Quota bypass threshold reached: %d consecutive failures. Check relay connectivity.", _unconfirmed)
        return

    if r.status_code == 200:
        _unconfirmed = 0
        return

    if r.status_code == 429:
        from fastapi import HTTPException
        detail = r.json().get("detail", {})
        raise HTTPException(
            status_code=402,  # Payment Required
            detail={
                "code": "quota_exceeded",
                "message": detail.get("message", "AI query quota exceeded"),
                "used": detail.get("used", 0),
                "limit": detail.get("limit", 0),
                "resets_at": detail.get("resets_at", ""),
                "upgrade_url": relay_subscribe_url(anchor="ai"),
            },
        )

    if r.status_code == 401:
        # Session expired — allow through (client will reconnect and retry)
        log.warning("Quota check 401: session token expired. Allowing query.")
        return

    log.warning("Quota check unexpected status %d — allowing query.", r.status_code)


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
