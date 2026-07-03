# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Connector routes — /connectors/*

Cloud-gated: all endpoints require both:
  - User authentication (get_current_user)
  - Active Celerp Cloud subscription (require_session_token via X-Session-Token)

Token flow (relay model):
  The client authenticates with relay.celerp.com to obtain a short-lived
  access_token, then passes it in the request body here. OAuth credentials
  never touch the core instance.

Self-hosted / bring-your-own-token:
  Pass access_token + store_handle directly. Core does not validate origin.
  Session token is still required (your instance must be connected to Celerp Cloud).
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


class SyncResponse(BaseModel):
    connector: str
    entity: SyncEntity
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
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
        ok=result.ok,
    )


# ── Module entry point ────────────────────────────────────────────────────────

def setup_api_routes(app) -> None:
    """Called by the module loader to register connector routes."""
    app.include_router(router)
