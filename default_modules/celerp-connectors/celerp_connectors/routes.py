# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Connector routes — /connectors/*

Cloud-gated: all endpoints require both:
  - User authentication (get_current_user)
  - Active Celerp Connect subscription (require_session_token via X-Session-Token)

Token flow (relay model):
  The client authenticates with relay.celerp.com to obtain a short-lived
  access_token, then passes it in the request body here. OAuth credentials
  never touch the core instance.

Self-hosted / bring-your-own-token:
  Pass access_token + store_handle directly. Core does not validate origin.
  Session token is still required (your instance must be connected to Celerp Connect).
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import celerp.connectors as connectors
from celerp.connectors.base import ConnectorContext, SyncEntity
from celerp.db import get_session
from celerp.services.auth import get_current_company_id, get_current_user, require_admin
from celerp.session_gate import require_session_token

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/connectors",
    tags=["connectors"],
    dependencies=[Depends(get_current_user), Depends(require_session_token)],
)


# ── Request / Response schemas ────────────────────────────────────────────────

class SyncRequest(BaseModel):
    entity: SyncEntity
    access_token: str
    store_handle: str | None = None   # required for Shopify; optional for others
    extra: dict | None = None


class ConnectorInfo(BaseModel):
    name: str
    display_name: str
    supported_entities: list[SyncEntity]
    direction: str


class SyncResponse(BaseModel):
    connector: str
    entity: SyncEntity
    direction: str
    created: int
    updated: int
    skipped: int
    errors: list[str] | None = None
    ok: bool


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ConnectorInfo])
async def list_connectors(
    _company_id: Annotated[str, Depends(get_current_company_id)],
) -> list[ConnectorInfo]:
    return [
        ConnectorInfo(
            name=c.name,
            display_name=c.display_name,
            supported_entities=c.supported_entities,
            direction=c.direction.value,
        )
        for c in connectors.all_connectors()
    ]


@router.post("/{connector_name}/sync", response_model=SyncResponse)
async def trigger_sync(
    connector_name: str,
    payload: SyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    session: AsyncSession = Depends(get_session),
) -> SyncResponse:
    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if payload.entity not in connector.supported_entities:
        raise HTTPException(
            status_code=400,
            detail=f"{connector.display_name} does not support entity '{payload.entity}'",
        )

    ctx = ConnectorContext(
        company_id=company_id,
        access_token=payload.access_token,
        store_handle=payload.store_handle,
        extra=payload.extra,
    )

    # Route through run_sync so the manual path gets the same audit row, concurrency
    # guard, and incremental watermark as the scheduled/webhook paths.
    from celerp.connectors.sync_runner import run_sync
    try:
        result = await run_sync(connector, ctx, payload.entity.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("connector sync error: %s/%s", connector_name, payload.entity)
        raise HTTPException(status_code=502, detail=f"Connector error: {exc}")

    return SyncResponse(
        connector=connector_name,
        entity=result.entity,
        direction=result.direction.value,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
        ok=result.ok,
    )


# ── Credential management (API-key platforms) ────────────────────────────────
# These endpoints run in the API process, which holds the live relay session
# from the gateway WebSocket handshake. The UI process has no relay session of
# its own, so all relay credential operations must proxy through here.
# Credentials are stored encrypted on the relay only - never persisted locally.

class ApiKeyCredentials(BaseModel):
    consumer_key: str
    consumer_secret: str
    store_url: str | None = None


def _relay_https_error() -> dict | None:
    """Refuse to send credentials to a non-HTTPS relay (dev escape via env)."""
    import os
    from celerp.gateway.state import relay_http_url

    if relay_http_url().startswith("https://") or os.environ.get("CELERP_ALLOW_HTTP_RELAY"):
        return None
    return {"ok": False, "error": "relay_not_https",
            "detail": "Relay URL must use HTTPS. Set CELERP_ALLOW_HTTP_RELAY=1 for development."}


@router.post("/{connector_name}/credentials")
async def store_credentials(
    connector_name: str,
    payload: ApiKeyCredentials,
    _=Depends(require_admin),
) -> dict:
    """Validate API-key credentials against the store, then store them on the relay.

    Returns {"ok": True} on success, else {"ok": False, "error": <code>, "detail": str}
    with error codes: store_rejected, store_unreachable, subscription_required,
    relay_not_https, relay_error.
    """
    import httpx
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if (err := _relay_https_error()) is not None:
        return err

    # Canonicalise (no trailing slash) so the stored handle matches the store
    # URL WooCommerce sends in webhook deliveries (X-WC-Webhook-Source).
    store_url = (payload.store_url or "").strip().rstrip("/")

    # Validate the credentials against the store BEFORE storing them, so a bad
    # key/secret/URL fails here with a clear message instead of silently failing
    # on the first background sync.
    if connector_name == "woocommerce":
        import os
        if not store_url:
            return {"ok": False, "error": "store_unreachable", "detail": "Store URL is required."}
        # The key/secret go to the store as Basic Auth, so cleartext http would
        # expose them; http is allowed only behind the dev override.
        allow_http = store_url.startswith("http://") and bool(os.environ.get("CELERP_ALLOW_HTTP_STORE"))
        if not (store_url.startswith("https://") or allow_http):
            return {"ok": False, "error": "store_unreachable",
                    "detail": "Store URL must use https:// (API keys are sent as Basic Auth)."}
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                probe = await c.get(
                    f"{store_url}/wp-json/wc/v3/products",
                    params={"per_page": 1},
                    auth=(payload.consumer_key, payload.consumer_secret),
                )
            if probe.status_code == 401:
                return {"ok": False, "error": "store_rejected",
                        "detail": "store rejected the consumer key/secret (401)"}
            probe.raise_for_status()
        except Exception as exc:
            return {"ok": False, "error": "store_unreachable", "detail": str(exc)}

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{relay_http_url()}/tokens/{connector_name}",
                json={
                    "consumer_key": payload.consumer_key,
                    "consumer_secret": payload.consumer_secret,
                    "store_url": store_url or None,
                },
                headers=relay_session_headers(),
            )
    except Exception as exc:
        return {"ok": False, "error": "relay_error", "detail": str(exc)}

    if r.status_code == 402:
        return {"ok": False, "error": "subscription_required", "detail": ""}
    if r.status_code != 200:
        return {"ok": False, "error": "relay_error", "detail": f"relay returned {r.status_code}"}
    return {"ok": True}


