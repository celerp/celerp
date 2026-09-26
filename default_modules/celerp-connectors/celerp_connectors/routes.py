# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Connector routes - /connectors/*."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import celerp.connectors as connectors
from celerp.connectors.base import SyncDirection, SyncEntity
from celerp.db import get_session
from celerp.services.auth import (
    get_current_company_id,
    get_current_user,
    require_install_owner,
)
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


async def _connector_context(company_id: str, connector_name: str, **kwargs):
    from celerp.connectors.relay_token import ConnectorUpgradeRequired, fetch_context

    try:
        return await fetch_context(company_id, connector_name, **kwargs)
    except ConnectorUpgradeRequired as exc:
        raise HTTPException(status_code=426, detail=str(exc)) from exc


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
    _: None = require_permission("manage_integrations"),
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

    from celerp.models.connector_config import ConnectorConfig
    from sqlalchemy import select
    config = await session.scalar(
        select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector_name,
        )
    )
    if config is None:
        raise HTTPException(status_code=409, detail="Connector is not connected")
    direction = SyncDirection(config.direction)

    ctx = await _connector_context(str(company_id), connector_name)
    if ctx is None:
        raise HTTPException(status_code=409, detail="Connector is not connected")

    # Route through run_sync so the manual path gets the same audit row, concurrency
    # guard, and incremental watermark as the scheduled/webhook paths.
    from celerp.connectors.sync_runner import run_sync
    try:
        result = await run_sync(
            connector, ctx, payload.entity.value, direction=direction
        )
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


@router.post("/{connector_name}/sync-plan")
async def trigger_sync_plan(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    from celerp.models.connector_config import ConnectorConfig
    from sqlalchemy import select
    config = await session.scalar(
        select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector_name,
        )
    )
    if config is None:
        raise HTTPException(status_code=409, detail="Connector is not connected")
    direction = SyncDirection(config.direction)

    ctx = await _connector_context(str(company_id), connector_name)
    if ctx is None:
        raise HTTPException(status_code=409, detail="Connector is not connected")

    from celerp.connectors.sync_runner import run_connector_sync
    from celerp.services.background import spawn_background

    async def _run() -> None:
        try:
            await run_connector_sync(connector, ctx, direction=direction)
        except Exception:
            log.exception("connector sync plan failed: %s", connector_name)

    spawn_background(_run())
    return {"ok": True}


