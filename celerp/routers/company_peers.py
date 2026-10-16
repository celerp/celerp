# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Owner-managed company integrations."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, File, Form, UploadFile
from pydantic import BaseModel, Field, ConfigDict, FiniteFloat, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
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
    lifetime_days: int = Field(default=30, ge=1, le=90)


def _public(row: CompanyPeerGrant) -> dict:
    return {
        "id": str(row.id), "source_company_id": str(row.source_company_id),
        "label": row.label, "scopes": list(row.scopes),
        "expires_at": row.expires_at.isoformat(),
        "revoked": row.revoked_at is not None,
    }


@router.post("/grants", status_code=201)
async def create_peer_grant(body: GrantRequest, ctx: AuthContext = Depends(user_owner),
                            session: AsyncSession = Depends(get_session)):
    row, secret = await peer_access.issue(
        session, company_id=ctx.company_id, source_company_id=body.source_company_id,
        scopes=frozenset(body.scopes), label=body.label, lifetime_days=body.lifetime_days)
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
    await session.commit()
    return {"revoked": True}

@router.get("/verify")
async def verify_peer_grant(source_company_id: uuid.UUID,
                            scope: Scope = "contacts",
                            authorization: str = Header(default=""),
                            session: AsyncSession = Depends(get_session)):
    scheme, _, credential = authorization.partition(" ")
    if scheme != "Peer" or not credential:
        raise HTTPException(401, "Invalid integration credentials")
    grant = await peer_access.authenticate(
        session, credential, source_company_id=source_company_id, scope=scope)
    return {"company_id": str(grant.company_id), "scopes": list(grant.scopes)}


class ContactData(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    billing_address: str | None = Field(default=None, max_length=4096)
    shipping_address: str | None = Field(default=None, max_length=4096)
    currency: str | None = Field(default=None, max_length=3)
    contact_type: Literal["customer", "vendor", "both"] = "customer"


class PeerContactUpdate(BaseModel):
    source_company_id: uuid.UUID
    source_entity_id: str = Field(min_length=1, max_length=512)
    source_revision: int = Field(ge=1, le=2**63 - 1)
    data: ContactData


@router.post("/contacts")
async def receive_peer_contact(body: PeerContactUpdate,
                               authorization: str = Header(default=""),
                               session: AsyncSession = Depends(get_session)):
    from celerp.services.peer_identity import receive_contact
    scheme, _, key = authorization.partition(" ")
    if scheme != "Peer" or not key:
        raise HTTPException(401, "Invalid integration credentials")
    grant = await peer_access.authenticate(
        session, key, source_company_id=body.source_company_id, scope="contacts")
    result = await receive_contact(
        session, grant=grant, source_entity_id=body.source_entity_id,
        source_revision=body.source_revision, data=body.data.model_dump())
    await session.commit()
    return {"status": result}



class ProductData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    sku: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=20000)
    sell_by: str = Field(default="piece", min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=255)
    barcode: str | None = Field(default=None, max_length=255)
    retail_price: FiniteFloat | None = Field(default=None, ge=0)
    wholesale_price: FiniteFloat | None = Field(default=None, ge=0)


class PeerProductUpdate(BaseModel):
    source_company_id: uuid.UUID
    source_entity_id: str = Field(min_length=1, max_length=512)
    source_revision: int = Field(ge=1, le=2**63 - 1)
    data: ProductData


@router.post("/products")
async def receive_peer_product(body: PeerProductUpdate,
                               authorization: str = Header(default=""),
                               session: AsyncSession = Depends(get_session)):
    from celerp.services.peer_identity import receive_product
    scheme, _, key = authorization.partition(" ")
    if scheme != "Peer" or not key:
        raise HTTPException(401, "Invalid integration credentials")
    grant = await peer_access.authenticate(
        session, key, source_company_id=body.source_company_id, scope="products")
    outcome = await receive_product(
        session, grant=grant, source_entity_id=body.source_entity_id,
        source_revision=body.source_revision, data=body.data.model_dump())
    await session.commit()
    return {"status": outcome}


class PeerDocumentUpdate(BaseModel):
    source_company_id: uuid.UUID
    source_entity_id: str = Field(min_length=1, max_length=512)
    source_revision: int = Field(ge=1, le=2**63 - 1)
    data: dict


@router.post("/documents")
async def receive_peer_document(body: PeerDocumentUpdate,
                                authorization: str = Header(default=""),
                                session: AsyncSession = Depends(get_session)):
    from celerp.services.peer_documents import receive_draft_document
    scheme, _, credential = authorization.partition(" ")
    if scheme != "Peer" or not credential:
        raise HTTPException(401, "Invalid integration credentials")
    grant = await peer_access.authenticate(
        session, credential, source_company_id=body.source_company_id,
        scope="documents")
    outcome = await receive_draft_document(
        session, grant=grant, source_entity_id=body.source_entity_id,
        source_revision=body.source_revision, data=body.data)
    await session.commit()
    return {"status": outcome}



@router.post("/attachments")
async def receive_peer_attachment(
    source_company_id: uuid.UUID = Form(),
    source_document_id: str = Form(min_length=1, max_length=512),
    source_file_id: str = Form(min_length=1, max_length=255),
    sha256_hex: str = Form(min_length=64, max_length=64),
    file: UploadFile = File(),
    document_tag: str | None = Form(default=None, max_length=128),
    description: str | None = Form(default=None, max_length=4096),
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
):
    from celerp.services.peer_attachments import receive_attachment

    scheme, _, credential = authorization.partition(" ")
    if scheme != "Peer" or not credential:
        raise HTTPException(401, "Invalid integration credentials")
    grant = await peer_access.authenticate(
        session, credential, source_company_id=source_company_id,
        scope="documents",
    )
    status = await receive_attachment(
        session, grant=grant, source_doc_id=source_document_id,
        source_file_id=source_file_id, sha256_hex=sha256_hex, file=file,
        document_tag=document_tag, description=description,
    )
    return {"status": status}



class StockCountItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=512)
    counted_qty: FiniteFloat = Field(ge=0)


class StockCountData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: uuid.UUID
    location_name: str = Field(min_length=1, max_length=255)
    location_type: str = Field(min_length=1, max_length=64)
    source_closed_event_id: StrictInt = Field(ge=1, le=2**63 - 1)
    counts: list[StockCountItem] = Field(min_length=1, max_length=1000)


class PeerStockAuditUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_company_id: uuid.UUID
    source_entity_id: str = Field(min_length=1, max_length=512)
    source_revision: int = Field(ge=1, le=2**63 - 1)
    data: StockCountData


@router.post("/inventory")
async def receive_peer_stock_audit(
    body: PeerStockAuditUpdate,
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
):
    from celerp.services.peer_inventory import receive_stock_audit
    scheme, _, credential = authorization.partition(" ")
    if scheme != "Peer" or not credential:
        raise HTTPException(401, "Invalid integration credentials")
    grant = await peer_access.authenticate(
        session, credential, source_company_id=body.source_company_id,
        scope="inventory")
    outcome = await receive_stock_audit(
        session, grant=grant, source_entity_id=body.source_entity_id,
        source_revision=body.source_revision, data=body.data.model_dump(mode="json"))
    await session.commit()
    return {"status": outcome}
