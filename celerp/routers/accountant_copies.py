# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company-owned accountant company copies."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.routers.migrations import user_owner
from celerp.services import company_copy
from celerp.services.auth import AuthContext

router = APIRouter(prefix="/accountant-copies", tags=["accountant-copies"])


class SendCopy(BaseModel):
    recipient: str = Field(min_length=3, max_length=320)
    note: str = Field(default="", max_length=1500)
    firm_name: str = Field(min_length=1, max_length=160)
    reply_to: str = Field(min_length=3, max_length=320)


@router.post("/send")
async def send_copy(body: SendCopy, ctx: AuthContext = Depends(user_owner)):
    return await company_copy.send_company_copy(
        ctx.company_id, recipient=body.recipient, note=body.note,
        firm_name=body.firm_name, reply_to=body.reply_to,
    )


@router.get("")
async def sent_copies(ctx: AuthContext = Depends(user_owner)):
    return {"items": await company_copy.list_company_copies(ctx.company_id)}


@router.post("/{handoff_id}/revoke")
async def revoke_copy(handoff_id: uuid.UUID, ctx: AuthContext = Depends(user_owner)):
    # The cloud uses the authenticated installation and object owner for this request.
    return await company_copy.revoke_company_copy(handoff_id)
