# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Connector routes - /connectors/*."""
from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import date
from typing import Annotated, Literal

import httpx
import sqlalchemy as sa

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import refusal
from celerp.connectors.base import SyncEntity
from celerp.connectors.ownership import PRODUCT_CHANNEL_MODULES, PRODUCT_CHANNEL_PLATFORMS
from celerp.db import get_session
from celerp.modules.boundary import company_settings, needs_modules_for, require_on
from celerp.services.auth import (
    get_current_company_id,
    get_current_role,
    get_current_user,
    require_install_owner,
)
from celerp.services.permissions import get_current_company_settings, require_permission
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
# A store fetches the product images Celerp sends it here, through the relay's
# short-lived share address; the signed token is the only access (connector_media).
media_router = APIRouter(prefix="/connector-media", tags=["connectors"])


@media_router.get("/{token}")
async def connector_media(token: str):
    """One product image a sync sent a store, while its token is valid."""
    from fastapi.responses import Response

    from celerp.services.attachments import MAX_FILE_BYTES, read_company_file
    from celerp.services.connector_media import media_claims

    claims = media_claims(token)
    content = await read_company_file(claims["c"], claims["u"], MAX_FILE_BYTES) if claims else None
    if content is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=content, media_type=claims["m"], headers={"Cache-Control": "no-store"})


# ── Request / Response schemas ────────────────────────────────────────────────

class SyncRequest(BaseModel):
    entity: SyncEntity


class ConnectorInfo(BaseModel):
    name: str
    display_name: str
    # Each kind of record the connector carries, with the directions it can be set to.
    entity_flows: dict[str, list[str]]
    default_directions: dict[str, str]


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
    from celerp.connectors.relay_token import ConnectorUpgradeRequired, ConnectRequired, fetch_context

    try:
        return await fetch_context(company_id, connector_name, **kwargs)
    except ConnectorUpgradeRequired as exc:
        raise HTTPException(status_code=426, detail=str(exc)) from exc
    except ConnectRequired as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc


@contextlib.contextmanager
def _execution_refusals(connector):
    """A person's connector work refused by its execution, as the answer they see."""
    from celerp.connectors.execution import ExecutionBusy, ExecutionRefused
    from celerp.connectors.ownership import ConnectorOwnershipError
    from celerp.connectors.relay_token import ConnectorUpgradeRequired, ConnectRequired

    try:
        yield
    except ConnectorOwnershipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecutionBusy as exc:
        raise HTTPException(status_code=409, detail=t("connectors.fail_busy")) from exc
    except ExecutionRefused as exc:
        key = {"direction_off": "connectors.fail_direction",
               "not_connected": "error.connector_not_connected"}.get(exc.reason, "connectors.err_first_sync_needed")
        raise HTTPException(status_code=409, detail=t(key, service=connector.display_name)) from exc
    except ConnectorUpgradeRequired as exc:
        raise HTTPException(status_code=426, detail=str(exc)) from exc
    except ConnectRequired as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc


@contextlib.asynccontextmanager
async def _products_work(session: AsyncSession, company_id, connector_name: str, purpose: str):
    """A person's change to the connection's products, run inside its execution of the
    products resource, as a products sync runs: refused as busy while one runs. The
    request's transaction ends first, so the work's own transactions begin inside it. Work
    that fails rolls the request's transaction back before the execution is released, so
    the release never waits on the rows the failed work still held."""
    from celerp import connectors
    from celerp.connectors.execution import connector_execution

    try:
        connector = connectors.get(connector_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name))
    await session.commit()
    with _execution_refusals(connector):
        async with connector_execution(company_id, connector, purpose, ["products"]) as ex:
            try:
                yield ex
            except BaseException:
                await session.rollback()
                raise


async def _first_sync_row(session: AsyncSession, company_id, connector_name: str, *, for_update: bool = False):
    """The connection's first-sync record (ConnectorFirstSync), None without one."""
    from celerp.models.connector_first_sync import ConnectorFirstSync

    return await session.get(
        ConnectorFirstSync, (str(company_id), connector_name), with_for_update=for_update, populate_existing=True)


