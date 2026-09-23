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
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.permissions import require_permission
from celerp.session_gate import require_session_token

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/connectors",
    tags=["connectors"],
    dependencies=[Depends(get_current_user), Depends(require_session_token)],
)
local_router = APIRouter(
    prefix="/connector-items", tags=["connectors"],
    dependencies=[Depends(get_current_user)],
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
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Validate API-key credentials against the store, then store them on the relay.

    Returns {"ok": True} on success, else {"ok": False, "error": <code>, "detail": str}
    with error codes: store_rejected, store_unreachable, subscription_required,
    relay_not_https, relay_error.
    """
    import httpx
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        connector = connectors.get(connector_name)
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

    from celerp.connectors.base import ConnectorCategory, SyncFrequency
    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        claim_connector_ownership,
        lock_connector_operation,
    )
    category = getattr(connector.category, "value", connector.category)
    default_frequency = (
        SyncFrequency.REALTIME.value
        if category == ConnectorCategory.WEBSITE.value
        else SyncFrequency.MANUAL.value
    )
    try:
        await claim_connector_ownership(
            session,
            company_id,
            connector_name,
            default_sync_frequency=default_frequency,
        )
        # This ownership row is the authorization boundary for an installation-wide
        # relay credential. Persist it before the remote write so a crash cannot
        # leave a live credential without an owning ERP company.
        await session.commit()
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        await lock_connector_operation(
            session, company_id, connector_name, require_owner=True
        )
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
        await session.rollback()
        return {"ok": False, "error": "relay_error", "detail": str(exc)}

    if r.status_code == 402:
        await session.rollback()
        return {"ok": False, "error": "subscription_required", "detail": ""}
    if r.status_code != 200:
        await session.rollback()
        return {"ok": False, "error": "relay_error", "detail": f"relay returned {r.status_code}"}
    await session.commit()
    return {"ok": True}


@router.delete("/{connector_name}/credentials")
async def revoke_credentials(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Disconnect one company-owned connector and clear its local sync state."""
    import httpx

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        lock_connector_operation,
        release_connector_ownership,
    )
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        config = await lock_connector_operation(
            session, company_id, connector_name, require_owner=True
        )
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    expected_credential: tuple[str, str | None] | None = None
    if connector_name == "woocommerce" and config and config.webhook_ids:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                token_response = await c.get(
                    f"{relay_http_url()}/tokens/{connector_name}/access-token",
                    headers=relay_session_headers(),
                )
            if token_response.status_code != 200:
                await session.rollback()
                return {
                    "ok": False,
                    "error": "relay_error",
                    "detail": f"credential lookup returned {token_response.status_code}",
                }
            token_data = token_response.json()
            expected_credential = (
                str(token_data["access_token"]),
                token_data.get("store_handle"),
            )
            from celerp.connectors.woocommerce import WooCommerceConnector
            ctx = ConnectorContext(
                company_id=str(company_id),
                access_token=expected_credential[0],
                store_handle=expected_credential[1],
            )
            await WooCommerceConnector().deregister_webhooks(
                ctx, config.webhook_ids
            )
            # The remote hooks are confirmed gone. Persist that fact before
            # revoking the credential so a retry never needs credentials merely
            # to repeat cleanup that already succeeded.
            config.webhook_ids = []
            config.webhook_secret = None
            await session.commit()
            config = await lock_connector_operation(
                session, company_id, connector_name, require_owner=True
            )
        except Exception as exc:
            await session.rollback()
            return {"ok": False, "error": "relay_error", "detail": str(exc)}

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            if expected_credential is not None:
                current = await c.get(
                    f"{relay_http_url()}/tokens/{connector_name}/access-token",
                    headers=relay_session_headers(),
                )
                if current.status_code == 200:
                    current_data = current.json()
                    current_credential = (
                        str(current_data["access_token"]),
                        current_data.get("store_handle"),
                    )
                    if current_credential != expected_credential:
                        await session.rollback()
                        return {
                            "ok": False,
                            "error": "connection_changed",
                            "detail": "Connector credentials changed while disconnecting; retry.",
                        }
                elif current.status_code != 404:
                    await session.rollback()
                    return {
                        "ok": False,
                        "error": "relay_error",
                        "detail": f"credential lookup returned {current.status_code}",
                    }

            response = await c.delete(
                f"{relay_http_url()}/tokens/{connector_name}",
                headers=relay_session_headers(),
            )
    except Exception as exc:
        await session.rollback()
        return {"ok": False, "error": "relay_error", "detail": str(exc)}

    if response.status_code not in (200, 404):
        await session.rollback()
        return {
            "ok": False,
            "error": "relay_error",
            "detail": f"relay returned {response.status_code}",
        }

    try:
        if connector_name in {"shopify", "woocommerce"}:
            from celerp_inventory.services import detach_external_links_for_platform
            await detach_external_links_for_platform(
                session, company_id, connector_name
            )
        await release_connector_ownership(session, company_id, connector_name)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        return {"ok": False, "error": "local_cleanup_failed", "detail": str(exc)}
    return {"ok": True}


