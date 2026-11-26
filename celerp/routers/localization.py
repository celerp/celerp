# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Update localization (Settings > Taxes): preview, confirm and undo applying the company's
country pack to its tax list (``celerp.localization.apply``), and the item rate review an update
offers (``celerp.localization.rate_review``)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.localization import apply as localization
from celerp.localization import rate_review
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.company_lock import locked_company
from celerp.services.permissions import require_permission
from ui.i18n import t

router = APIRouter(prefix="/settings/taxes", tags=["settings"],
                   dependencies=[Depends(get_current_user)])


class Confirm(BaseModel):
    binding: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class Undo(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)


class ReviewApply(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=5000)
    binding: str = Field(min_length=1, max_length=128)


async def _company(session: AsyncSession, company_id):
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=t("error.company_unavailable"))
    return company


@router.post("/localization/preview", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-confirm": False}, summary="Show what updating the tax list from the company's country pack would add, change or retire, before anything is changed.")
async def preview(company_id=Depends(get_current_company_id),
                  _: None = require_permission("manage_company_settings"),
                  session: AsyncSession = Depends(get_session)) -> dict:
    return localization.preview(dict((await _company(session, company_id)).settings or {}))


@router.post("/localization/confirm", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True, "x-celerp-agent-key": "idempotency_key"}, summary="Update the tax list from the company's country pack, as the preview showed it.")
async def confirm(payload: Confirm, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                  _: None = require_permission("manage_company_settings"),
                  session: AsyncSession = Depends(get_session)) -> dict:
    company = await _company(session, company_id)
    operation_id = await localization.apply(session, company, user.id, payload.binding, payload.idempotency_key)
    await session.commit()
    return {"operation_id": operation_id}


@router.post("/localization/undo", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/settings/sales"}, summary="Undo a tax list update from the country pack, keeping any tax code changed since.")
async def undo(payload: Undo, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
               _: None = require_permission("manage_company_settings"),
               session: AsyncSession = Depends(get_session)) -> dict:
    company = await _company(session, company_id)
    kept = await localization.undo(session, company, user.id, payload.operation_id)
    await session.commit()
    return {"kept": kept}


@router.get("/rate-review/{operation_id}", openapi_extra={"x-celerp-agent": True}, summary="List the items whose tax rate a tax list update suggests changing.")
async def review(operation_id: str, company_id=Depends(get_current_company_id),
                 _: None = require_permission("edit_inventory"),
                 session: AsyncSession = Depends(get_session)) -> dict:
    return await rate_review.propose(session, company_id, operation_id)


@router.post("/rate-review/{operation_id}/apply", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/settings/sales"}, summary="Move the chosen items to the tax rate a tax list update suggests.")
async def review_apply(operation_id: str, payload: ReviewApply, company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), _: None = require_permission("edit_inventory"),
                       session: AsyncSession = Depends(get_session)) -> dict:
    await _company(session, company_id)
    skipped = await rate_review.apply(session, company_id, user.id, operation_id, payload.item_ids, payload.binding)
    await session.commit()
    return {"skipped": skipped}


@router.post("/rate-review/{operation_id}/undo", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/settings/sales"}, summary="Undo moving items to a suggested tax rate, keeping any item changed since.")
async def review_undo(operation_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                      _: None = require_permission("edit_inventory"),
                      session: AsyncSession = Depends(get_session)) -> dict:
    await _company(session, company_id)
    kept = await rate_review.undo(session, company_id, user.id, operation_id)
    await session.commit()
    return {"kept": kept}
