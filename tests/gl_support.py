# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Posted balance per account, for tests that assert the money a flow books."""
from __future__ import annotations

from sqlalchemy import select

from celerp.models.projections import Projection


async def gl_totals(session, company_id, *, entry_id_part: str | None = None) -> dict[str, float]:
    """Debit minus credit per account over every posted journal entry of the company,
    or only the entries whose id contains ``entry_id_part``. Accounts at 0 are left out."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).scalars().all()
    out: dict[str, float] = {}
    for r in rows:
        s = r.state or {}
        if s.get("status") != "posted" or (entry_id_part and entry_id_part not in r.entity_id):
            continue
        for e in s.get("entries") or []:
            out[e["account"]] = round(out.get(e["account"], 0) + float(e.get("debit") or 0)
                                      - float(e.get("credit") or 0), 2)
    return {k: v for k, v in out.items() if v}
