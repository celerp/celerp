# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Writing off stock that carries no cost disposes of it and posts no journal entry,
the way every other automatic entry skips an amount of zero."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from test_cost_restatement import _state
from test_posting_roles_kept_stock import _available, _ok, _write_off

pytestmark = pytest.mark.asyncio


async def _writeoff_entries(session, auth, wo: str) -> list[dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.like(f"je:auto:{wo}:writeoff:%")))).scalars().all()
    return [row.state for row in rows]


async def test_a_zero_cost_write_off_disposes_and_posts_nothing(session, client, auth):
    lot = await _available(client, auth, 0.0)
    wo = await _write_off(client, auth, lot, 1)
    assert (await _state(session, auth, lot))["status"] == "disposed"
    assert await _writeoff_entries(session, auth, wo) == []

    await _ok(client, auth, "POST", f"/lists/{wo}/undo-write-off")
    assert (await _state(session, auth, lot))["status"] == "available"
    assert await _writeoff_entries(session, auth, wo) == []


async def test_a_write_off_with_cost_posts_its_value(session, client, auth):
    lot = await _available(client, auth, 40.0)
    account = (await _state(session, auth, lot))["inventory_account_code"]
    wo = await _write_off(client, auth, lot, 1)
    [je] = await _writeoff_entries(session, auth, wo)
    lines = {(e["account"], float(e["debit"] or 0), float(e["credit"] or 0)) for e in je["entries"]}
    assert lines == {("6950", 40.0, 0.0), (account, 0.0, 40.0)}
