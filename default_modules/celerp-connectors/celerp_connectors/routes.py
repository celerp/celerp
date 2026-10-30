# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Connector routes - /connectors/*."""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import refusal
from celerp.connectors.base import SyncDirection, SyncEntity
from celerp.connectors.ownership import PRODUCT_CHANNEL_MODULES, PRODUCT_CHANNEL_PLATFORMS
from celerp.db import get_session
from celerp.modules.boundary import needs_modules_for
from celerp.services.auth import (
    get_current_company_id,
    get_current_user,
    require_install_owner,
)
from celerp.services.permissions import require_permission
from ui.i18n import t

log = logging.getLogger(__name__)


async def _require_session(request: Request) -> None:
    """Refuse connector routes while this installation has no active Connect session."""
    from celerp.session_gate import require_session_token
    await require_session_token(request)


router = APIRouter(
    prefix="/connectors",
    tags=["connectors"],
    dependencies=[Depends(get_current_user), Depends(_require_session)],
)
# Leaving a connection never needs Connect.
open_router = APIRouter(
    prefix="/connectors", tags=["connectors"],
    dependencies=[Depends(get_current_user)],
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
    from celerp import connectors
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
    from celerp import connectors
    try:
        connector = connectors.get(connector_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name))

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
        raise HTTPException(status_code=409, detail=t("error.connector_not_connected", service=connector.display_name))
    direction = SyncDirection(config.direction)

    ctx = await _connector_context(str(company_id), connector_name)
    if ctx is None:
        raise HTTPException(status_code=409, detail=t("error.connector_not_connected", service=connector.display_name))

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
        raise HTTPException(status_code=502, detail=t("error.connector_sync_error", service=connector.display_name, detail=exc))

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
    from celerp import connectors
    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name)) from exc

    from celerp.models.connector_config import ConnectorConfig
    from sqlalchemy import select
    config = await session.scalar(
        select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector_name,
        )
    )
    if config is None:
        raise HTTPException(status_code=409, detail=t("error.connector_not_connected", service=connector.display_name))
    direction = SyncDirection(config.direction)

    ctx = await _connector_context(str(company_id), connector_name)
    if ctx is None:
        raise HTTPException(status_code=409, detail=t("error.connector_not_connected", service=connector.display_name))

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
            "detail": t("connectors.err_relay_not_https")}


@router.post("/{connector_name}/credentials")
@needs_modules_for("connector_name", PRODUCT_CHANNEL_MODULES)
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
    from celerp import connectors

    try:
        connector = connectors.get(connector_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name))

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

    elif connector_name == "celerp":
        import json
        from urllib.parse import urlsplit
        from celerp.services.outbound_url import validate_public_base_url, fetch_public_bytes
        try:
            parsed = urlsplit(store_url)
            if (not store_url or parsed.scheme != "https" or not parsed.hostname
                    or parsed.port not in (None, 443) or parsed.path not in ("", "/")
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or payload.consumer_secret):
                return {"ok": False, "error": "store_unreachable",
                        "detail": "Enter the receiving installation's HTTPS address and company key."}
            store_url = await validate_public_base_url(
                store_url, reject_query=True, reject_fragment=True)
            response = await fetch_public_bytes(
                f"{store_url}/company-peers/verify",
                max_bytes=4096, timeout=10.0,
                headers={"Authorization": "Peer " + payload.consumer_key},
                params={"source_company_id": str(company_id)},
            )
            if response.status_code != 200:
                return {"ok": False, "error": "store_rejected",
                        "detail": "The receiving installation rejected this company key."}
            info = json.loads(response.content)
            if (not isinstance(info, dict) or not info.get("scopes")
                    or not info.get("company_id")):
                return {"ok": False, "error": "store_rejected",
                        "detail": "The receiving company did not confirm which records it accepts."}
        except Exception:
            return {"ok": False, "error": "store_unreachable",
                    "detail": "The receiving Celerp installation could not be verified."}

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

    store_ctx = ConnectorContext(
        company_id=str(company_id),
        access_token=(payload.consumer_key if connector_name == "celerp"
                      else f"{payload.consumer_key}:{payload.consumer_secret}"),
        store_handle=store_url or None,
    )
    # The store is checked against the company's records, and its old product
    # links become history, in the same commit that makes the company its owner.
    try:
        await bind_connector_store(session, company_id, connector, store_ctx)
    except ConnectorStoreChangedError as exc:
        await session.rollback()
        return {"ok": False, "error": "store_changed", "detail": str(exc)}

    try:
        if connector_name in PRODUCT_CHANNEL_PLATFORMS:
            from celerp.connectors.product_links import detach_product_links
            await detach_product_links(session, company_id, connector_name)
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

    from celerp.connectors.relay_token import RelayStoreError, store_api_key
    try:
        await store_api_key(connector_name, payload.consumer_key, payload.consumer_secret, store_url)
    except RelayStoreError as exc:
        if exc.status == 402:
            return await _failure("subscription_required", "")
        return await _failure("relay_error", str(exc))

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


