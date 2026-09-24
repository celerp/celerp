# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Remote connector cleanup shared by disconnect and lifecycle operations."""
from __future__ import annotations

import logging

import httpx

from celerp.connectors.base import ConnectorContext

log = logging.getLogger(__name__)


class ConnectorRemoteCleanupError(RuntimeError):
    """Remote connector cleanup could not be confirmed."""


class ConnectorRemoteStateChangedError(ConnectorRemoteCleanupError):
    """Remote connector credentials changed during cleanup."""


async def _woocommerce_credential(
    company_id: str,
    webhook_ids: list[str],
) -> tuple[str, str | None] | None:
    if not webhook_ids:
        return None

    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            response = await client.get(
                f"{relay_http_url()}/tokens/woocommerce/access-token",
                headers=relay_session_headers(),
            )
    except Exception:
        log.warning(
            "WooCommerce webhook cleanup could not fetch credentials",
            exc_info=True,
        )
        return None
    if response.status_code != 200:
        log.warning(
            "WooCommerce webhook cleanup skipped after credential lookup returned %d",
            response.status_code,
        )
        return None

    data = response.json()
    credential = (str(data["access_token"]), data.get("store_handle"))
    try:
        from celerp.connectors.registry import get as get_connector

        await get_connector("woocommerce").deregister_webhooks(
            ConnectorContext(
                company_id=str(company_id),
                access_token=credential[0],
                store_handle=credential[1],
            ),
            webhook_ids,
        )
    except Exception:
        log.warning("WooCommerce webhook cleanup failed", exc_info=True)
    return credential


async def revoke_connector_remote_state(
    company_id: str,
    connector_name: str,
    *,
    webhook_ids: list[str] | None = None,
) -> None:
    """Revoke one connector credential and confirm the result."""
    from celerp.gateway.state import relay_http_url, relay_session_headers

    expected = None
    if connector_name == "woocommerce":
        expected = await _woocommerce_credential(
            str(company_id), list(webhook_ids or [])
        )

    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            if expected is not None:
                current = await client.get(
                    f"{relay_http_url()}/tokens/{connector_name}/access-token",
                    headers=relay_session_headers(),
                )
                if current.status_code == 200:
                    data = current.json()
                    observed = (
                        str(data["access_token"]),
                        data.get("store_handle"),
                    )
                    if observed != expected:
                        raise ConnectorRemoteStateChangedError(
                            "Connector credentials changed while disconnecting; retry."
                        )
                elif current.status_code == 404:
                    return
                else:
                    raise ConnectorRemoteCleanupError(
                        "Connector credential state could not be confirmed."
                    )

            response = await client.delete(
                f"{relay_http_url()}/tokens/{connector_name}",
                headers=relay_session_headers(),
            )
    except ConnectorRemoteCleanupError:
        raise
    except Exception as exc:
        raise ConnectorRemoteCleanupError(
            "Connector cleanup could not be confirmed."
        ) from exc

    if response.status_code not in (200, 404):
        raise ConnectorRemoteCleanupError(
            f"Connector cleanup returned {response.status_code}."
        )
