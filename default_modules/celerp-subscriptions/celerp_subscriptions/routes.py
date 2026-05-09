# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-subscriptions API routes.

Subscription templates are stored as docs with doc_type "subscription_invoice"
or "subscription_po". This module provides convenience endpoints for listing
templates and lifecycle actions (generate, pause, resume, cancel).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user

SUBSCRIPTION_DOC_TYPES = frozenset({"subscription_invoice", "subscription_po"})
VALID_FREQUENCIES = frozenset({"weekly", "biweekly", "monthly", "quarterly", "annually", "custom"})


def _days_in_month(year: int, month: int) -> int:
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]


def _add_months(d: date, months: int) -> date:
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, min(d.day, _days_in_month(year, month)))


def _next_run_date(frequency: str, custom_interval_days: int | None, from_date: str) -> str:
    d = date.fromisoformat(from_date)
    if frequency == "weekly":
        d += timedelta(weeks=1)
    elif frequency == "biweekly":
        d += timedelta(weeks=2)
    elif frequency == "monthly":
        d = _add_months(d, 1)
    elif frequency == "quarterly":
        d = _add_months(d, 3)
    elif frequency == "annually":
        try:
            d = date(d.year + 1, d.month, d.day)
        except ValueError:
            d = date(d.year + 1, d.month, d.day - 1)
    else:  # custom
        d += timedelta(days=custom_interval_days or 30)
    return d.isoformat()


def _build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("")
    async def list_subscription_templates(
        direction: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        company_id: uuid.UUID = Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """List subscription templates (docs with subscription_invoice/subscription_po doc_type)."""
        rows = (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )).scalars().all()
        items = [r.state | {"id": r.entity_id} for r in rows
                 if r.state.get("doc_type") in SUBSCRIPTION_DOC_TYPES]
        if direction == "sales":
            items = [i for i in items if i.get("doc_type") == "subscription_invoice"]
        elif direction == "purchasing":
            items = [i for i in items if i.get("doc_type") == "subscription_po"]
        if status:
            items = [i for i in items if i.get("status") == status]
        total = len(items)
        return {"items": items[offset:offset + limit], "total": total}

    @router.post("/{entity_id}/generate")
    async def generate_now(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Generate a document from the subscription template immediately."""
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail="Subscription template not found")
        if proj.state.get("status") == "cancelled":
            raise HTTPException(status_code=409, detail="Cannot generate from a cancelled subscription")

        state = proj.state
        target_doc_type = "invoice" if state.get("doc_type") == "subscription_invoice" else "purchase_order"
        doc_id = f"doc:{uuid.uuid4()}"
        today = date.today().isoformat()

        line_items = list(state.get("line_items") or [])
        for li in line_items:
            li["line_total"] = float(li.get("quantity", 1)) * float(li.get("unit_price", 0))
        subtotal = sum(li["line_total"] for li in line_items)
        discount = float(state.get("discount") or 0)
        shipping = float(state.get("shipping") or 0)
        tax = float(state.get("tax") or 0)
        total = subtotal - discount + tax + shipping

        template_name = state.get("name") or entity_id
        auto_note = f"Auto-created from Subscription {template_name} (/subscriptions/{entity_id})"
        existing_notes = state.get("notes") or ""
        notes = f"{existing_notes}\n{auto_note}".strip() if existing_notes else auto_note

        doc_data = {
            "doc_type": target_doc_type,
            "contact_id": state.get("contact_id"),
            "line_items": line_items,
            "payment_terms": state.get("payment_terms"),
            "currency": state.get("currency"),
            "discount": discount,
            "shipping": shipping,
            "tax": tax,
            "subtotal": subtotal,
            "total": total,
            "amount_outstanding": total,
            "status": "draft",
            "subscription_id": entity_id,
            "notes": notes,
        }

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

        # Update template: next_run_date + append to generated_doc_ids
        next_run = _next_run_date(
            state.get("frequency", "monthly"),
            state.get("custom_interval_days"),
            today,
        )
        existing_ids = list(state.get("generated_doc_ids") or [])
        existing_ids.append(doc_id)

        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="doc",
            event_type="doc.updated",
            data={"fields_changed": {
                "next_run_date": {"new": next_run},
                "generated_doc_ids": {"new": existing_ids},
            }},
            actor_id=user.id,
            location_id=None,
            source="subscription",
            idempotency_key=str(uuid.uuid4()),
        )

        await session.commit()
        return {"doc_id": doc_id, "next_run_date": next_run}

    @router.post("/{entity_id}/pause")
    async def pause_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail="Subscription template not found")
        if proj.state.get("status") != "active":
            raise HTTPException(status_code=409, detail="Subscription is not active")
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="doc",
                         event_type="doc.updated", data={"fields_changed": {"status": {"new": "paused"}}},
                         actor_id=user.id, location_id=None, source="subscription",
                         idempotency_key=str(uuid.uuid4()))
        await session.commit()
        return {"ok": True}

    @router.post("/{entity_id}/resume")
    async def resume_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail="Subscription template not found")
        if proj.state.get("status") != "paused":
            raise HTTPException(status_code=409, detail="Subscription is not paused")
        next_run = _next_run_date(
            proj.state.get("frequency", "monthly"),
            proj.state.get("custom_interval_days"),
            date.today().isoformat(),
        )
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="doc",
                         event_type="doc.updated", data={"fields_changed": {
                             "status": {"new": "active"},
                             "next_run_date": {"new": next_run},
                         }},
                         actor_id=user.id, location_id=None, source="subscription",
                         idempotency_key=str(uuid.uuid4()))
        await session.commit()
        return {"ok": True, "next_run_date": next_run}

    @router.post("/{entity_id}/cancel")
    async def cancel_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail="Subscription template not found")
        if proj.state.get("status") == "cancelled":
            raise HTTPException(status_code=409, detail="Subscription is already cancelled")
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="doc",
                         event_type="doc.updated", data={"fields_changed": {"status": {"new": "cancelled"}}},
                         actor_id=user.id, location_id=None, source="subscription",
                         idempotency_key=str(uuid.uuid4()))
        await session.commit()
        return {"ok": True}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/subscriptions", tags=["subscriptions"])
