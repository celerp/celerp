# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fetch a short-lived connector access token from the relay.

Autonomous syncs (the reconciliation scheduler and webhook-triggered syncs) run
inside the core process, so unlike the manual sync route — where the UI passes
the token in the request body — they must fetch the token themselves. The relay
holds the encrypted token and returns a short-lived copy to the authenticated
instance. Returns None when there is no relay session (e.g. a self-hosted
instance not connected to Celerp Connect), in which case the caller skips.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from celerp.connectors.base import ConnectorContext


async def fetch_context(company_id: str, connector_name: str) -> "ConnectorContext | None":
    import httpx

    from celerp.connectors.base import ConnectorContext
    from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers

    if not get_session_token():
        return None  # no cloud session → no relay token available
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{relay_http_url()}/tokens/{connector_name}/access-token",
                headers=relay_session_headers(),
            )
        if r.status_code != 200:
            log.debug("relay token fetch for %s returned %d", connector_name, r.status_code)
            return None
        data = r.json()
    except Exception as exc:
        log.warning("relay token fetch for %s failed: %s", connector_name, exc)
        return None

    return ConnectorContext(
        company_id=company_id,
        access_token=data["access_token"],
        store_handle=data.get("store_handle"),
        extra=data.get("extra"),
    )
