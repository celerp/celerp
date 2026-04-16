# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-manufacturing API routes.

Registered into the FastAPI app by the module loader via setup_api_routes().
All routes are mounted under /manufacturing (set in PLUGIN_MANIFEST or by the
loader's register_api_routes calling setup_api_routes with the app directly).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(prefix="/manufacturing", dependencies=[Depends(get_current_user)], tags=["manufacturing"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class MfgInput(BaseModel):
    item_id: str
    quantity: float


class MfgOutput(BaseModel):
    sku: str
    name: str
    quantity: float
    category: str | None = None


class MfgOrderCreate(BaseModel):
    description: str
    order_type: str = "assembly"
    bom_id: str | None = None
    inputs: list[MfgInput] = Field(default_factory=list)
    expected_outputs: list[MfgOutput] = Field(default_factory=list)
    location_id: str | None = None
    assigned_to: str | None = None
    due_date: str | None = None
    estimated_cost: float | None = None
    notes: str | None = None
    idempotency_key: str | None = None


class ConsumeBody(BaseModel):
    item_id: str
    quantity: float
    idempotency_key: str | None = None


class StepBody(BaseModel):
    step_id: str
    notes: str | None = None
    idempotency_key: str | None = None


class CompleteBody(BaseModel):
    actual_outputs: list[MfgOutput] | None = None
    waste_quantity: float | None = None
    waste_unit: str | None = None
    waste_reason: str | None = None
    labor_hours: float | None = None
    idempotency_key: str | None = None


class CancelBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class MfgImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class MfgBatchImportRequest(BaseModel):
    records: list[MfgImportRecord]


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


class BOMComponent(BaseModel):
    item_id: str | None = None
    sku: str
    qty: float
    unit: str = "pieces"


class BOMCreate(BaseModel):
    name: str
    output_item_id: str | None = None
    output_qty: float = 1.0
    components: list[BOMComponent] = Field(default_factory=list)


class BOMUpdate(BaseModel):
    name: str | None = None
    output_item_id: str | None = None
    output_qty: float | None = None
    components: list[BOMComponent] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_bom(session: AsyncSession, company_id, bom_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": bom_id})
    if row is None or row.entity_type != "bom" or row.state.get("deleted"):
        raise HTTPException(status_code=404, detail="BOM not found")
    return row


async def _get_order(session: AsyncSession, company_id, order_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": order_id})
    if row is None or row.entity_type != "mfg_order":
        raise HTTPException(status_code=404, detail="Manufacturing order not found")
    return row


# ---------------------------------------------------------------------------
# BOM endpoints
# ---------------------------------------------------------------------------

@router.get("/boms")
async def list_boms(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "bom",
            )
        )
    ).scalars().all()
    items = [r.state | {"bom_id": r.entity_id} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/boms")
async def create_bom(
    payload: BOMCreate,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="BOM name is required")
    bom_id = f"bom:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=bom_id,
        entity_type="bom",
        event_type="bom.created",
        data=payload.model_dump(),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "bom_id": bom_id}


@router.get("/boms/{bom_id}")
async def get_bom(
    bom_id: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_bom(session, company_id, bom_id)
    return row.state | {"bom_id": row.entity_id}


@router.put("/boms/{bom_id}")
async def update_bom(
    bom_id: str,
    payload: BOMUpdate,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_bom(session, company_id, bom_id)
    update_data = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=bom_id,
        entity_type="bom",
        event_type="bom.updated",
        data=update_data,
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "bom_id": bom_id}


@router.delete("/boms/{bom_id}")
async def delete_bom(
    bom_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_bom(session, company_id, bom_id)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=bom_id,
        entity_type="bom",
        event_type="bom.deleted",
        data={},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Import endpoints
# ---------------------------------------------------------------------------

@router.get("/import/template", response_class=PlainTextResponse, include_in_schema=False)
async def import_manufacturing_template():
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=manufacturing.csv"},
    )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_manufacturing(
    body: MfgBatchImportRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from sqlalchemy import select as _select
    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in body.records]
    existing_keys = set((await session.execute(
        _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
    )).scalars().all())

    create_entity_ids = [r.entity_id for r in body.records if r.event_type in {"bom.created", "mfg.order.created"}]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            _select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    created = skipped = 0
    errors: list[str] = []
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            skipped += 1
            continue
        if rec.event_type in {"bom.created", "mfg.order.created"} and rec.entity_id in existing_entities:
            skipped += 1
            continue
        try:
            entity_type = "bom" if rec.event_type.startswith("bom.") else "mfg_order"
            await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type=entity_type,
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            if rec.event_type in {"bom.created", "mfg.order.created"}:
                existing_entities.add(rec.entity_id)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------
# Manufacturing order endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_orders(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "mfg_order",
        )
    )).scalars().all()
    items = [r.state | {"id": r.entity_id} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("")
async def create_order(
    payload: MfgOrderCreate,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not payload.description.strip():
        raise HTTPException(status_code=422, detail="description is required")
    if len(payload.inputs) == 0:
        raise HTTPException(status_code=409, detail="Cannot create/start order with no inputs")
    entity_id = f"mfg:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="mfg_order",
        event_type="mfg.order.created",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=uuid.UUID(payload.location_id) if payload.location_id else None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    return row.state | {"id": row.entity_id}


@router.post("/{order_id}/start")
async def start_order(
    order_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Order already completed")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.order.started",
        data={"started_by": str(user.id)},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{order_id}/consume")
async def consume_input(
    order_id: str,
    payload: ConsumeBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot consume for closed order")

    item = await session.get(Projection, {"company_id": company_id, "entity_id": payload.item_id})
    if item is None or item.entity_type != "item":
        raise HTTPException(status_code=404, detail="Input item not found")
    available = float(item.state.get("quantity", 0) or 0)
    reserved = float(item.state.get("reserved_quantity", 0) or 0)
    if payload.quantity > max(0.0, available - reserved) + 1e-9:
        raise HTTPException(status_code=409, detail="Cannot consume more than available quantity")

    item_ev = await emit_event(
        session,
        company_id=company_id,
        entity_id=payload.item_id,
        entity_type="item",
        event_type="item.consumed",
        data={"quantity_consumed": payload.quantity},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={"manufacturing_order_id": order_id},
    )
    await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.step.completed",
        data={"step_id": f"consume:{payload.item_id}", "notes": f"qty={payload.quantity}"},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": item_ev.id}


@router.post("/{order_id}/step")
async def complete_step(
    order_id: str,
    payload: StepBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_order(session, company_id, order_id)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.step.completed",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{order_id}/complete")
async def complete_order(
    order_id: str,
    payload: CompleteBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    state = row.state
    if state.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Cannot complete an order twice")

    consumed_item_ids = set()
    for step in state.get("steps_completed", []):
        if isinstance(step, str) and step.startswith("consume:"):
            consumed_item_ids.add(step.split(":", 1)[1])
    required_item_ids = {x.get("item_id") for x in state.get("inputs", []) if x.get("item_id")}
    if required_item_ids and not required_item_ids.issubset(consumed_item_ids):
        raise HTTPException(status_code=409, detail="Cannot complete order without consuming all inputs")

    outputs = payload.actual_outputs or [MfgOutput(**o) for o in state.get("expected_outputs", [])]
    for out in outputs:
        new_item_id = f"item:{uuid.uuid4()}"
        await emit_event(
            session,
            company_id=company_id,
            entity_id=new_item_id,
            entity_type="item",
            event_type="item.created",
            data={
                "sku": out.sku,
                "name": out.name,
                "quantity": 0,
                "category": out.category,
                "location_id": state.get("location_id"),
                "manufacturing_order_id": order_id,
            },
            actor_id=user.id,
            location_id=state.get("location_id"),
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"manufacturing_order_id": order_id},
        )
        await emit_event(
            session,
            company_id=company_id,
            entity_id=new_item_id,
            entity_type="item",
            event_type="item.produced",
            data={"quantity_produced": out.quantity},
            actor_id=user.id,
            location_id=state.get("location_id"),
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"manufacturing_order_id": order_id},
        )

    mfg_entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.order.completed",
        data={
            "completed_by": str(user.id),
            "actual_outputs": [o.model_dump(exclude_none=True) for o in outputs],
            "waste": (
                {"quantity": payload.waste_quantity, "unit": payload.waste_unit, "reason": payload.waste_reason}
                if payload.waste_quantity is not None else None
            ),
            "labor_hours": payload.labor_hours,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )

    input_cost = float(state.get("estimated_cost", 0) or 0)
    waste_cost = 0.0
    if payload.waste_quantity and payload.waste_quantity > 0:
        total_input_qty = sum(float(i.get("quantity", 0) or 0) for i in state.get("inputs", [])) or 0.0
        if total_input_qty > 0:
            waste_cost = input_cost * (float(payload.waste_quantity) / total_input_qty)

    await auto_je.create_for_mfg_completed(
        session,
        company_id=company_id,
        user_id=user.id,
        order_id=order_id,
        input_cost=input_cost,
        waste_cost=waste_cost,
    )
    await session.commit()
    return {"event_id": mfg_entry.id}


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    payload: CancelBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_order(session, company_id, order_id)
    if row.state.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Cannot cancel completed order")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=order_id,
        entity_type="mfg_order",
        event_type="mfg.order.cancelled",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

def setup_api_routes(app) -> None:
    """Called by the module loader to register manufacturing routes."""
    app.include_router(router)
