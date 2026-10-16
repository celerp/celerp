# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A changed cost follows a traceable write-off and its complete undo."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from stock_books import assert_books_carry_stock
from test_cost_restatement import _set_cost, _state
from test_posting_roles_kept_stock import _available, _ok, _write_off

pytestmark = pytest.mark.asyncio


async def _journals(session, auth, prefix):
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_type == "journal_entry",
        Projection.entity_id.like(prefix + "%"),
    ).order_by(Projection.entity_id))).scalars().all()
    return [SimpleNamespace(entity_id=row.entity_id, state=dict(row.state)) for row in rows]


def _net(state, account):
    if state.get("status") != "posted":
        return 0.0
    return round(sum(float(row.get("debit") or 0) - float(row.get("credit") or 0)
                     for row in state.get("entries") or [] if row["account"] == account), 2)


@pytest.mark.parametrize("original,corrected", [(40.0, 55.0), (40.0, 25.0), (0.0, 5.0)])
async def test_writeoff_correction_posts_difference_once_and_undo_restores_books(
        client, session, auth, original, corrected):
    lot = await _available(client, auth, original)
    origin = (await _state(session, auth, lot))["inventory_account_code"]
    writeoff = await _write_off(client, auth, lot, 1)
    key = f"writeoff-cost:{uuid.uuid4()}"
    response = await _set_cost(client, auth, lot, corrected, key=key)
    assert response.status_code == 200, response.text
    assert (await _state(session, auth, lot))["status"] == "disposed"
    assert (await _state(session, auth, lot))["cost_total"] == corrected
    original_entries = await _journals(session, auth, f"je:auto:{writeoff}:writeoff:")
    adjustments = await _journals(session, auth, f"je:auto:{writeoff}:cost:")
    assert len(adjustments) == 1
    assert round(sum(_net(r.state, "6950") for r in (*original_entries, *adjustments)), 2) == corrected
    assert round(sum(_net(r.state, origin) for r in (*original_entries, *adjustments)), 2) == -corrected
    before = [r.entity_id for r in await _journals(session, auth, "je:auto:")]
    retry = await _set_cost(client, auth, lot, corrected, key=key)
    assert retry.status_code == 200, retry.text
    assert [r.entity_id for r in await _journals(session, auth, "je:auto:")] == before
    await assert_books_carry_stock(session, auth["company_id"])

    await _ok(client, auth, "POST", f"/lists/{writeoff}/undo-write-off")
    assert (await _state(session, auth, lot))["status"] == "available"
    assert (await _state(session, auth, lot))["cost_total"] == corrected
    assert all(r.state["status"] == "void" for r in
               await _journals(session, auth, f"je:auto:{writeoff}:cost:"))
    await assert_books_carry_stock(session, auth["company_id"])


async def test_untraceable_disposal_refuses_cost_correction(client, session, auth):
    lot = await _available(client, auth, 40.0)
    # A live write-off can be corrected, but once it is undone the original
    # cost must not be charged to its old destination on a later edit.
    writeoff = await _write_off(client, auth, lot, 1)
    await _ok(client, auth, "POST", f"/lists/{writeoff}/undo-write-off")
    edit = await _set_cost(client, auth, lot, 50.0)
    assert edit.status_code == 200, edit.text
    assert not await _journals(session, auth, f"je:auto:{writeoff}:cost:")


async def test_writeoff_refusal_explains_next_step(client, session, auth):
    """A lot that left stock without a write-off is refused in plain words with what to do instead."""
    from test_consignment_in_sale import _consign

    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0)
    back = await client.post(f"/docs/{con}/return-items", headers=auth["headers"],
                             json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert back.status_code == 200, back.text
    assert (await _state(session, auth, lot))["status"] == "disposed"
    edit = await _set_cost(client, auth, lot, 50.0)
    assert edit.status_code == 409, edit.text
    detail = edit.json()["detail"]
    assert "write-off" in detail and "journal entry" in detail, detail
    assert "verifiably" not in detail and "restated" not in detail, detail
