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

from celerp.config import ensure_instance_id, settings
from celerp.db import get_session
from celerp.gateway.state import relay_http_url
from celerp.services import supporter_badge as _badge
from celerp.services.auth import get_current_company_id, get_current_user, require_admin
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


# ── Supporter badge (claim via the relay verify flow, then store locally) ─────


@router.get("/badge/verify-url")
async def badge_verify_url(return_url: str, opt_in: int = 0, _user=Depends(get_current_user)) -> dict:
    """The relay verify-start URL to open in the browser. ``return_url`` is where the
    relay redirects back with the credential (the install's own page). ``opt_in`` carries
    the founder's choice to be listed publicly through to the relay verify flow."""
    from urllib.parse import urlencode

    iid = ensure_instance_id()
    q = urlencode({"instance_id": iid, "return_url": return_url, "opt_in": int(bool(opt_in))})
    return {"url": f"{relay_http_url()}/github/verify/start?{q}"}


@router.get("/badge")
async def get_badge(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The current user's supporter badge (or null) + the public founders wall URL."""
    return {
        "badge": _badge.to_dict(await _badge.get(session, user.id)),
        "wall_url": f"{relay_http_url()}/github/founders",
    }


@router.post("/badge")
async def claim_badge(
    payload: dict,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store the current user's badge from a relay-issued credential."""
    cred = (payload or {}).get("cred", "")
    if not cred:
        return {"error": "Missing credential."}
    try:
        badge = await _badge.claim(session, user.id, cred)
    except ValueError:
        return {"error": "Invalid credential."}
    return {"badge": _badge.to_dict(badge)}


@router.delete("/badge")
async def delete_badge(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Remove the badge and opt out of the relay founders wall (undo)."""
    await _badge.remove(session, user.id)
    return {"removed": True}
