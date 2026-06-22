# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""GitHub-star CTA endpoints.

``GET /stars/cta`` is readable by any authenticated user (the footer shows it to
everyone). ``POST /stars/dismiss`` is admin-only and install-level (one ask for the
whole install), so it lives here rather than under the admin-gated /system router.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.services.auth import get_current_company_id, require_admin
from celerp.services.runtime_state import dismiss_star_prompt, star_prompt_dismissed
from celerp.services.star_cta import get_star_cta, neutral_cta

router = APIRouter()


@router.get("/cta")
async def star_cta(
    medium: str = "footer",
    _company=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the relay-resolved CTA for ``medium`` (or the neutral link if the relay
    is unreachable), plus the install-level dismissed flag. ``{}`` when disabled."""
    if not settings.star_cta_enabled:
        return {}
    cta = await get_star_cta(medium) or neutral_cta(medium)
    return {**cta, "dismissed": await star_prompt_dismissed(session)}


@router.post("/dismiss")
async def dismiss(
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Dismiss the GitHub-star ask for the whole install (onboarding/milestone cards)."""
    await dismiss_star_prompt(session)
    return {"dismissed": True}
