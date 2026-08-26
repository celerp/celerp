# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

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


def _item_actions(entity_type: str) -> list[str]:
    actions = ["count"]
    if entity_type == "item":
        actions += ["transfer", "reserve", "memo_add", "mfg_consume"]
    return actions


@router.get("/resolve/{code}")
async def resolve_scan(code: str, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Resolve a scanned/typed code to one item, a chooser (ambiguous SKU), or a
    non-item entity (direct entity_id scan).

    Barcode (unique) wins; an exact SKU may map to N physical lots. When a SKU is
    ambiguous, return a self-sufficient chooser: each candidate carries batch_no,
    location and on-hand so the user can pick a lot even when barcode is blank
    (received parcels often have none).
    """
    from celerp.models.company import Location
    from celerp_inventory.routes import duplicate_barcode_detail, resolve_item_by_code

    res = await resolve_item_by_code(session, company_id, code)
    if res.duplicate_barcode:
        raise HTTPException(status_code=409, detail=duplicate_barcode_detail(code))
    if res.ambiguous:
        loc_rows = (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
        loc_map = {str(r.id): r.name for r in loc_rows}
        candidates = []
        for r in res.matches:
            st = r.state or {}
            lid = st.get("location_id")
            candidates.append({
                "id": r.entity_id,
                "sku": st.get("sku"),
                "name": st.get("name"),
                "batch_no": st.get("batch_no"),
                "barcode": st.get("barcode"),
                "location_id": lid,
                "location_name": loc_map.get(str(lid)) if lid else None,
                "quantity": float(st.get("quantity", 0) or 0),
            })
        return {"ambiguous": True, "kind": "sku", "code": code, "candidates": candidates}

    row = res.one
    if row is None:
        # Non-item entities (scanning a doc/list/etc.) resolve by exact entity_id.
        row = await session.get(Projection, {"company_id": company_id, "entity_id": code})
    if row is not None:
        return {"id": row.entity_id, "entity_type": row.entity_type, "state": row.state,
                "available_actions": _item_actions(row.entity_type)}
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