async def _ready_config(session: AsyncSession, company_id, connector):
    """The company's connection, refused while it is not connected or its first sync
    has not completed: the first sync is always started from its preview."""
    from celerp.models.connector_config import ConnectorConfig
    from sqlalchemy import select

    config = await session.scalar(
        select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector.name,
        )
    )
    if config is None:
        raise HTTPException(status_code=409, detail=t("error.connector_not_connected", service=connector.display_name))
    row = await _first_sync_row(session, company_id, connector.name)
    if row is None or row.state != "completed":
        raise HTTPException(status_code=409, detail=t("connectors.err_first_sync_needed", service=connector.display_name))
    return config


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
            entity_flows={
                key: [d.value for d in flow.allowed()] for key, flow in c.entity_flows.items()
            },
            default_directions=c.default_directions,
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

    from celerp.connectors.sync_runner import implements
    if not implements(connector, payload.entity.value):
        raise HTTPException(
            status_code=400,
            detail=f"{connector.display_name} does not support entity '{payload.entity}'",
        )

    await _ready_config(session, company_id, connector)
    # The sync's transactions begin inside its execution.
    await session.commit()

    from celerp.connectors.sync_runner import run_sync
    try:
        with _execution_refusals(connector):
            result = await run_sync(connector, company_id, payload.entity.value, purpose="manual_sync")
    except HTTPException:
        raise
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

    await _ready_config(session, company_id, connector)

    from celerp.connectors.execution import ExecutionError
    from celerp.connectors.relay_token import ConnectorUpgradeRequired, ConnectRequired
    from celerp.connectors.sync_runner import run_connector_sync
    from celerp.services.background import spawn_background

    async def _run() -> None:
        try:
            await run_connector_sync(connector, company_id, purpose="manual_sync")
        except (ExecutionError, ConnectRequired, ConnectorUpgradeRequired) as exc:
            log.info("connector sync plan not run: %s: %s", connector_name, exc)
        except Exception:
            log.exception("connector sync plan failed: %s", connector_name)

    spawn_background(_run())
    return {"ok": True}


class DirectionRequest(BaseModel):
    entity: str
    direction: str


@open_router.post("/{connector_name}/directions")
async def set_connector_direction(
    connector_name: str,
    payload: DirectionRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set the direction one kind of record syncs in, under the company lock and the
    connection's row lock taken after the ownership fence. A direction the connector cannot
    sync that record in is refused, naming both. A store whose webhooks follow the
    directions has them marked for reconciling; the execution holding the webhooks
    changes them, never this request."""
    from celerp import connectors
    from celerp.connectors.base import DirectionError, validate_directions
    from celerp.connectors.execution import first_sync_state
    from celerp.connectors.ownership import (
        ConnectorOwnershipError, lock_connection_row, lock_connector_operation,
    )
    from celerp.connectors.registry import service_name
    from celerp.connectors.webhook_reconcile import mark_webhooks_dirty

    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name)) from exc
    try:
        await lock_connector_operation(session, company_id, connector_name, require_owner=True)
        config = await lock_connection_row(session, company_id, connector_name)
        if config is None:
            raise ConnectorOwnershipError(t("error.connector_not_here", service=service_name(connector_name)))
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        directions = validate_directions(
            connector, {**(config.entity_directions or {}), payload.entity: payload.direction}
        )
    except DirectionError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    changed = directions != config.entity_directions
    config.entity_directions = directions
    if changed and config.webhook_secret and await first_sync_state(
            session, company_id, connector_name) not in (None, "pending"):
        await mark_webhooks_dirty(session, config)
    await session.commit()
    return {"entity_directions": directions}


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

    async def _failure(error: str, detail: str) -> dict:
        try:
            await revoke_connector_remote_state(company_id, connector_name)
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

    if connector_name == "woocommerce":
        import secrets

        # Connecting writes nothing to the store: the first sync creates the webhooks,
        # signed with this secret, and retires any an earlier connection left.
        config.webhook_secret = secrets.token_hex(32)
        config.webhook_ids = []

    try:
        await session.commit()
    except Exception as exc:
        return await _failure("local_cleanup_failed", str(exc))
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
    ``kept_store_values`` counts the items still holding values the first sync took
    from the store, whose undo record goes with the connection.
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
    from celerp.connectors.product_links import refuse_while_links_cannot_detach
    await refuse_while_links_cannot_detach(session, company_id, connector_name)

    # The accountant connection is this company's own: when the relay cannot be
    # reached to drop its copy of the key, it disconnects anyway.
    if failure := await _revoke_remote(
        session, company_id, connector_name, webhook_ids, force=force,
        best_effort=connector_name == "celerp",
    ):
        return failure

    # Read before the release forgets the first sync's undo record with the connection.
    first_sync = await _first_sync_row(session, company_id, connector_name)
    kept = first_sync.links_holding_store_values if first_sync is not None else 0
    try:
        await release_connector_ownership(
            session, company_id, connector_name,
            status=RESET_STATUS_FORCED if force else RESET_STATUS_DISCONNECTED,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        return {"ok": False, "error": "local_cleanup_failed", "detail": str(exc)}
    return {"ok": True, "kept_store_values": kept}


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


@router.get("/{connector_name}/first-sync/preview")
async def preview_first_sync(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    orders_from: date | None = None,
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What starting the first sync in the connection's current directions would do,
    importing orders from ``orders_from`` (today when not given). Reads both sides and
    writes to neither."""
    import dataclasses

    from fastapi.encoders import jsonable_encoder

    from celerp import connectors

    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name)) from exc
    config = await _owned_config(session, company_id, connector)
    row = await _first_sync_row(session, company_id, connector_name)
    if row is None or row.state != "pending":
        raise HTTPException(status_code=409, detail=t("connectors.err_first_sync_done"))
    await session.commit()
    preview = await _read_preview(connector, company_id, config.id, dict(config.entity_directions), orders_from)
    return jsonable_encoder(dataclasses.asdict(preview))


