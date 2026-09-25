# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Remote connector cleanup shared by disconnect and lifecycle operations."""
from __future__ import annotations

import logging
import re

import httpx

from celerp.connectors.base import ConnectorContext

log = logging.getLogger(__name__)


class ConnectorRemoteCleanupError(RuntimeError):
    """Remote connector cleanup could not be confirmed."""


class ConnectorRemoteStateChangedError(ConnectorRemoteCleanupError):
    """The remote connection changed during cleanup."""


def _safe_connector_name(value: str) -> str:
    name = str(value)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None:
        raise ConnectorRemoteCleanupError("Connector name is invalid.")
    return name


async def _connection_revision(connector_name: str) -> str | None:
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            response = await client.get(
                f"{relay_http_url()}/tokens/{connector_name}/revision",
                headers=relay_session_headers(),
            )
    except Exception as exc:
        raise ConnectorRemoteCleanupError(
            "Connector state could not be confirmed."
        ) from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ConnectorRemoteCleanupError(
            "Connector state could not be confirmed."
        )
    try:
        data = response.json()
    except Exception as exc:
        raise ConnectorRemoteCleanupError(
            "Connector state could not be confirmed."
        ) from exc
    revision = data.get("revision") if isinstance(data, dict) else None
    if not isinstance(revision, str) or not revision:
        raise ConnectorRemoteCleanupError(
            "Connector state could not be confirmed."
        )
    return revision


async def _woocommerce_credential() -> tuple[str, str | None] | None:
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

    try:
        data = response.json()
    except Exception:
        log.warning("WooCommerce webhook cleanup received an invalid credential response")
        return None
    if not isinstance(data, dict):
        return None
    access_token = data.get("access_token")
    store_handle = data.get("store_handle")
    if not isinstance(access_token, str) or not access_token:
        return None
    if store_handle is not None and not isinstance(store_handle, str):
        return None
    return access_token, store_handle


async def _remove_woocommerce_webhooks(
    company_id: str, webhook_ids: list[str], *, force: bool
) -> None:
    """Remove every Celerp hook from the store while its credential still
    exists. Unconfirmed removal fails unless the disconnect is forced."""
    from celerp.connectors.registry import get as get_connector
    from celerp.connectors.woocommerce import webhook_delivery_url

    try:
        credential = await _woocommerce_credential()
        if credential is None:
            raise ConnectorRemoteCleanupError("Store credentials were not available.")
        await get_connector("woocommerce").deregister_webhooks(
            ConnectorContext(
                company_id=str(company_id),
                access_token=credential[0],
                store_handle=credential[1],
            ),
            webhook_delivery_url(),
            webhook_ids,
        )
    except Exception as exc:
        if not force:
            raise ConnectorRemoteCleanupError(
                "The store's webhooks could not be removed."
            ) from exc
        log.warning("WooCommerce webhook cleanup failed during a forced disconnect", exc_info=True)


async def revoke_connector_remote_state(
    company_id: str,
    connector_name: str,
    *,
    webhook_ids: list[str] | None = None,
    force: bool = False,
) -> None:
    """Disconnect one connector only if its remote state is unchanged. Store
    webhooks are removed first, while the credential still exists; `force`
    continues when that removal cannot be confirmed."""
    from celerp.gateway.state import relay_http_url, relay_session_headers

    connector_name = _safe_connector_name(connector_name)
    revision = await _connection_revision(connector_name)
    if revision is None:
        return

    if connector_name == "woocommerce":
        await _remove_woocommerce_webhooks(
            str(company_id), list(webhook_ids or []), force=force
        )
        if await _connection_revision(connector_name) != revision:
            raise ConnectorRemoteStateChangedError(
                "The connection changed while disconnecting; retry."
            )

    headers = {
        **relay_session_headers(),
        "X-Celerp-Connector-Revision": revision,
    }
    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            response = await client.delete(
                f"{relay_http_url()}/tokens/{connector_name}",
                headers=headers,
            )
    except Exception as exc:
        raise ConnectorRemoteCleanupError(
            "Connector cleanup could not be confirmed."
        ) from exc

    if response.status_code == 409:
        raise ConnectorRemoteStateChangedError(
            "The connection changed while disconnecting; retry."
        )
    if response.status_code not in (200, 404):
        raise ConnectorRemoteCleanupError(
            f"Connector cleanup returned {response.status_code}."
        )
