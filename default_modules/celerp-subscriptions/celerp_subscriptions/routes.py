# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user

VALID_FREQUENCIES = {"weekly", "biweekly", "monthly", "quarterly", "annually", "custom"}
VALID_DOC_TYPES = {"invoice", "purchase_order"}


class LineItem(BaseModel):
    item_id: str | None = None
    description: str | None = None
    quantity: float = 1
    unit_price: float = 0


class SubscriptionCreate(BaseModel):
    name: str
    contact_id: str | None = None
    doc_type: str  # invoice | purchase_order
    frequency: str  # weekly | biweekly | monthly | quarterly | annually | custom
    custom_interval_days: int | None = None  # required when frequency == "custom"
    start_date: str  # ISO date YYYY-MM-DD
    end_date: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    payment_terms: str | None = None
    shipping: float = 0
    discount: float = 0
    tax: float = 0
    idempotency_key: str | None = None


class SubscriptionPatch(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None


class SubImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class SubBatchImportRequest(BaseModel):
    records: list[SubImportRecord]


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


def _next_run_date(frequency: str, custom_interval_days: int | None, from_date: str) -> str:
    """Compute the next run date from a given date string."""
    d = date.fromisoformat(from_date)
    if frequency == "weekly":
        d += timedelta(weeks=1)
    elif frequency == "biweekly":
        d += timedelta(weeks=2)
    elif frequency == "monthly":
        month = d.month + 1
        year = d.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        d = date(year, month, day)
    elif frequency == "quarterly":
        month = d.month + 3
        year = d.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        d = date(year, month, day)
    elif frequency == "annually":
        try:
            d = date(d.year + 1, d.month, d.day)
        except ValueError:
            d = date(d.year + 1, d.month, d.day - 1)
    elif frequency == "custom":
        days = custom_interval_days or 30
        d += timedelta(days=days)
    return d.isoformat()


def _build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("")
    async def list_subscriptions(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        company_id: uuid.UUID = Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        rows = (
            await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "subscription",
                )
            )
        ).scalars().all()
        items = [r.state | {"id": r.entity_id} for r in rows]
        if status:
            items = [i for i in items if i.get("status") == status]
        return {"items": items[offset:offset + limit], "total": len(items)}

    @router.post("")
    async def create_subscription(
        payload: SubscriptionCreate,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        if payload.doc_type not in VALID_DOC_TYPES:
            raise HTTPException(status_code=422, detail=f"doc_type must be one of {VALID_DOC_TYPES}")
        if payload.frequency not in VALID_FREQUENCIES:
            raise HTTPException(status_code=422, detail=f"frequency must be one of {VALID_FREQUENCIES}")
        if payload.frequency == "custom" and not payload.custom_interval_days:
            raise HTTPException(status_code=422, detail="custom_interval_days required when frequency is custom")

        entity_id = f"sub:{uuid.uuid4()}"
        next_run = _next_run_date(payload.frequency, payload.custom_interval_days, payload.start_date)
        idem_key = payload.idempotency_key or str(uuid.uuid4())

        data = payload.model_dump(exclude={"idempotency_key"})
        data["next_run"] = next_run
        data["line_items"] = [li.model_dump() for li in payload.line_items]

        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="subscription",
            event_type="sub.created",
            data=data,
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=idem_key,
        )
        await session.commit()
        return {"id": entity_id, "event_id": entry.id, "next_run": next_run}

    @router.get("/import/template", response_class=PlainTextResponse, include_in_schema=False)
    async def import_subs_template():
        return PlainTextResponse(
            "entity_id,event_type,idempotency_key,name,doc_type,frequency,start_date,status\n",
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=subscriptions.csv"},
        )

    @router.get("/{entity_id:path}")
    async def get_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.entity_type != "subscription":
            raise HTTPException(status_code=404, detail="Subscription not found")
        return proj.state | {"id": proj.entity_id}

    @router.patch("/{entity_id:path}")
    async def patch_subscription(
        entity_id: str,
        payload: SubscriptionPatch,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.entity_type != "subscription":
            raise HTTPException(status_code=404, detail="Subscription not found")

        idem_key = payload.idempotency_key or str(uuid.uuid4())
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="subscription",
            event_type="sub.updated",
            data={"fields_changed": payload.fields_changed},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=idem_key,
        )
        await session.commit()
        return {"event_id": entry.id}

    @router.post("/{entity_id:path}/pause")
    async def pause_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.entity_type != "subscription":
            raise HTTPException(status_code=404, detail="Subscription not found")
        if proj.state.get("status") != "active":
            raise HTTPException(status_code=409, detail="Subscription is not active")

        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="subscription",
            event_type="sub.paused",
            data={},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
        )
        await session.commit()
        return {"event_id": entry.id}

    @router.post("/{entity_id:path}/resume")
    async def resume_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.entity_type != "subscription":
            raise HTTPException(status_code=404, detail="Subscription not found")
        if proj.state.get("status") != "paused":
            raise HTTPException(status_code=409, detail="Subscription is not paused")

        state = proj.state
        next_run = _next_run_date(
            state.get("frequency", "monthly"),
            state.get("custom_interval_days"),
            date.today().isoformat(),
        )
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="subscription",
            event_type="sub.resumed",
            data={"next_run": next_run},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
        )
        await session.commit()
        return {"event_id": entry.id, "next_run": next_run}

    @router.post("/import/batch", response_model=BatchImportResult)
    async def batch_import_subscriptions(
        body: SubBatchImportRequest,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> BatchImportResult:
        from sqlalchemy import select as _select
        from celerp.models.ledger import LedgerEntry

        keys = [r.idempotency_key for r in body.records]
        existing_keys = set((await session.execute(
            _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
        )).scalars().all())

        create_entity_ids = [r.entity_id for r in body.records if r.event_type == "sub.created"]
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
            if rec.event_type == "sub.created" and rec.entity_id in existing_entities:
                skipped += 1
                continue
            try:
                await emit_event(
                    session,
                    company_id=company_id,
                    entity_id=rec.entity_id,
                    entity_type="subscription",
                    event_type=rec.event_type,
                    data=rec.data,
                    actor_id=user.id,
                    location_id=None,
                    source=rec.source,
                    idempotency_key=rec.idempotency_key,
                    metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
                )
                existing_keys.add(rec.idempotency_key)
                if rec.event_type == "sub.created":
                    existing_entities.add(rec.entity_id)
                created += 1
            except Exception as exc:
                if len(errors) < 10:
                    errors.append(f"{rec.entity_id}: {exc}")

        await session.commit()
        return BatchImportResult(created=created, skipped=skipped, errors=errors)

    @router.post("/{entity_id:path}/generate")
    async def generate_now(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Manually trigger document generation for this subscription right now."""
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.entity_type != "subscription":
            raise HTTPException(status_code=404, detail="Subscription not found")

        state = proj.state
        doc_id = f"doc:{uuid.uuid4()}"
        today = date.today().isoformat()
        next_run = _next_run_date(
            state.get("frequency", "monthly"),
            state.get("custom_interval_days"),
            today,
        )

        line_items = state.get("line_items") or []
        doc_data = {
            "doc_type": state.get("doc_type", "invoice"),
            "contact_id": state.get("contact_id"),
            "line_items": line_items,
            "payment_terms": state.get("payment_terms"),
            "shipping": float(state.get("shipping", 0) or 0),
            "discount": float(state.get("discount", 0) or 0),
            "tax": float(state.get("tax", 0) or 0),
            "status": "draft",
            "source_subscription_id": entity_id,
        }

        total = float(state.get("total", 0) or 0)
        subtotal = float(state.get("subtotal", 0) or 0)

        if total > 0:
            doc_data["total"] = total
            doc_data["subtotal"] = subtotal if subtotal > 0 else total - doc_data["tax"] - doc_data["shipping"]
        elif line_items:
            computed = sum((float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)) for li in line_items)
            computed = computed - doc_data["discount"]
            computed = computed + doc_data["tax"] + doc_data["shipping"]
            doc_data["total"] = computed
            doc_data["subtotal"] = computed - doc_data["tax"] - doc_data["shipping"]

        doc_data["amount_outstanding"] = float(doc_data.get("total", 0) or 0)

        await emit_event(
            session,
            company_id=company_id,
            entity_id=doc_id,
            entity_type="doc",
            event_type="doc.created",
            data=doc_data,
            actor_id=user.id,
            location_id=None,
            source="subscription",
            idempotency_key=str(uuid.uuid4()),
        )

        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="subscription",
            event_type="sub.generated",
            data={"doc_id": doc_id, "generated_at": today, "next_run": next_run},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
        )
        await session.commit()
        return {"event_id": entry.id, "doc_id": doc_id, "next_run": next_run}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/subscriptions", tags=["subscriptions"])