@router.delete("/{connector_name}/credentials")
async def revoke_credentials(
    connector_name: str,
    _=Depends(require_admin),
) -> dict:
    """Revoke stored connector credentials on the relay (disconnect).

    A 404 from the relay means nothing was stored - already disconnected, so ok.
    """
    import httpx
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.delete(
                f"{relay_http_url()}/tokens/{connector_name}",
                headers=relay_session_headers(),
            )
    except Exception as exc:
        return {"ok": False, "error": "relay_error", "detail": str(exc)}

    if r.status_code in (200, 404):
        return {"ok": True}
    return {"ok": False, "error": "relay_error", "detail": f"relay returned {r.status_code}"}


@router.get("/{connector_name}/access-token")
async def connector_access_token(
    connector_name: str,
    _=Depends(require_admin),
) -> dict:
    """Return a short-lived decrypted access token from the relay.

    Returns the relay payload ({access_token, store_handle, ...}) on success,
    else {"error": <code>, "detail": str} with error codes: not_connected,
    session_invalid, subscription_required, relay_error.
    """
    import httpx
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{relay_http_url()}/tokens/{connector_name}/access-token",
                headers=relay_session_headers(),
            )
    except Exception as exc:
        return {"error": "relay_error", "detail": str(exc)}

    if r.status_code == 404:
        return {"error": "not_connected",
                "detail": f"No {connector_name} connection found. Connect the platform first."}
    if r.status_code == 401:
        return {"error": "session_invalid",
                "detail": f"{connector_name} token expired. Please reconnect."}
    if r.status_code == 402:
        return {"error": "subscription_required",
                "detail": "An active subscription is required to sync connectors."}
    if r.status_code != 200:
        return {"error": "relay_error", "detail": f"relay returned {r.status_code}"}
    return r.json()


# ── Module entry point ────────────────────────────────────────────────────────

def setup_api_routes(app) -> None:
    """Called by the module loader to register connector routes."""
    app.include_router(router)
