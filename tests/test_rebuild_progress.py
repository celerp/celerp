"""A full rebuild reports its progress in the log, so a long rebuild after an update
reads as work in progress, not a hang. The desktop app reads these lines to show the
progress while it waits for the API."""
from __future__ import annotations

import logging
import re

import pytest
from sqlalchemy import func, select

from test_helpers import lock_every_company, register_admin

_LINE = re.compile(r"Rebuilding records: (\d+) of (\d+) ledger entries")


@pytest.mark.asyncio
async def test_full_rebuild_logs_progress_from_zero_to_every_entry(client, session, caplog):
    from celerp.models.ledger import LedgerEntry
    from celerp.projections.engine import ProjectionEngine
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    for name in ("Alice", "Bob", "Carol"):
        r = await client.post("/crm/contacts", json={"name": name}, headers=h)
        assert r.status_code == 200, r.text
    total = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()

    with caplog.at_level(logging.INFO, logger="celerp.projections.engine"):
        await lock_every_company(session)
        await ProjectionEngine.rebuild(session)

    progress = [tuple(map(int, m.groups())) for m in map(_LINE.search, caplog.messages) if m]
    assert progress[0] == (0, total)
    assert progress[-1] == (total, total)
    assert [done for done, _ in progress] == sorted(done for done, _ in progress)
