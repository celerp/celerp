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
GET    /api/labels/sources/{source}       A kind of record other modules register (label_source)
GET    /api/labels/source-rows            The label rows of such records
POST   /api/labels/print-source           Print labels for such records (returns PDF)
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
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import get_current_company_settings, require_permission, role_has_permission
from celerp.schemas.numbers import FiniteFloat
from celerp_labels.models import LabelTemplate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labels", tags=["labels"], dependencies=[Depends(get_current_user)])
# The most records one label sheet is printed for.
_MAX_SOURCE_IDS = 500


# ── Schemas ──────────────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    format: str = Field("40x30mm")
    orientation: str = Field("portrait")
    width_mm: FiniteFloat | None = None
    height_mm: FiniteFloat | None = None
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
    width_mm: FiniteFloat | None = None
    height_mm: FiniteFloat | None = None
    fields: list[dict] | None = None
    copies: int | None = Field(None, ge=1, le=100)
    notes: str | None = None


class BulkPrintBody(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    template_id: str | None = None


class SourcePrintBody(BaseModel):
    source: str
    ids: list[str] = Field(..., min_length=1, max_length=_MAX_SOURCE_IDS)
    template_id: str | None = None


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _seed_presets(session, company_id)
    items = [item.as_dict() for item in await _templates(session, company_id)]
    return {"items": items, "total": len(items)}


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateCreate,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("manage_labels"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    t = LabelTemplate(
        id=uuid.uuid4(),
        company_id=company_id,
        **body.model_dump(),
    )
    session.add(t)
    await session.commit()
    log.debug("Created label template %s for company %s", t.id, company_id)
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
    _: None = require_permission("manage_labels"),
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
    _: None = require_permission("manage_labels"),
    session: AsyncSession = Depends(get_session),
) -> None:
    t = await _get_or_404(session, company_id, template_id)
    await session.delete(t)
    await session.commit()


@router.post("/print/{entity_id}")
async def print_single(
    entity_id: str,
    request: Request,
    _: None = require_permission("view_inventory"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Generate a PDF label for a single item."""
    from celerp_labels.service import render_label_pdf

    template_id_str = request.query_params.get("template_id")
    template = await _resolve_template(session, company_id, template_id_str)
    item = await _fetch_item(session, company_id, entity_id, role, settings)
    pdf = render_label_pdf([item], template, await _unit_map(session, company_id))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="label-{entity_id}.pdf"'},
    )


@router.post("/bulk-print")
async def bulk_print(
    body: BulkPrintBody,
    _: None = require_permission("view_inventory"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Generate a PDF label sheet for multiple items."""
    from celerp_labels.service import render_label_pdf

    template = await _resolve_template(session, company_id, body.template_id)
    items = [await _fetch_item(session, company_id, eid, role, settings) for eid in body.entity_ids]
    pdf = render_label_pdf(items, template, await _unit_map(session, company_id))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="labels.pdf"'},
    )


# ── Other records (the label_source slot) ────────────────────────────────────

@router.get("/sources/{source}")
async def get_source(
    source: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A kind of record labels print for: the fields its rows carry and the company's
    templates its rows can fill."""
    entry = _label_source(source, role, settings)
    return {"entity_type": source, "label_key": entry["label_key"], "keys": entry["keys"],
            "templates": await _source_templates(session, company_id, entry)}


@router.get("/source-rows")
async def source_rows(
    source: str,
    ids: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The label rows of the records ``ids`` (comma separated), in the order asked."""
    entry = _label_source(source, role, settings)
    return {"rows": await _rows(session, company_id, entry, ids.split(","))}


@router.post("/print-source")
async def print_source(
    body: SourcePrintBody,
    company_id: uuid.UUID = Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """A PDF label sheet for records of a registered kind, on the chosen template, else the
    first of the company's that the rows can fill."""
    from celerp_labels.service import render_label_pdf

    entry = _label_source(body.source, role, settings)
    rows = await _rows(session, company_id, entry, body.ids)
    templates = await _source_templates(session, company_id, entry)
    template = next((tpl for tpl in templates if tpl["id"] == body.template_id), None) or templates[0]
    pdf = render_label_pdf(rows, template, await _unit_map(session, company_id))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{body.source}-labels.pdf"'})


def _label_source(source: str, role: str, settings: dict) -> dict:
    """The label_source entry for ``source`` from a module the company uses, refused when the
    role may not see those records."""
    from celerp.modules.registry import uses_module
    from celerp.modules.slots import get as get_slot

    entry = next((c for c in get_slot("label_source")
                  if c.get("entity_type") == source and uses_module(settings, c.get("_module"))), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Labels are not printed for {source} records.")
    if not role_has_permission(settings, role, entry["permission"]):
        raise HTTPException(status_code=403, detail="Your role cannot see these records, so it cannot print their labels.")
    return entry


async def _rows(session: AsyncSession, company_id: uuid.UUID, entry: dict, ids: list[str]) -> list[dict]:
    from celerp.modules.slots import resolve_handler

    wanted = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
    if not wanted:
        raise HTTPException(status_code=422, detail="Choose the records to print labels for.")
    if len(wanted) > _MAX_SOURCE_IDS:
        raise HTTPException(status_code=422, detail=f"Print at most {_MAX_SOURCE_IDS} labels at a time.")
    found = {r["id"]: r for r in await resolve_handler(entry["handler"])(
        session=session, company_id=company_id, ids=wanted)}
    return [found[i] for i in wanted if i in found]


async def _templates(session: AsyncSession, company_id: uuid.UUID) -> list[LabelTemplate]:
    """The company's templates, oldest first."""
    return (await session.execute(select(LabelTemplate).where(LabelTemplate.company_id == company_id)
                                  .order_by(LabelTemplate.created_at))).scalars().all()


async def _seed_presets(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Give a company with no templates the default presets, on first read, so every consumer
    (inventory/doc print dropdowns, the labels page) sees them without visiting Settings first."""
    if await _templates(session, company_id):
        return
    from .presets import PRESET_TEMPLATES
    for preset in PRESET_TEMPLATES:
        session.add(LabelTemplate(id=uuid.uuid4(), company_id=company_id, **TemplateCreate(**preset).model_dump()))
    await session.commit()


def _fitting(rows: list[LabelTemplate], keys: set[str]) -> list[dict]:
    fits = []
    for row in rows:
        tpl = row.as_dict()
        fields = [f.get("key") if isinstance(f, dict) else f for f in tpl.get("fields") or []]
        if fields and set(fields) <= keys:
            fits.append(tpl)
    return fits


async def _source_templates(session: AsyncSession, company_id: uuid.UUID, entry: dict) -> list[dict]:
    """The company's templates whose every field the source's rows carry, oldest first. Like
    the default presets, the kind's own template (its preset, in the reader's language) is
    added on first read, so its first labels print at once and it can be edited in Labels. It is
    named like the presets beside it: what it is, then its size."""
    from ui.i18n import t as translate

    keys = set(entry["keys"])
    fits = _fitting(await _templates(session, company_id), keys)
    if fits:
        return fits
    await _seed_presets(session, company_id)
    preset = entry["preset"]
    fields = [{**f, "label": translate(f["label"]) if f.get("label") else ""} for f in preset.get("fields") or []]
    session.add(LabelTemplate(id=uuid.uuid4(), company_id=company_id, **TemplateCreate(
        **{**preset, "name": f"{translate(preset['name'])} ({preset['format'].removesuffix('mm')})", "fields": fields}).model_dump()))
    await session.commit()
    return _fitting(await _templates(session, company_id), keys)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _unit_map(session: AsyncSession, company_id: uuid.UUID) -> dict[str, dict]:
    """Name-keyed company units; drives the derived weight/pieces label fields."""
    from celerp.services.units import build_unit_map, get_company_units

    return build_unit_map(await get_company_units(session, company_id))


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


async def _fetch_item(session: AsyncSession, company_id: uuid.UUID, entity_id: str,
                      role: str, settings: dict) -> dict:
    """Fetch item data from projections; fall back to minimal stub if not found.

    Flattened through the inventory serializer so labels print the same values every
    other surface shows: recipe-rolled cost and computed derived price lists included.
    Cost fields are stripped unless the caller holds view_inventory_costs, so a label
    never leaks cost to a role that cannot see it elsewhere.
    """
    from celerp.services.field_schema import get_effective_field_schema
    from celerp.services.pricing import get_price_config
    from celerp_inventory.routes import apply_item_visibility, flatten_item

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
        flat = flatten_item(proj.state, entity_id,
                             price_config=await get_price_config(session, company_id))
        field_schema = await get_effective_field_schema(session, company_id, category=flat.get("category"))
        can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
        return apply_item_visibility([flat], role, field_schema, can_see_costs)[0]
    return {"entity_id": entity_id, "name": entity_id, "sku": entity_id}


def setup_api_routes(app) -> None:
    """Entry point called by the module loader."""
    app.include_router(router)
    log.info("celerp-labels: API routes registered")
