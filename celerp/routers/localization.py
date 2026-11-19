# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Update localization (Settings > Taxes): preview, confirm and undo applying the company's
country pack to its tax list (``celerp.localization.apply``)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.localization import apply as localization
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.company_lock import locked_company
from celerp.services.permissions import require_permission
from ui.i18n import t

router = APIRouter(prefix="/settings/taxes/localization", tags=["settings"],
                   dependencies=[Depends(get_current_user)])


class Confirm(BaseModel):
    binding: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class Undo(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)


async def _company(session: AsyncSession, company_id):
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=t("error.company_unavailable"))
    return company


@router.post("/preview")
async def preview(company_id=Depends(get_current_company_id),
                  _: None = require_permission("manage_company_settings"),
                  session: AsyncSession = Depends(get_session)) -> dict:
    return localization.preview(dict((await _company(session, company_id)).settings or {}))


@router.post("/confirm")
async def confirm(payload: Confirm, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                  _: None = require_permission("manage_company_settings"),
                  session: AsyncSession = Depends(get_session)) -> dict:
    company = await _company(session, company_id)
    operation_id = await localization.apply(session, company, user.id, payload.binding, payload.idempotency_key)
    await session.commit()
    return {"operation_id": operation_id}


@router.post("/undo")
async def undo(payload: Undo, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
               _: None = require_permission("manage_company_settings"),
               session: AsyncSession = Depends(get_session)) -> dict:
    company = await _company(session, company_id)
    kept = await localization.undo(session, company, user.id, payload.operation_id)
    await session.commit()
    return {"kept": kept}
