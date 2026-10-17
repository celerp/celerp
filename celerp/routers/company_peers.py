# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Owner-managed company integrations."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerGrant
from celerp.routers.migrations import user_owner
from celerp.services.auth import AuthContext
from celerp.services import peer_access

router = APIRouter(prefix="/company-peers", tags=["company-peers"])

Scope = Literal[peer_access.SCOPE_ORDER]


class GrantRequest(BaseModel):
    source_company_id: uuid.UUID
    label: str = Field(min_length=1, max_length=128)
    scopes: set[Scope] = Field(min_length=1, max_length=len(peer_access.SCOPES))


def _public(row: CompanyPeerGrant) -> dict:
    return {
        "id": str(row.id), "source_company_id": str(row.source_company_id),
        "label": row.label, "scopes": list(row.scopes),
        "created_at": row.created_at.isoformat(), "expires_at": row.expires_at.isoformat(),
        "revoked": row.revoked_at is not None,
    }


def _peer_key(authorization: str) -> str:
    scheme, _, credential = authorization.partition(" ")
    if scheme != "Peer" or not credential:
        raise HTTPException(401, peer_access.INVALID)
    return credential


@router.post("/grants", status_code=201)
async def create_peer_grant(body: GrantRequest, ctx: AuthContext = Depends(user_owner),
                            session: AsyncSession = Depends(get_session)):
    row, secret = await peer_access.issue(
        session, company_id=ctx.company_id, source_company_id=body.source_company_id,
        scopes=frozenset(body.scopes), label=body.label)
    await session.commit()
    return {**_public(row), "credential": secret}


@router.get("/grants")
async def list_peer_grants(ctx: AuthContext = Depends(user_owner),
                           session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(CompanyPeerGrant).where(
        CompanyPeerGrant.company_id == ctx.company_id
    ).order_by(CompanyPeerGrant.created_at.desc()).limit(100))).scalars().all()
    return {"items": [_public(row) for row in rows]}


@router.post("/grants/{grant_id}/revoke")
async def revoke_peer_grant(grant_id: uuid.UUID, ctx: AuthContext = Depends(user_owner),
                            session: AsyncSession = Depends(get_session)):
    await peer_access.revoke(session, ctx.company_id, grant_id)
    await peer_access.notify_expiring(session, ctx.company_id)
    await session.commit()
    return {"revoked": True}


@router.post("/grants/{grant_id}/renew")
async def renew_peer_grant(grant_id: uuid.UUID, ctx: AuthContext = Depends(user_owner),
                           session: AsyncSession = Depends(get_session)):
    row = await peer_access.renew(session, ctx.company_id, grant_id)
    await peer_access.notify_expiring(session, ctx.company_id)
    await session.commit()
    return _public(row)


@router.get("/verify")
async def verify_peer_grant(source_company_id: uuid.UUID,
                            authorization: str = Header(default=""),
                            session: AsyncSession = Depends(get_session)):
    grant = await peer_access.held(
        session, _peer_key(authorization), source_company_id=source_company_id, now=None)
    company = await session.get(Company, grant.company_id)
    return {"company_id": str(grant.company_id), "company_name": company.name,
            "scopes": list(grant.scopes),
            "expires_at": grant.expires_at.isoformat()}
