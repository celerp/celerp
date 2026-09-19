# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Gateway quota status client.

Reads AI quota status from the relay for display and tier checks. Quota is
enforced by the gateway as a query runs; this module only reports it.
"""

from __future__ import annotations

import logging

from celerp.config import settings
from celerp.gateway.state import get_session_token, relay_http_url

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
    """Fetch authoritative AI quota without requiring a live WS session.

    None means this install has no cloud identity. Explicit disconnect and relay
    ambiguity are returned as distinct sentinels so UI never turns transport
    state into a purchase decision.
    """
    from celerp.services.cloud_entitlement import authenticated_request, stored_api_key, sync_existing_entitlement
    if settings.cloud_disconnected:
        return {"disconnected": True}
    if not settings.gateway_instance_id:
        return None
    if not await stored_api_key():
        return None
    try:
        response = await authenticated_request("GET", "/quota/ai/status", total_s=5.0)
    except Exception as exc:
        log.warning("Failed to fetch quota status: %s", exc)
        return {"unknown": True}
    if response is None:
        return None
    if response.status_code != 200:
        log.warning("Quota status returned %s", response.status_code)
        return {"unknown": True}
    data = response.json()
    if not isinstance(data, dict):
        return {"unknown": True}
    tier = str(data.get("tier") or "free")
    if tier in ("cloud", "ai", "team") and not get_session_token():
        await sync_existing_entitlement()
    return data