@open_router.delete("/{connector_name}/credentials")
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

    # Refused before the store forgets anything, while product links are left
    # that only Inventory can turn into history.
    from celerp.connectors.product_links import detach_product_links, refuse_while_links_cannot_detach
    await refuse_while_links_cannot_detach(session, company_id, connector_name)

    # The accountant connection is this company's own: when the relay cannot be
    # reached to drop its copy of the key, it disconnects anyway.
    if failure := await _revoke_remote(
        session, company_id, connector_name, webhook_ids, force=force,
        best_effort=connector_name == "celerp",
    ):
        return failure

    try:
        if connector_name in PRODUCT_CHANNEL_PLATFORMS:
            await detach_product_links(session, company_id, connector_name)
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
    webhook_ids: list[str], *, force: bool, best_effort: bool = False,
) -> dict | None:
    """Disconnect the connector remotely. Returns the failure response, with
    the local transaction rolled back, or None when it is disconnected. With
    `best_effort`, a cleanup that cannot be confirmed is logged, not refused."""
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
        if best_effort:
            log.warning("%s remote cleanup unconfirmed; disconnecting locally", connector_name)
            return None
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
    from celerp import connectors
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
    except KeyError:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name))

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


def _order_changed() -> dict:
    """The refusal for marking an order reconciled after WooCommerce changed it again."""
    return refusal("connectors.order_changed_in_woocommerce",
                   "This order changed in WooCommerce. Select Try again to review the change.")


class ReconciledRequest(BaseModel):
    signature: str


async def _set_order_reconciled(
    session: AsyncSession, company_id, order_id: str, signature: str | None, actor_id,
) -> dict:
    """Mark (signature) or unmark (None) one WooCommerce order on the attention
    list as reconciled by hand, on the list and on the order together."""
    from celerp.connectors.sync_runner import update_attention_entry
    from celerp.modules.boundary import company_settings, require_on
    require_on(await company_settings(company_id), "celerp-docs")
    from celerp_docs.doc_service import (
        WooCommerceReconciliationChanged,
        set_woocommerce_order_reconciled,
    )

    if not order_id.isdigit():
        raise HTTPException(status_code=422, detail="order_id must be a WooCommerce order number")
    from celerp.services.company_lock import lock_company
    await lock_company(session, uuid.UUID(str(company_id)))

    def _apply(entry: dict) -> None:
        if not entry.get("signature"):
            raise HTTPException(
                status_code=409,
                detail=t("error.order_clears_itself"),
            )
        if signature is not None and entry["signature"] != signature:
            raise HTTPException(status_code=409, detail=_order_changed())
        entry["reconciled"] = signature is not None

    entry = await update_attention_entry(
        session, str(company_id), "woocommerce", SyncEntity.ORDERS.value, order_id, _apply
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=t("error.order_no_attention"))
    try:
        await set_woocommerce_order_reconciled(
            session, str(company_id), order_id,
            signature=entry["signature"], reconciled=signature is not None,
            reason=entry.get("reason"), actor_id=actor_id,
        )
    except WooCommerceReconciliationChanged as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_order_changed()) from exc
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
@needs_modules_for("connector_name", PRODUCT_CHANNEL_MODULES)
async def set_item_sync(
    connector_name: str, payload: ItemSyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user), _: None = require_permission("adjust_inventory"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Enable or disable catalog product synchronization for one connected channel."""
    if connector_name not in PRODUCT_CHANNEL_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported catalog connector")
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail=t("error.sync_none_selected"))
    if len(payload.entity_ids) > _ITEM_SYNC_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=t("error.sync_too_many", count=len(payload.entity_ids), limit=_ITEM_SYNC_LIMIT),
        )
    from celerp.connectors.ownership import (
        ConnectorOwnershipError,
        lock_connector_operation,
    )
    from celerp.events.engine import emit_event
    from celerp.connectors.product_links import external_link_for_state
    from celerp_inventory.services import resolve_catalog_anchor_for_item, set_external_link_state
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
            raise HTTPException(status_code=409, detail=t("error.woocommerce_credentials"))
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
    app.include_router(open_router)
    app.include_router(local_router)
