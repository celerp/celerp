# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Supporter badge: claim (from a relay credential), read, and remove (opt-out).

The relay's founders registry is authoritative for anything public; this stores a
local copy used only to render the badge next to the user's name. Per the trust
model (relay spec §0.4) the install decodes the credential's claims for display and
does NOT cryptographically validate it - forging a local badge only affects the
forger's own self-hosted instance.
"""
from __future__ import annotations

import uuid

import httpx
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.gateway.state import relay_http_url
from celerp.models.supporter import SupporterBadge


def _claims(cred: str) -> dict:
    """Decode (unverified) the relay credential's claims. Raises ValueError if it is
    not a well-formed supporter credential."""
    try:
        c = jwt.get_unverified_claims(cred)
    except JWTError as exc:
        raise ValueError("malformed credential") from exc
    if c.get("typ") != "supporter_cred" or "login" not in c or "ordinal" not in c:
        raise ValueError("not a supporter credential")
    return c


def label_for(tier: str, ordinal: int) -> str:
    if tier == "founding":
        return f"Founding Supporter #{ordinal}"
    if tier == "early":
        return "Early Supporter"
    return "Supporter"


async def claim(session: AsyncSession, user_id: uuid.UUID, cred: str) -> SupporterBadge:
    """Store or refresh the current user's badge from a relay credential."""
    c = _claims(cred)
    badge = await session.get(SupporterBadge, user_id)
    if badge is None:
        badge = SupporterBadge(user_id=user_id)
        session.add(badge)
    badge.github_login = str(c["login"])
    badge.tier = str(c.get("tier", "supporter"))
    badge.ordinal = int(c["ordinal"])
    badge.credential = cred
    await session.commit()
    return badge


async def get(session: AsyncSession, user_id: uuid.UUID) -> SupporterBadge | None:
    return await session.get(SupporterBadge, user_id)


async def remove(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Remove the local badge and best-effort opt out on the relay (undo)."""
    badge = await session.get(SupporterBadge, user_id)
    if badge is None:
        return
    cred = badge.credential
    await session.delete(badge)
    await session.commit()
    # Best-effort relay opt-out; a relay failure must not block local removal.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.request(
                "DELETE", f"{relay_http_url()}/github/founder", params={"cred": cred}
            )
    except Exception:
        pass


def to_dict(badge: SupporterBadge | None) -> dict | None:
    if badge is None:
        return None
    return {
        "github_login": badge.github_login,
        "tier": badge.tier,
        "ordinal": badge.ordinal,
        "label": label_for(badge.tier, badge.ordinal),
    }
