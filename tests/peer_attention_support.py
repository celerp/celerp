# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The oracle of a connected company's attention reasons: what a writer stored, read back
from its row. Every writer's end-to-end scenario asserts its row through here, so a reason
stored as English alone fails the scenario that drives it (peer_attention.stored, which
every writer passes its reason through, refuses one)."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select


def stored_key(reason: str) -> str:
    """The catalog key of a stored attention reason; fails on anything but a keyed refusal
    (a plain error a writer met is stored under the generic key carrying its text)."""
    try:
        refusal = json.loads(reason)
    except ValueError:
        raise AssertionError(f"an attention reason stored as English: {reason!r}") from None
    assert isinstance(refusal, dict) and refusal.get("message") and refusal.get("message_key"), refusal
    return refusal["message_key"]


async def stored_keys(session, company_id) -> list[str]:
    """The keys of every attention reason the company holds, each read back from its row."""
    from celerp.models.company_peer import CompanyPeerAttention

    reasons = (await session.scalars(select(CompanyPeerAttention.reason).where(
        CompanyPeerAttention.company_id == uuid.UUID(str(company_id))))).all()
    return [stored_key(reason) for reason in reasons]


async def attention_keys(engine, company_id) -> list[str]:
    """stored_keys read through a session of its own on ``engine``."""
    from migration_support import maker

    async with maker(engine)() as session:
        return await stored_keys(session, company_id)