@router.get("/{connector_name}/access-token")
async def connector_access_token(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a short-lived decrypted access token from the relay.

    Returns the relay payload ({access_token, store_handle, ...}) on success,
    else {"error": <code>, "detail": str} with error codes: not_connected,
    session_invalid, subscription_required, relay_error.
    """
    import httpx
    from celerp.connectors.ownership import connector_owned_by_company
    from celerp.gateway.state import relay_http_url, relay_session_headers

    if not await connector_owned_by_company(session, company_id, connector_name):
        return {"error": "not_connected", "detail": "Connector is not owned by the current company."}

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



class ItemSyncRequest(BaseModel):
    entity_ids: list[str]
    enable: bool = True


@local_router.post("/{connector_name}/sync")
async def set_item_sync(
    connector_name: str, payload: ItemSyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user), _: None = require_permission("adjust_inventory"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Enable or disable catalog product synchronization for one connected channel."""
    if connector_name not in {"shopify", "woocommerce"}:
        raise HTTPException(status_code=404, detail="Unsupported catalog connector")
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    import sqlalchemy as sa
    from celerp.events.engine import emit_event
    from celerp.models.connector_config import ConnectorConfig
    from celerp_inventory.services import external_link_for_state, resolve_catalog_anchor_for_item, set_external_link_state
    configured = (await session.execute(sa.select(ConnectorConfig.id).where(
        ConnectorConfig.company_id == str(company_id),
        ConnectorConfig.connector == connector_name,
    ))).scalar_one_or_none()
    if configured is None:
        raise HTTPException(status_code=409, detail=f"{connector_name} is not connected")
    anchors: dict[str, object] = {}
    errors: list[str] = []
    for entity_id in dict.fromkeys(payload.entity_ids):
        try:
            anchor = await resolve_catalog_anchor_for_item(session, company_id, entity_id)
            anchors[anchor.entity_id] = anchor
        except ValueError as exc:
            errors.append(f"{entity_id}: {exc}")
    updated = 0
    if connector_name == "shopify":
        for anchor_id, anchor in anchors.items():
            state = anchor.state or {}
            linked = external_link_for_state(state, "shopify")
            if payload.enable and not linked:
                errors.append(f"{state.get('sku') or anchor_id}: not linked to a Shopify product; import it from Shopify first")
                continue
            desired = bool(payload.enable)
            if bool(anchor.is_sync_to_shopify) == desired: continue
            await emit_event(session, company_id=company_id, entity_id=anchor_id, entity_type="item",
                event_type="shop.sync.enabled" if desired else "shop.sync.disabled", data={},
                actor_id=user.id, location_id=None, source="connector_ui",
                idempotency_key=str(__import__("uuid").uuid4()), metadata_={})
            updated += 1
        await session.commit()
    elif not payload.enable:
        for anchor_id, anchor in anchors.items():
            link = external_link_for_state(anchor.state or {}, "woocommerce")
            if not link or link.get("sync_enabled") is False: continue
            await set_external_link_state(session, company_id, anchor_id, "woocommerce",
                sync_enabled=False, actor_id=user.id, source="connector_ui")
            updated += 1
        await session.commit()
    else:
        from celerp.connectors.ownership import lock_connector_operation
        from celerp.connectors.relay_token import fetch_context
        from celerp.connectors.woocommerce import WooCommerceConnector
        await lock_connector_operation(
            session, company_id, "woocommerce", require_owner=True
        )
        ctx = await fetch_context(str(company_id), "woocommerce")
        if ctx is None:
            raise HTTPException(status_code=409, detail="WooCommerce is connected but its credentials are not currently available")
        for anchor_id, anchor in anchors.items():
            try:
                await WooCommerceConnector().ensure_product_link(ctx, anchor_id, actor_id=user.id)
                updated += 1
            except Exception as exc:
                errors.append(f"{(anchor.state or {}).get('sku') or anchor_id}: {exc}")
    return {"updated": updated, "enabled": payload.enable, "errors": errors}


# ── Module entry point ────────────────────────────────────────────────────────

def setup_api_routes(app) -> None:
    """Called by the module loader to register connector routes."""
    app.include_router(router)
    app.include_router(local_router)