async def _read_preview(connector, company_id, config_id: int, directions: dict, orders_from: date | None):
    """The first sync's preview read from the store, outside any lock: what the preview
    page shows and what Start compares the person's receipt with."""
    from datetime import datetime, time, timezone

    from celerp.connectors.first_sync import build_preview

    ctx = await _connector_context(str(company_id), connector.name)
    if ctx is None:
        raise HTTPException(status_code=409, detail=t("error.connector_not_connected", service=connector.display_name))
    start = datetime.combine(orders_from or datetime.now(timezone.utc).date(), time(), tzinfo=timezone.utc)
    try:
        return await build_preview(connector, ctx, directions, start, config_id)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=t(
            "connectors.err_preview", service=connector.display_name, detail=exc)) from exc


@router.get("/{connector_name}/first-sync/summary")
async def first_sync_summary(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What the first sync did and how to undo it: its state (with the reason when it needs
    attention), the import of the products it created,
    each link it made (with its undo's outcome and what that undo kept), and the
    invoices whose creation names it (first_sync_metadata), newest first. Invoices are voided one by one, which keeps their audit trail."""
    import uuid

    import sqlalchemy as sa

    from celerp import connectors
    from celerp.connectors.base import FIRST_SYNC_METADATA_KEY
    from celerp.models.import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection

    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name)) from exc
    await _owned_config(session, company_id, connector)
    first_sync = await _first_sync_row(session, company_id, connector_name)
    undo = dict((first_sync.undo if first_sync is not None else None) or {})
    batch = None
    if first_sync is not None and first_sync.batch_id:
        row = await session.get(ImportBatch, uuid.UUID(first_sync.batch_id))
        if row is not None:
            batch = {"id": str(row.id), "rows": row.row_count, "status": row.status}
    links = list(undo.get("links") or [])
    items = {row.entity_id: row.state or {} for row in (await session.execute(sa.select(Projection).where(
        Projection.company_id == uuid.UUID(str(company_id)),
        Projection.entity_id.in_([link["entity_id"] for link in links])))).scalars()} if links else {}
    invoices = []
    if first_sync is not None and first_sync.operation_id:
        # The documents whose creation names this first sync (first_sync_metadata).
        cid = uuid.UUID(str(company_id))
        created = sa.select(LedgerEntry.entity_id).where(
            LedgerEntry.company_id == cid, LedgerEntry.entity_type == "doc",
            LedgerEntry.event_type == "doc.created",
            LedgerEntry.metadata_[FIRST_SYNC_METADATA_KEY].as_string() == first_sync.operation_id)
        rows = (await session.execute(sa.select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "doc", Projection.entity_id.in_(created),
        ).order_by(Projection.created_at.desc(), Projection.entity_id.desc()))).scalars().all()
        invoices = [{"entity_id": r.entity_id,
                     "number": (r.state or {}).get("doc_number") or (r.state or {}).get("ref_id"),
                     "total": (r.state or {}).get("total"), "status": (r.state or {}).get("status")}
                    for r in rows]
    return {
        "started_at": first_sync.started_at.isoformat() if first_sync is not None and first_sync.started_at else None,
        "state": first_sync.state if first_sync is not None else None,
        "error": first_sync.error if first_sync is not None else None,
        "batch": batch,
        "links": [_link_summary(link, items.get(link["entity_id"])) for link in links],
        "links_undone_at": undo.get("links_undone_at"),
        "invoices": invoices,
    }


def _link_summary(link: dict, item: dict | None) -> dict:
    """One link on the first sync's summary: the item (its SKU and name now; None once it
    is gone), its undo outcome (None while still to undo), the fields and files its undo
    kept because they changed since, and how many files the first sync pulled onto it."""
    kept = link.get("kept") or {}
    return {"entity_id": link["entity_id"], "sku": (item or {}).get("sku"), "name": (item or {}).get("name"),
            "outcome": link.get("outcome"),
            "fields": kept.get("fields") or {}, "kept_files": kept.get("files") or [],
            "files": len(link.get("files") or [])}


async def _owned_config(session: AsyncSession, company_id, connector):
    from celerp.connectors.ownership import ConnectorOwnershipError, lock_connector_operation

    try:
        config = await lock_connector_operation(session, company_id, connector.name, require_owner=True)
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return config


class FirstSyncRequest(BaseModel):
    """The choices a person made on the first sync's preview."""
    directions: dict[str, str]
    match: Literal["keep", "take"]
    # The earliest order date to import; none starts at the first sync itself.
    orders_from: date | None = None
    # The version of the preview the person reviewed: Start refuses once it changed.
    preview_version: str = Field(min_length=64, max_length=64)


@router.post("/{connector_name}/first-sync")
async def start_first_sync(
    connector_name: str,
    payload: FirstSyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user),
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start a connection's first sync with the choices reviewed on its preview: the
    directions, the earliest order date and how SKU matches link. The preview is read
    again, outside every lock, with those choices; when it is no longer the version the
    person reviewed (the store, the catalog or the connection changed since), Start is
    refused and the page shows the preview again. It runs once, in the background, and
    only on the store products the person reviewed: it first sets up the store's
    webhooks, and when the store refuses, the first sync needs attention with the store's
    reason."""
    import uuid
    from datetime import datetime, time, timezone

    from celerp import connectors
    from celerp.connectors.base import DirectionError, entity_allowed, validate_directions
    from celerp.connectors.ownership import ConnectorOwnershipError, lock_connector_operation
    from celerp.connectors.webhook_reconcile import mark_webhooks_dirty
    from celerp.models.import_batch import ImportBatch

    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name)) from exc
    try:
        directions = validate_directions(connector, payload.directions)
    except DirectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    previewed = await _owned_config(session, company_id, connector)
    row = await _first_sync_row(session, company_id, connector_name)
    if row is None or row.state != "pending":
        raise HTTPException(status_code=409, detail=t("connectors.err_first_sync_done"))
    config_id = previewed.id
    await session.commit()
    preview = await _read_preview(connector, company_id, config_id, directions, payload.orders_from)
    if preview.version != payload.preview_version:
        raise HTTPException(status_code=409, detail=t("connectors.err_preview_changed", service=connector.display_name))

    try:
        config = await lock_connector_operation(
            session, company_id, connector_name, require_owner=True, exclusive=True)
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    first_sync = await _first_sync_row(session, company_id, connector_name, for_update=True)
    if first_sync is None or first_sync.state != "pending":
        await session.rollback()
        raise HTTPException(status_code=409, detail=t("connectors.err_first_sync_done"))
    if config.id != config_id:  # reconnected since the preview was read
        await session.rollback()
        raise HTTPException(status_code=409, detail=t("connectors.err_preview_changed", service=connector.display_name))

    now = datetime.now(timezone.utc)
    config.entity_directions = directions
    config.orders_from = (datetime.combine(payload.orders_from, time(), tzinfo=timezone.utc)
                          if payload.orders_from is not None else now)
    batch_id = None
    if entity_allowed(connector, "products", directions):
        batch = ImportBatch(
            company_id=uuid.UUID(str(company_id)), entity_type="item",
            filename=t("connectors.first_sync_batch", service=connector.display_name),
            row_count=0, entity_ids=[], idempotency_keys=[], reversible=True,
        )
        session.add(batch)
        await session.flush()
        batch_id = str(batch.id)
    first_sync.state = "starting"
    first_sync.directions = directions
    first_sync.match = payload.match
    first_sync.preview_version = preview.version
    first_sync.reviewed = preview.reviewed
    first_sync.watermark = now
    first_sync.started_by = str(user.id)
    first_sync.started_at = now
    first_sync.batch_id = batch_id
    first_sync.operation_id = str(uuid.uuid4())
    first_sync.undo = {"links": []}
    first_sync.error = None
    if config.webhook_secret:
        await mark_webhooks_dirty(session, config)
    await session.commit()
    _spawn_first_sync(company_id, connector_name)
    return {"ok": True}


def _spawn_first_sync(company_id, connector_name: str) -> None:
    """Run a started first sync in the background (sync_runner.execute_first_sync); one a
    stopped process leaves behind is resumed by the execution sweeper."""
    from celerp.connectors.sync_runner import execute_first_sync
    from celerp.services.background import spawn_background

    async def _run() -> None:
        try:
            await execute_first_sync(company_id, connector_name)
        except Exception:
            log.exception("first sync failed: %s", connector_name)

    spawn_background(_run())


@router.post("/{connector_name}/first-sync/retry")
async def retry_first_sync(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start a first sync that needs attention again, with the choices, import batch and
    order cutoff it started with; it runs only what did not complete."""
    from celerp import connectors
    from celerp.connectors import sync_runner
    from celerp.connectors.ownership import ConnectorOwnershipError, lock_connector_operation

    try:
        connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=t("connectors.unknown_connector", platform=connector_name)) from exc
    try:
        await lock_connector_operation(session, company_id, connector_name, require_owner=True, exclusive=True)
        await sync_runner.retry_first_sync(session, company_id, connector_name)
    except ConnectorOwnershipError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=t("connectors.err_first_sync_not_failed")) from exc
    await session.commit()
    _spawn_first_sync(company_id, connector_name)
    return {"ok": True}


