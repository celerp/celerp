# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-labels API routes.

Registered into the FastAPI app by the module loader.
All routes are prefixed with /api/labels.

Endpoints
---------
GET    /api/labels/templates              List label templates for current company
POST   /api/labels/templates              Create a label template
GET    /api/labels/templates/{id}         Get one template
PUT    /api/labels/templates/{id}         Update a template
DELETE /api/labels/templates/{id}         Delete a template
POST   /api/labels/print/{entity_id}      Print a single item label (returns PDF)
POST   /api/labels/bulk-print             Print labels for multiple items (returns PDF)
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user
from celerp_labels.models import LabelTemplate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labels", tags=["labels"], dependencies=[Depends(get_current_user)])


# ── Schemas ──────────────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    format: str = Field("40x30mm")
    orientation: str = Field("portrait")
    width_mm: float | None = None
    height_mm: float | None = None
    fields: list[dict] = Field(default_factory=lambda: [
        {"key": "name", "label": "Name", "type": "text"},
        {"key": "sku", "label": "SKU", "type": "text"},
        {"key": "barcode", "label": "Barcode", "type": "barcode"},
    ])
    copies: int = Field(1, ge=1, le=100)
    notes: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    format: str | None = None
    orientation: str | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    fields: list[dict] | None = None
    copies: int | None = Field(None, ge=1, le=100)
    notes: str | None = None


class BulkPrintBody(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    template_id: str | None = None


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(
            select(LabelTemplate)
            .where(LabelTemplate.company_id == company_id)
            .order_by(LabelTemplate.created_at)
        )
    ).scalars().all()
    items = [item.as_dict() for item in rows]
    return {"items": items, "total": len(items)}


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateCreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    t = LabelTemplate(
        id=uuid.uuid4(),
        company_id=company_id,
        **body.model_dump(),
    )
    session.add(t)
    await session.commit()
    log.info("Created label template %s for company %s", t.id, company_id)
    return t.as_dict()


@router.get("/templates/{template_id}")
async def get_template(
    template_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    t = await _get_or_404(session, company_id, template_id)
    return t.as_dict()


@router.put("/templates/{template_id}")
async def update_template(
    template_id: uuid.UUID,
    body: TemplateUpdate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    t = await _get_or_404(session, company_id, template_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(t, k, v)
    await session.commit()
    return t.as_dict()


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    t = await _get_or_404(session, company_id, template_id)
    await session.delete(t)
    await session.commit()


@router.post("/print/{entity_id}")
async def print_single(
    entity_id: str,
    request: Request,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Generate a PDF label for a single item."""
    from celerp_labels.service import render_label_pdf

    template_id_str = request.query_params.get("template_id")
    template = await _resolve_template(session, company_id, template_id_str)
    item = await _fetch_item(session, company_id, entity_id)
    pdf = render_label_pdf([item], template)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="label-{entity_id}.pdf"'},
    )


@router.post("/bulk-print")
async def bulk_print(
    body: BulkPrintBody,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Generate a PDF label sheet for multiple items."""
    from celerp_labels.service import render_label_pdf

    template = await _resolve_template(session, company_id, body.template_id)
    items = [await _fetch_item(session, company_id, eid) for eid in body.entity_ids]
    pdf = render_label_pdf(items, template)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="labels.pdf"'},
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(session: AsyncSession, company_id: uuid.UUID, template_id: uuid.UUID) -> LabelTemplate:
    t = (
        await session.execute(
            select(LabelTemplate).where(
                LabelTemplate.id == template_id,
                LabelTemplate.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


async def _resolve_template(
    session: AsyncSession, company_id: uuid.UUID, template_id_str: str | None
) -> dict:
    """Resolve template by id or fall back to first available, then built-in default."""
    if template_id_str:
        try:
            tid = uuid.UUID(template_id_str)
            t = (
                await session.execute(
                    select(LabelTemplate).where(
                        LabelTemplate.id == tid,
                        LabelTemplate.company_id == company_id,
                    )
                )
            ).scalar_one_or_none()
            if t:
                return t.as_dict()
        except ValueError:
            pass

    first = (
        await session.execute(
            select(LabelTemplate)
            .where(LabelTemplate.company_id == company_id)
            .order_by(LabelTemplate.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if first:
        return first.as_dict()

    return {
        "id": "default",
        "company_id": str(company_id),
        "name": "Default",
        "format": "40x30mm",
        "orientation": "portrait",
        "width_mm": None,
        "height_mm": None,
        "fields": [
            {"key": "name", "label": "Name", "type": "text"},
            {"key": "sku", "label": "SKU", "type": "text"},
            {"key": "barcode", "label": "Barcode", "type": "barcode"},
        ],
        "copies": 1,
    }


async def _fetch_item(session: AsyncSession, company_id: uuid.UUID, entity_id: str) -> dict:
    """Fetch item data from projections; fall back to minimal stub if not found."""
    proj = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_id == entity_id,
                Projection.entity_type == "item",
            )
        )
    ).scalar_one_or_none()
    if proj and proj.state:
        return dict(proj.state)
    return {"entity_id": entity_id, "name": entity_id, "sku": entity_id}


def setup_api_routes(app) -> None:
    """Entry point called by the module loader."""
    app.include_router(router)
    log.info("celerp-labels: API routes registered")
