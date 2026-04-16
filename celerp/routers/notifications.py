# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Notification API router.

Endpoints:
  GET    /notifications              List notifications (X-Unread-Count header)
  POST   /notifications/{id}/read    Mark one read
  POST   /notifications/read-all     Mark all read
  GET    /notifications/stream       SSE endpoint for real-time push
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.notifications import service as notif_svc
from celerp.notifications.sse import event_stream
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    category: str
    title: str
    body: str
    action_url: str | None
    priority: str
    read: bool
    created_at: str

    model_config = {"from_attributes": True}


class NotificationList(BaseModel):
    items: list[NotificationOut]
    unread_count: int


@router.get("", response_model=NotificationList)
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
):
    """List notifications for the current user, newest first."""
    items = await notif_svc.list_notifications(
        session, company_id, user.id, limit=limit, offset=offset,
    )
    unread = await notif_svc.get_unread_count(session, company_id, user.id)
    return NotificationList(
        items=[
            NotificationOut(
                id=n.id,
                category=n.category,
                title=n.title,
                body=n.body,
                action_url=n.action_url,
                priority=n.priority,
                read=n.read,
                created_at=n.created_at.isoformat(),
            )
            for n in items
        ],
        unread_count=unread,
    )


@router.post("/{notification_id}/read", status_code=204)
async def mark_read(
    notification_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
):
    """Mark a single notification as read."""
    found = await notif_svc.mark_read(session, notification_id, company_id)
    if not found:
        raise HTTPException(status_code=404, detail="Notification not found")
    await session.commit()


@router.post("/read-all", status_code=204)
async def mark_all_read(
    session: AsyncSession = Depends(get_session),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
):
    """Mark all notifications as read for the current user."""
    await notif_svc.mark_all_read(session, company_id, user.id)
    await session.commit()


@router.get("/stream")
async def notification_stream(
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
):
    """SSE endpoint for real-time notification push.

    Sends keepalive comments every 30s. Events are JSON-encoded.
    Max 5 concurrent connections per user (oldest evicted).
    """
    return StreamingResponse(
        event_stream(company_id, user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