@router.post("/{connector_name}/first-sync/undo-links")
async def undo_first_sync_links(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo the links the first sync made to items Celerp already had, as far as each is
    still the first sync's (celerp_inventory.services.undo_external_link): a value or file
    changed since, or a link the item no longer holds, is kept and listed in ``kept``. Each
    link's outcome is saved on the undo record in the same transaction, so a failed undo
    changes nothing and its repeat settles only the links still open. The items the first
    sync created are undone in Import History."""
    require_on(await company_settings(company_id), "celerp-inventory")
    from datetime import datetime, timezone

    from celerp_inventory import services as inventory

    from celerp.services.company_lock import lock_company

    async with _products_work(session, company_id, connector_name, "undo"):
        # The company before the first-sync row, as every company write takes it (lock order).
        await lock_company(session, uuid.UUID(str(company_id)))
        first_sync = await _first_sync_row(session, company_id, connector_name, for_update=True)
        undo = dict((first_sync.undo if first_sync is not None else None) or {})
        if undo.get("links_undone_at") or not undo.get("links"):
            await session.rollback()
            raise HTTPException(status_code=409, detail=t("connectors.err_no_links_to_undo"))
        undone, kept, links = 0, [], []
        for link in sorted(undo["links"], key=lambda link: link["entity_id"]):
            if "outcome" not in link:
                result = await inventory.undo_external_link(
                    session, company_id, connector_name, link, role=role, settings=settings,
                    actor_id=user.id, source="connector_ui")
                link = {**link, "outcome": result["outcome"], "kept": {
                    "fields": result["kept"], "files": result["files"]}}
                undone += result["outcome"] == "undone"
                if result["outcome"] != "undone" or result["kept"] or result["files"]:
                    kept.append({"entity_id": link["entity_id"], "outcome": result["outcome"],
                                 "fields": result["kept"], "files": result["files"]})
            links.append(link)
        first_sync.undo = {**undo, "links": links, "links_undone_at": datetime.now(timezone.utc).isoformat()}
        await session.commit()
        return {"undone": undone, "kept": kept}


class ConflictChoice(BaseModel):
    keep: str  # "store": take the store's values; "celerp": keep Celerp's


@router.post("/{connector_name}/products/{record_id}/resolve")
async def resolve_product_conflict(
    connector_name: str,
    record_id: str,
    payload: ConflictChoice,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user),
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A person chose between the two values of a product field changed in both
    systems. Either way the link now agrees on the store's value, so a later pull
    leaves the field alone; keeping Celerp's value sends it out at the next sync when
    products go out. Taking the store's is an ordinary item edit, undone by editing
    the item back."""
    from celerp.connectors.sync_runner import remove_attention_entry
    from celerp.events.engine import emit_event
    from celerp.services.field_fingerprints import LINK_KEY, fingerprint
    require_on(await company_settings(company_id), "celerp-inventory")
    from celerp_inventory.services import (
        lock_external_identity_namespace,
        resolve_catalog_anchor_for_item,
        set_external_link_state,
    )
    from celerp.connectors.product_links import external_link_for_state

    if connector_name not in PRODUCT_CHANNEL_PLATFORMS or payload.keep not in ("store", "celerp"):
        raise HTTPException(status_code=422, detail=t("connectors.err_conflict_choice"))
    async with _products_work(session, company_id, connector_name, "resolve"):
        entry = await remove_attention_entry(
            session, str(company_id), connector_name, SyncEntity.PRODUCTS.value, record_id)
        if entry is None or entry.get("kind") != "conflict":
            await session.rollback()
            raise HTTPException(status_code=404, detail=t("connectors.err_conflict_gone"))
        entity_id, fields = entry["entity_id"], entry["fields"]
        # The store's links before the item row the store-value edit writes (lock order).
        await lock_external_identity_namespace(session, company_id, connector_name)
        anchor = await resolve_catalog_anchor_for_item(session, company_id, entity_id)
        link = external_link_for_state(anchor.state or {}, connector_name)
        if not link:
            await session.rollback()
            raise HTTPException(status_code=404, detail=t("connectors.err_conflict_gone"))
        if payload.keep == "store":
            state = anchor.state or {}
            changed = {key: {"old": state.get(key), "new": both["remote"]} for key, both in fields.items()}
            if "sale_price" in fields:
                changed["retail_price"] = {"old": state.get("retail_price"), "new": fields["sale_price"]["remote"]}
            await emit_event(
                session, company_id=company_id, entity_id=entity_id, entity_type="item",
                event_type="item.updated", data={"fields_changed": changed}, actor_id=user.id,
                location_id=None, source="connector_ui",
                idempotency_key=f"conflict:{connector_name}:{entity_id}:v{anchor.version}", metadata_={},
            )
        agreed = {**(link.get(LINK_KEY) or {}),
                  **{key: fingerprint(both["remote"]) for key, both in fields.items()}}
        await set_external_link_state(
            session, company_id, entity_id, connector_name, link_updates={LINK_KEY: agreed},
            actor_id=user.id, source="connector_ui",
        )
        await session.commit()
        return {"entry": entry, "kept": payload.keep}


_ITEM_SYNC_LIMIT = 200


class ItemSyncRequest(BaseModel):
    entity_ids: list[str]
    # publish: link or create the product, published; unpublish: set it to draft in the
    # store, keeping the link; stop / resume: stop or resume syncing it, in Celerp only.
    action: Literal["publish", "unpublish", "stop", "resume"]
    # Items whose existing store product (same SKU) a person agreed to link.
    confirm_link: list[str] = []


@local_router.post("/{connector_name}/sync")
@needs_modules_for("connector_name", PRODUCT_CHANNEL_MODULES)
async def set_item_sync(
    connector_name: str, payload: ItemSyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    user=Depends(get_current_user), _: None = require_permission("adjust_inventory"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Publish, unpublish, stop or resume catalog products on one connected channel.

    Publishing an item whose SKU the store already has asks first: the item is listed
    under ``needs_confirmation`` with the store product's name, and is linked when sent
    again in ``confirm_link``. Unpublish sets the store product to draft and keeps the
    link, so Publish undoes it; nothing is ever deleted in the store."""
    if connector_name not in PRODUCT_CHANNEL_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported catalog connector")
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail=t("error.sync_none_selected"))
    if len(payload.entity_ids) > _ITEM_SYNC_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=t("error.sync_too_many", count=len(payload.entity_ids), limit=_ITEM_SYNC_LIMIT),
        )
    if connector_name == "shopify" and payload.action == "unpublish":
        raise HTTPException(status_code=422, detail=t("connectors.err_unpublish_shopify"))
    # Publishing and unpublishing change the store; the rest changes only Celerp's records.
    store_change = connector_name == "woocommerce" and payload.action in ("publish", "unpublish")
    async with _products_work(session, company_id, connector_name,
                              "item_publish" if store_change else "item_flags") as ex:
        from celerp import connectors
        from celerp.connectors.ownership import ensure_store_bound
        from celerp.events.engine import emit_event
        from celerp.connectors.product_links import external_link_for_state
        from celerp_inventory.services import resolve_catalog_anchor_for_item, set_external_link_state
        if connector_name == "shopify":
            from celerp.connectors.ownership import lock_connector_operation

            # The sync flag's event needs the Shopify fence in its transaction, ahead of
            # the company lock (emit_event).
            await lock_connector_operation(session, company_id, "shopify", require_owner=True)
        anchors: dict[str, object] = {}
        errors: list[str] = []
        for entity_id in dict.fromkeys(payload.entity_ids):
            try:
                anchor = await resolve_catalog_anchor_for_item(session, company_id, entity_id)
                anchors[anchor.entity_id] = anchor
            except ValueError as exc:
                errors.append(f"{entity_id}: {exc}")
        updated = 0
        needs_confirmation: list[dict] = []
        action = payload.action
        if connector_name == "shopify":
            desired = action in ("publish", "resume")
            for anchor_id, anchor in anchors.items():
                state = anchor.state or {}
                linked = external_link_for_state(state, "shopify")
                if desired and not linked:
                    errors.append(f"{state.get('sku') or anchor_id}: not linked to a Shopify product; import it from Shopify first")
                    continue
                if bool(anchor.is_sync_to_shopify) == desired: continue
                await emit_event(session, company_id=company_id, entity_id=anchor_id, entity_type="item",
                    event_type="shop.sync.enabled" if desired else "shop.sync.disabled", data={},
                    actor_id=user.id, location_id=None, source="connector_ui",
                    idempotency_key=str(__import__("uuid").uuid4()), metadata_={})
                updated += 1
            await session.commit()
        elif action in ("stop", "resume"):
            for anchor_id, anchor in anchors.items():
                link = external_link_for_state(anchor.state or {}, "woocommerce")
                if not link or link.get("sync_enabled") is (action == "resume"):
                    continue
                await set_external_link_state(session, company_id, anchor_id, "woocommerce",
                    sync_enabled=action == "resume", actor_id=user.id, source="connector_ui")
                updated += 1
            await session.commit()
        else:
            from celerp.connectors.woocommerce import LinkNeedsConfirmation, WooCommerceConnector
            # No transaction stays open across a store call; the same store check a sync makes.
            await session.commit()
            ctx = ex.ctx
            await ensure_store_bound(company_id, connectors.get("woocommerce"), ctx)
            woo = WooCommerceConnector()
            for anchor_id, anchor in anchors.items():
                sku = (anchor.state or {}).get("sku") or anchor_id
                try:
                    if action == "unpublish":
                        await woo.set_product_status(ctx, anchor_id, "draft", actor_id=user.id)
                    else:
                        await woo.ensure_product_link(
                            ctx, anchor_id, actor_id=user.id,
                            confirm_link=anchor_id in payload.confirm_link,
                        )
                    updated += 1
                except LinkNeedsConfirmation as ask:
                    needs_confirmation.append({"entity_id": anchor_id, "sku": sku,
                                               "remote_name": ask.remote_name, "message": str(ask)})
                except Exception as exc:
                    errors.append(f"{sku}: {exc}")
        return {"updated": updated, "action": action, "errors": errors,
                "needs_confirmation": needs_confirmation}


# ── Module entry point ────────────────────────────────────────────────────────

def setup_api_routes(app) -> None:
    """Called by the module loader to register connector routes."""
    app.include_router(router)
    app.include_router(open_router)
    app.include_router(local_router)
    app.include_router(media_router)
