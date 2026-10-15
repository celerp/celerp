# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Older stock placed against an account that served several roles before posting
accounts were recorded on each line.

A line posted before then carries no roles of its own, so what it meant is read from
the roles its account has served. When that account served an inventory role and
another one too, the line may or may not have been stock. That never proves the stock
was never booked: placing books no opening stock against retained earnings for it,
and leaves the difference open for the accountant instead."""
from __future__ import annotations

from sqlalchemy import select

from celerp.accounting_roles import SCOPES_KEY
from celerp.models.projections import Projection
from test_older_stock_placement import _accounts, _imported, _marked, _off_books, _placements, _view
from test_posting_roles_older_stock import _net, _older_release, _settings
from test_posting_roles_rollout import _startup

import pytest

pytestmark = pytest.mark.asyncio


async def _untagged(session, auth) -> None:
    """Every posted line as an older release left it: no roles recorded on it."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    for row in rows:
        row.state = {**row.state, "entries": [{k: v for k, v in e.items() if k != "account_roles"}
                                              for e in row.state.get("entries") or []]}
    await session.commit()


async def _older_books(session, client, auth, scopes: dict[str, list[str]]) -> list[str]:
    """One older lot worth 30 whose opening entry sits on 1130-OB with no roles on its
    lines, the accounts having served ``scopes`` before."""
    lots = await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    await _untagged(session, auth)
    await _settings(session, auth, **{SCOPES_KEY: scopes})
    await _marked(session, auth)
    return lots


async def test_untagged_stock_on_an_account_that_served_several_roles_is_not_booked_again(session, client, auth):
    lots = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"], "tax_input": ["1130-OB"]})
    before = await _net(session, auth, "1130-OB", "1130-P", "3200")
    await _startup(session)
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before == (30.0, 0.0, -30.0)
    assert await _placements(session, auth) == []
    assert await _accounts(session, auth, lots) == ["1130-OB"]

    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "review"
    assert view["reconciliation"]["open"] == [
        {"placement": view["placements"][0]["pid"], "account": "1130-OB", "kind": "unresolved", "amount": -30.0}]
    (placement,) = view["placements"]
    assert (placement["classification"], placement["recognized"], placement["unresolved"]) == ("unresolved", 0.0, -30.0)
    assert await _off_books(session, auth) == [{"kind": "unresolved_difference", "account": "1130-OB", "amount": -30.0}]

    # Starting again books nothing more and keeps the difference open.
    await _startup(session)
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before
    assert (await _view(client, auth))["reconciliation"]["open"] == view["reconciliation"]["open"]


async def test_untagged_stock_on_an_account_only_inventory_served_is_still_counted(session, client, auth):
    lots = await _older_books(session, client, auth, {"inventory_opening": ["1130-OB"]})
    before = await _net(session, auth, "1130-OB", "1130-P", "3200")
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-OB", "1130-P", "3200") == before == (30.0, 0.0, -30.0)
    view = await _view(client, auth)
    assert view["reconciliation"]["state"] == "reconciled"
    assert view["placements"][0]["classification"] == "exact"
    assert await _off_books(session, auth) == []
