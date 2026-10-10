# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-subscriptions API routes.

Subscription templates are stored as docs with doc_type "subscription_invoice"
or "subscription_po". This module provides convenience endpoints for listing
templates and lifecycle actions (generate, pause, resume, cancel).
"""
from __future__ import annotations

import copy
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.company_lock import locked_company
from celerp.services.permissions import require_permission
from celerp.services.terms import resolve_document_terms
from celerp_docs.doc_money import document_money
from celerp_docs.routes import finalize_document
from celerp_docs.sequences import next_draft_ref
from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES, search_subscription_templates

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


def _compute_due_date(issue_date: date, payment_terms: str | None, company: Company) -> str | None:
    """Return ISO due_date by looking up payment_terms days in company settings."""
    if not payment_terms:
        return None
    terms_list: list[dict] = (company.settings or {}).get("payment_terms", [])
    for t in terms_list:
        if t.get("name") == payment_terms:
            days = int(t.get("days") or 0)
            return (issue_date + timedelta(days=days)).isoformat() if days else None
    return None


class GenerateBody(BaseModel):
    idempotency_key: str | None = Field(None, max_length=255)


def _build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("")
    async def list_subscription_templates(
        direction: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        company_id: uuid.UUID = Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """List subscription templates (docs with subscription_invoice/subscription_po doc_type)."""
        items, total = await search_subscription_templates(
            session, company_id, direction=direction, status=status, q=q, limit=limit, offset=offset
        )
        return {"items": items, "total": total}

    @router.post("/{entity_id}/generate")
    async def generate_now(
        entity_id: str,
        body: GenerateBody | None = None,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Create the template's document and finalize it; a retry with the same key returns the first result."""
        company = await locked_company(session, company_id)
        key = (body and body.idempotency_key) or str(uuid.uuid4())
        if (earlier := await find_event_by_idempotency(session, company_id, key)) is not None:
            if (earlier.event_type != "doc.updated" or earlier.entity_id != entity_id
                    or "result" not in (earlier.metadata_ or {})):
                raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
            return earlier.metadata_["result"]
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail="Subscription template not found")
        if proj.state.get("status") == "cancelled":
            raise HTTPException(status_code=409, detail="Cannot generate from a cancelled subscription")

        state = proj.state
        target_doc_type = "invoice" if state.get("doc_type") == "subscription_invoice" else "purchase_order"
        today = date.today()

        # Resolve contact name from projection
        contact_id = state.get("contact_id")
        contact_name = ""
        contact_company_name = ""
        contact_state: dict = {}
        if contact_id:
            contact_proj = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
            if contact_proj and contact_proj.entity_type == "contact":
                contact_state = contact_proj.state or {}
                contact_name = contact_state.get("name") or ""
                contact_company_name = contact_state.get("company_name") or ""

        pf_ref = next_draft_ref(company, target_doc_type)
        doc_entity_id = f"doc:{pf_ref}"

        line_items = copy.deepcopy(list(state.get("line_items") or []))
        payment_terms = state.get("payment_terms")
        due_date = _compute_due_date(today, payment_terms, company)
        template_name = state.get("name") or entity_id
        auto_note = f"Auto-created from subscription template {template_name}"
        existing_notes = state.get("notes") or ""
        notes = f"{existing_notes}\n{auto_note}".strip() if existing_notes else auto_note

        currency = state.get("currency") or (company.settings or {}).get("currency", "USD")
        money_inputs = {k: state[k] for k in ("discount", "discount_type", "shipping", "tax", "tax_rate", "doc_taxes")
                        if k in state}
        money = document_money(money_inputs, line_items, currency, keep_unrated_tax=True)

        doc_data: dict = {
            "doc_type": target_doc_type,
            "ref_id": pf_ref,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "contact_company_name": contact_company_name,
            "line_items": line_items,
            "payment_terms": payment_terms,
            "currency": currency,
            **money_inputs,
            **money,
            "amount_outstanding": money["total"],
            "issue_date": today.isoformat(),
            "subscription_id": entity_id,
            "notes": notes,
        }
        if due_date:
            doc_data["due_date"] = due_date

        # Use the same canonical creation policy as ordinary documents. Legacy
        # subscription templates may still carry `terms`, but generated docs do not.
        doc_data.update(resolve_document_terms(
            state, company.settings or {}, target_doc_type,
        ))
        if "customer_note" in state:
            doc_data["customer_note"] = state.get("customer_note") or ""

        await emit_event(
            session,
            company_id=company_id,
            entity_id=doc_entity_id,
            entity_type="doc",
            event_type="doc.created",
            data=doc_data,
            actor_id=user.id,
            location_id=None,
            source="subscription",
            idempotency_key=str(uuid.uuid4()),
        )
        await session.flush()
        await finalize_document(doc_entity_id, company_id, user, session, commit=False)
        await session.flush()
        final_ref = (await session.get(Projection, {"company_id": company_id, "entity_id": doc_entity_id})).state["ref_id"]

        # Update template: next_run_date + append to generated_doc_ids
        today_str = today.isoformat()
        next_run = _next_run_date(
            state.get("frequency", "monthly"),
            state.get("custom_interval_days"),
            today_str,
        )
        existing_ids = list(state.get("generated_doc_ids") or [])
        existing_ids.append(doc_entity_id)
        result = {"doc_id": doc_entity_id, "ref_id": final_ref, "next_run_date": next_run}

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
            idempotency_key=key,
            metadata_={"result": result},
        )

        await session.commit()
        return result

    @router.post("/{entity_id}/pause")
    async def pause_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
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
        _: None = require_permission("edit_documents"),
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
        _: None = require_permission("edit_documents"),
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

    @router.post("/{entity_id}/activate")
    async def activate_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Promote a draft subscription template to active, computing next_run_date."""
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail="Subscription template not found")
        if proj.state.get("status") != "draft":
            raise HTTPException(status_code=409, detail="Only draft subscriptions can be activated")
        frequency = proj.state.get("frequency", "monthly")
        if frequency not in VALID_FREQUENCIES:
            raise HTTPException(status_code=422, detail="Frequency must be set before activating")
        start = proj.state.get("start_date") or date.today().isoformat()
        next_run = _next_run_date(frequency, proj.state.get("custom_interval_days"), start)
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="doc",
                         event_type="doc.updated", data={"fields_changed": {
                             "status": {"new": "active"},
                             "start_date": {"new": start},
                             "next_run_date": {"new": next_run},
                         }},
                         actor_id=user.id, location_id=None, source="subscription",
                         idempotency_key=str(uuid.uuid4()))
        await session.commit()
        return {"ok": True, "next_run_date": next_run}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/subscriptions", tags=["subscriptions"])
