# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Build connector context for background synchronization."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

# Connectors whose API calls go through the relay. Their context carries the
# connected account but no credential.
_RELAY_CALL_CONNECTORS = frozenset({"xero"})



class ConnectorUpgradeRequired(Exception):
    """The relay serves this connector only to a newer Celerp."""


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from celerp.connectors.base import ConnectorContext


async def fetch_context(
    company_id: str,
    connector_name: str,
    *,
    ownership_session: "AsyncSession | None" = None,
) -> "ConnectorContext | None":
    import httpx

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.ownership import connector_owned_by_company
    from celerp.db import get_session_ctx
    from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers

    if not get_session_token():
        return None

    if ownership_session is None:
        async with get_session_ctx() as session:
            owned = await connector_owned_by_company(
                session, company_id, connector_name
            )
    else:
        owned = await connector_owned_by_company(
            ownership_session, company_id, connector_name
        )
    if not owned:
        log.warning(
            "connector context unavailable for %s and company %s",
            connector_name, company_id,
        )
        return None
    relay_calls = connector_name in _RELAY_CALL_CONNECTORS
    endpoint = "context" if relay_calls else "access-token"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{relay_http_url()}/tokens/{connector_name}/{endpoint}",
                headers=relay_session_headers(),
            )
    except Exception as exc:
        log.warning("relay token fetch for %s failed: %s", connector_name, exc)
        return None
    if r.status_code == 426:
        from celerp.connectors.registry import get as get_connector

        raise ConnectorUpgradeRequired(
            f"Update Celerp to continue syncing {get_connector(connector_name).display_name}."
        )
    if r.status_code != 200:
        log.debug("relay token fetch for %s returned %d", connector_name, r.status_code)
        return None
    try:
        data = r.json()
    except ValueError as exc:
        log.warning("relay token fetch for %s failed: %s", connector_name, exc)
        return None

    return ConnectorContext(
        company_id=company_id,
        access_token="" if relay_calls else data["access_token"],
        store_handle=data.get("store_handle"),
        extra=data.get("extra"),
    )
