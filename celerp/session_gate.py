# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

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


async def require_session_token(request: Request) -> None:
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
                    "This instance is not connected to Celerp Connect. "
                    "Set GATEWAY_TOKEN in your environment and restart, "
                    "or connect web access under Settings > Web Access."
                ),
            )
        if header_token != current:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Session token is invalid or has expired (tokens rotate every 2 hours). "
                    "Your Celerp app reconnects automatically - if this persists, "
                    "go to Settings > Web Access and click Reconnect."
                ),
            )
        return  # Valid header token

    # No header: same-origin callers may recover transport from durable authority.
    if current:
        return
    from celerp.config import settings
    if not settings.cloud_disconnected:
        from celerp.services.cloud_entitlement import sync_existing_entitlement
        await sync_existing_entitlement()
        if get_session_token():
            return

    # Session state is transport/authentication state, never a billing verdict.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "No active Celerp Connect session. Connect or reconnect web access "
            "under Settings > Web Access, then try again."
        ),
    )
