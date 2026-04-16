# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


class ScanBody(BaseModel):
    code: str
    location_id: str | None = None
    raw: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class ScanBatchBody(BaseModel):
    scans: list[ScanBody]


class StartBatchBody(BaseModel):
    location_id: str | None = None


@router.post("/scan")
async def scan_once(payload: ScanBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entity_id = f"scan:{uuid.uuid4()}"
    try:
        location_uuid = uuid.UUID(payload.location_id) if payload.location_id else None
    except Exception:
        location_uuid = None
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="scan", event_type="scan.barcode",
        data=payload.model_dump(exclude_none=True), actor_id=user.id, location_id=location_uuid, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "scan_id": entity_id}


@router.post("/scan/batch")
async def scan_batch(payload: ScanBatchBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    created = 0
    for s in payload.scans:
        try:
            location_uuid = uuid.UUID(s.location_id) if s.location_id else None
        except Exception:
            location_uuid = None
        await emit_event(
            session, company_id=company_id, entity_id=f"scan:{uuid.uuid4()}", entity_type="scan", event_type="scan.barcode",
            data=s.model_dump(exclude_none=True), actor_id=user.id, location_id=location_uuid, source="api",
            idempotency_key=s.idempotency_key or str(uuid.uuid4()), metadata_={},
        )
        created += 1
    await session.commit()
    return {"created": created}


@router.get("/resolve/{code}")
async def resolve_scan(code: str, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
    for row in rows:
        st = row.state
        if row.entity_id == code or st.get("sku") == code or st.get("barcode") == code:
            actions = ["count"]
            if row.entity_type == "item":
                actions += ["transfer", "reserve", "memo_add", "mfg_consume"]
            return {"id": row.entity_id, "entity_type": row.entity_type, "state": st, "available_actions": actions}
    raise HTTPException(status_code=404, detail="Code not found")


@router.post("/batch")
async def start_batch(payload: StartBatchBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    batch_id = f"scan-batch:{uuid.uuid4()}"
    try:
        location_uuid = uuid.UUID(payload.location_id) if payload.location_id else None
    except Exception:
        location_uuid = None
    await emit_event(
        session, company_id=company_id, entity_id=batch_id, entity_type="scan", event_type="scan.nfc",
        data={"code": batch_id, "location_id": payload.location_id, "raw": {"action": "start"}},
        actor_id=user.id, location_id=location_uuid, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"batch_id": batch_id}


@router.post("/batch/{batch_id}/complete")
async def complete_batch(batch_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    await emit_event(
        session, company_id=company_id, entity_id=batch_id, entity_type="scan", event_type="scan.nfc",
        data={"code": batch_id, "raw": {"action": "complete"}}, actor_id=user.id, location_id=None, source="api",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"ok": True, "batch_id": batch_id, "discrepancies": []}