# ── Credential management (API-key platforms) ────────────────────────────────

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
    with error codes: store_rejected, store_unreachable, store_changed,
    subscription_required, relay_not_https, relay_error.
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
        allow_http = bool(os.environ.get("CELERP_ALLOW_HTTP_STORE"))
        try:
            from celerp.services.outbound_url import validate_public_base_url
            store_url = await validate_public_base_url(
                store_url,
                allow_http=allow_http,
                reject_query=True,
                reject_fragment=True,
            )
        except ValueError as exc:
            return {"ok": False, "error": "store_unreachable", "detail": str(exc)}
        try:
            from celerp.services.outbound_url import fetch_public_bytes
            probe = await fetch_public_bytes(
                f"{store_url}/wp-json/wc/v3/products",
                max_bytes=64 * 1024,
                timeout=8.0,
                allow_http=allow_http,
                auth=(payload.consumer_key, payload.consumer_secret),
                params={"per_page": 1, "_fields": "id"},
            )
            if probe.status_code == 401:
                return {"ok": False, "error": "store_rejected",
                        "detail": "store rejected the consumer key/secret (401)"}
            if probe.status_code >= 400:
                return {
                    "ok": False,
                    "error": "store_unreachable",
                    "detail": f"store returned {probe.status_code}",
                }
        except Exception as exc:
            return {"ok": False, "error": "store_unreachable", "detail": str(exc)}

    from celerp.connectors.base import ConnectorCategory, ConnectorContext, SyncFrequency
    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        ConnectorStoreChangedError,
        bind_connector_store,
        claim_connector_ownership,
        lock_connector_operation,
        release_connector_ownership,
    )
    category = getattr(connector.category, "value", connector.category)
    default_frequency = (
        SyncFrequency.REALTIME.value
        if category == ConnectorCategory.WEBSITE.value
        else SyncFrequency.MANUAL.value
    )
    try:
        config, ownership_created = await claim_connector_ownership(
            session,
            company_id,
            connector_name,
            default_sync_frequency=default_frequency,
            report_created=True,
        )
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not ownership_created:
        await session.rollback()
        return {
            "ok": False,
            "error": "already_connected",
            "detail": "Disconnect the existing connector before changing credentials.",
        }

    try:
        await session.commit()
        config = await lock_connector_operation(
            session, company_id, connector_name, require_owner=True, exclusive=True
        )
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        return {"ok": False, "error": "local_cleanup_failed", "detail": str(exc)}

    from celerp.connectors.remote_state import (
        ConnectorRemoteCleanupError,
        revoke_connector_remote_state,
    )

    async def _failure(
        error: str,
        detail: str,
        *,
        webhook_ids: list[str] | None = None,
    ) -> dict:
        try:
            await revoke_connector_remote_state(
                company_id,
                connector_name,
                webhook_ids=webhook_ids,
            )
        except ConnectorRemoteCleanupError:
            await session.rollback()
            suffix = (
                " The connection could not be cleaned up automatically; "
                "disconnect it before retrying."
            )
            return {"ok": False, "error": error, "detail": (detail + suffix).strip()}

        await session.rollback()
        try:
            await release_connector_ownership(
                session, company_id, connector_name
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            return {
                "ok": False,
                "error": "local_cleanup_failed",
                "detail": str(exc),
            }
        return {"ok": False, "error": error, "detail": detail}

    store_ctx = ConnectorContext(
        company_id=str(company_id),
        access_token=f"{payload.consumer_key}:{payload.consumer_secret}",
        store_handle=store_url or None,
    )
    try:
        await bind_connector_store(session, company_id, connector, store_ctx)
    except ConnectorStoreChangedError as exc:
        return await _failure("store_changed", str(exc))

    try:
        # Remove any stale remote state before starting the new generation.
        await revoke_connector_remote_state(company_id, connector_name)
    except ConnectorRemoteCleanupError as exc:
        await session.rollback()
        return {
            "ok": False,
            "error": "relay_error",
            "detail": f"{exc} Disconnect it before retrying.",
        }

    try:
        if connector_name in {"shopify", "woocommerce"}:
            from celerp_inventory.services import detach_external_links_for_platform
            await detach_external_links_for_platform(
                session, company_id, connector_name
            )
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            r = await client.post(
                f"{relay_http_url()}/tokens/{connector_name}",
                json={
                    "consumer_key": payload.consumer_key,
                    "consumer_secret": payload.consumer_secret,
                    "store_url": store_url or None,
                },
                headers=relay_session_headers(),
            )
    except Exception as exc:
        return await _failure("relay_error", str(exc))

    if r.status_code == 402:
        return await _failure("subscription_required", "")
    if r.status_code != 200:
        return await _failure(
            "relay_error", f"relay returned {r.status_code}"
        )

    webhook_ids: list[str] = []
    if connector_name == "woocommerce":
        import secrets

        from celerp.connectors.woocommerce import webhook_delivery_url

        secret = secrets.token_hex(32)
        delivery_url = webhook_delivery_url()
        try:
            # Hooks left by an earlier connection carry an old secret.
            await connector.deregister_webhooks(store_ctx, delivery_url, [])
            webhook_ids = await connector.register_webhooks(
                store_ctx, delivery_url, secret=secret
            )
        except Exception as exc:
            return await _failure(
                "store_unreachable",
                f"Webhook setup failed: {exc}",
                webhook_ids=webhook_ids,
            )

        config.webhook_secret = secret
        config.webhook_ids = webhook_ids

    try:
        await session.commit()
    except Exception as exc:
        return await _failure(
            "local_cleanup_failed",
            str(exc),
            webhook_ids=webhook_ids,
        )
    return {"ok": True}


@router.delete("/{connector_name}/credentials")
async def revoke_credentials(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
    force: bool = False,
) -> dict:
    """Disconnect one company-owned connector and clear its local sync state.

    A normal disconnect first confirms the store's webhooks are removed. With
    `force`, it disconnects even when that cannot be confirmed and records
    that it was forced. Imported records keep their store of origin either way.
    """
    from celerp.connectors.ownership import (
        RESET_STATUS_DISCONNECTED,
        RESET_STATUS_FORCED,
        ConnectorOwnershipAmbiguousError,
        ConnectorOwnershipError,
        connector_release_scope,
        lock_connector_operation,
        release_connector_ownership,
    )

    try:
        try:
            config = await lock_connector_operation(
                session, company_id, connector_name, require_owner=True, exclusive=True
            )
            webhook_ids = list(config.webhook_ids or [])
        except ConnectorOwnershipAmbiguousError:
            rows = await connector_release_scope(session, company_id, connector_name)
            webhook_ids = list(dict.fromkeys(
                wid for row in rows for wid in (row.webhook_ids or [])
            ))
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if failure := await _revoke_remote(
        session, company_id, connector_name, webhook_ids, force=force
    ):
        return failure

    try:
        if connector_name in {"shopify", "woocommerce"}:
            from celerp_inventory.services import detach_external_links_for_platform
            await detach_external_links_for_platform(
                session, company_id, connector_name
            )
        await release_connector_ownership(
            session, company_id, connector_name,
            status=RESET_STATUS_FORCED if force else RESET_STATUS_DISCONNECTED,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        return {"ok": False, "error": "local_cleanup_failed", "detail": str(exc)}
    return {"ok": True}


async def _revoke_remote(
    session: AsyncSession, company_id: str, connector_name: str,
    webhook_ids: list[str], *, force: bool,
) -> dict | None:
    """Disconnect the connector remotely. Returns the failure response, with
    the local transaction rolled back, or None when it is disconnected."""
    from celerp.connectors.remote_state import (
        ConnectorRemoteCleanupError,
        ConnectorRemoteStateChangedError,
        revoke_connector_remote_state,
    )

    try:
        await revoke_connector_remote_state(
            company_id, connector_name, webhook_ids=webhook_ids, force=force,
        )
    except ConnectorRemoteStateChangedError as exc:
        await session.rollback()
        return {"ok": False, "error": "connection_changed", "detail": str(exc)}
    except ConnectorRemoteCleanupError as exc:
        await session.rollback()
        return {"ok": False, "error": "relay_error", "detail": str(exc)}
    return None


@router.delete("/{connector_name}/unassigned")
async def reset_unassigned_connector(
    connector_name: str,
    _owner=Depends(require_install_owner),
    session: AsyncSession = Depends(get_session),
    force: bool = False,
) -> dict:
    """Disconnect a connector set up before companies had their own
    connectors, for the whole installation. No company becomes its owner;
    the company that should use it reconnects it afterwards."""
    from celerp.config import ensure_instance_id
    from celerp.connectors.ownership import (
        RESET_STATUS_DISCONNECTED,
        RESET_STATUS_FORCED,
        ConnectorOwnershipError,
        lock_unassigned_connector,
        release_unassigned_connector,
    )

    try:
        connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        rows = await lock_unassigned_connector(session, connector_name)
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    webhook_ids = list(dict.fromkeys(
        wid for row in rows for wid in (row.webhook_ids or [])
    ))

    if failure := await _revoke_remote(
        session, ensure_instance_id(), connector_name, webhook_ids, force=force
    ):
        return failure

    try:
        await release_unassigned_connector(
            session, connector_name,
            status=RESET_STATUS_FORCED if force else RESET_STATUS_DISCONNECTED,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        return {"ok": False, "error": "local_cleanup_failed", "detail": str(exc)}
    return {"ok": True}


class ReconciledRequest(BaseModel):
    signature: str


async def _set_order_reconciled(
    session: AsyncSession, company_id, order_id: str, signature: str | None, actor_id,
) -> dict:
    """Mark (signature) or unmark (None) one WooCommerce order on the attention
    list as reconciled by hand, on the list and on the order together."""
    from celerp.connectors.sync_runner import update_attention_entry
    from celerp_docs.doc_service import (
        WooCommerceReconciliationChanged,
        set_woocommerce_order_reconciled,
    )

    if not order_id.isdigit():
        raise HTTPException(status_code=422, detail="order_id must be a WooCommerce order number")

    def _apply(entry: dict) -> None:
        if not entry.get("signature"):
            raise HTTPException(
                status_code=409,
                detail="This order clears on its own once its data is fixed",
            )
        if signature is not None and entry["signature"] != signature:
            raise HTTPException(
                status_code=409,
                detail="This order changed in WooCommerce; refresh to review the change",
            )
        entry["reconciled"] = signature is not None

    entry = await update_attention_entry(
        session, str(company_id), "woocommerce", SyncEntity.ORDERS.value, order_id, _apply
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="This order is not waiting for attention")
    try:
        await set_woocommerce_order_reconciled(
            session, str(company_id), order_id,
            signature=entry["signature"], reconciled=signature is not None,
            reason=entry.get("reason"), actor_id=actor_id,
        )
    except WooCommerceReconciliationChanged as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {"entry": entry}


@router.post("/woocommerce/orders/{order_id}/reconciled")
async def mark_order_reconciled(
    order_id: str,
    payload: ReconciledRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user),
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A person reconciled this order change by hand; the import leaves the
    order alone until WooCommerce changes it again."""
    return await _set_order_reconciled(session, company_id, order_id, payload.signature, user.id)


@router.delete("/woocommerce/orders/{order_id}/reconciled")
async def unmark_order_reconciled(
    order_id: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user),
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo a mark: the order needs reconciliation again."""
    return await _set_order_reconciled(session, company_id, order_id, None, user.id)


_ITEM_SYNC_LIMIT = 200


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
    if len(payload.entity_ids) > _ITEM_SYNC_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"At most {_ITEM_SYNC_LIMIT} items can be synchronized in one request "
                f"(received {len(payload.entity_ids)})."
            ),
        )
    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        lock_connector_operation,
    )
    from celerp.events.engine import emit_event
    from celerp_inventory.services import (
        external_link_for_state,
        resolve_catalog_anchor_for_item,
        set_external_link_state,
    )
    try:
        await lock_connector_operation(
            session, company_id, connector_name, require_owner=True
        )
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        from celerp.connectors.woocommerce import WooCommerceConnector
        ctx = await _connector_context(
            str(company_id), "woocommerce", ownership_session=session
        )
        if ctx is None:
            raise HTTPException(status_code=409, detail="WooCommerce is connected but its credentials are not currently available")
        for anchor_id, anchor in anchors.items():
            try:
                await WooCommerceConnector().ensure_product_link(ctx, anchor_id, actor_id=user.id)
                updated += 1
            except Exception as exc:
                errors.append(f"{(anchor.state or {}).get('sku') or anchor_id}: {exc}")
        await session.commit()
    return {"updated": updated, "enabled": payload.enable, "errors": errors}


# ── Module entry point ────────────────────────────────────────────────────────

def setup_api_routes(app) -> None:
    """Called by the module loader to register connector routes."""
    app.include_router(router)
    app.include_router(local_router)
