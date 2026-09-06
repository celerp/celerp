# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Read-only global-search provider over journal entries.

The provider narrows through the same filter and payload builder the journal
route uses, so a global-search hit and the journal itself can never disagree
about which entries match a term. It reads the whole book (no date range) and
truncates the already-ordered entries to the search limit.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from celerp_accounting.routes import _journal_filter, _journal_payload


async def global_search(
    session: AsyncSession,
    company_id,
    role: str,
    q: str,
    limit: int,
) -> dict:
    """Global-search provider: journal entries matching q, capped to limit.

    result_key is "entries" (the journal's own shape), not "items".
    """
    filt = _journal_filter(q)
    payload, _refs, _base = await _journal_payload(session, company_id, None, None, filt)
    return {"entries": payload["entries"][:limit]}
