# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Session token gate for cloud-gated API endpoints.

Cloud-gated routes (/ai/*, /backup/*, /connectors/*) require a valid
X-Session-Token header issued by the Celerp gateway after the hello_ack
handshake. This token is:
  - Short-lived (15 minutes, refreshed automatically by GatewayClient)
  - Tied to the instance's WebSocket connection
  - Never present in source code — issued server-side by relay.celerp.com

Effect: pointing an AI agent at the API without a live, licensed Celerp
instance connected to the gateway will return 401 on all cloud endpoints.
The core ERP API (/inventory, /docs, /crm, etc.) remains fully open.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from celerp.gateway.state import get_session_token

_SUBSCRIBE_BASE = "https://celerp.com/subscribe"


def _subscribe_url() -> str:
    from celerp.config import settings
    iid = settings.gateway_instance_id
    if iid:
        return f"{_SUBSCRIBE_BASE}?instance_id={iid}"
    return _SUBSCRIBE_BASE


def require_session_token(request: Request) -> None:
    """FastAPI dependency - raises 401 if instance has no active Cloud session.

    Checks in order:
      1. X-Session-Token header (external callers must provide the token)
      2. In-process gateway state (UI server proxies without the token;
         the API process holds the session token from the gateway WS handshake)

    If neither source has a valid session, raises 401 with actionable detail.
    """
    current = get_session_token()

    # Check header first (external API consumers)
    header_token = request.headers.get("X-Session-Token", "").strip()
    if header_token:
        if not current:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "This instance is not connected to Celerp Cloud. "
                    "Set GATEWAY_TOKEN in your environment and restart, "
                    "or enable Cloud in Settings > Cloud Relay."
                ),
            )
        if header_token != current:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Session token is invalid or has expired (tokens rotate every 2 hours). "
                    "Your Celerp app reconnects automatically - if this persists, "
                    "go to Settings > Cloud Relay and click Reconnect."
                ),
            )
        return  # Valid header token

    # No header - check in-process gateway state (same-origin UI requests)
    if current:
        return  # Gateway is connected, allow through

    # No session anywhere
    url = _subscribe_url()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            f"This endpoint requires an active Celerp Cloud subscription. "
            f"Subscribe at {url} then enable Cloud in Settings > Cloud Relay. "
            f"The core ERP API (/inventory, /docs, /crm, etc.) is always free."
        ),
    )
