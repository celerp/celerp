# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Upgrading from an older release whose projection handlers computed state differently.

The database starts as the older release left it (``pre366``): its ledger, the projection
rows its handlers wrote, and a marker saying it last started on an older RELEASE. The normal
start of this release rebuilds the projections from the ledger, because the handlers'
semantics changed, and only then lets the modules settle data on them: the run the older
release issued to is carrying its work in progress at the end of that same start.

A start whose projection semantics are already current rebuilds nothing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import pre366
from celerp.models.projections import Projection
from celerp.modules import slots
from stock_books import assert_wip_carried

pytestmark = pytest.mark.asyncio


@pytest.fixture
def startup_hooks():
    saved = {s: slots.get(s) for s in ("on_modules_ready", "inventory_in_production")}
    for s in saved:
        slots._slots[s] = []
    pre366.register_startup_hooks(slots)
    yield
    for s, v in saved.items():
        slots._slots[s] = v


async def _meta(session, key: str) -> str | None:
    from celerp.migrations._data_reconcile import get_meta

    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, key))


async def _run(session, old, name: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": old["company_id"], "entity_id": old["runs"][name]})
    return row.state


def _issued(state: dict, item: str) -> float:
    return sum(float(i.get("issued_qty") or 0) for i in state["inputs"] if i["item_id"] == item)


async def test_release_upgrade_rebuilds_projections_and_settles_runs_in_the_same_start(client, session, startup_hooks):
    from celerp import __version__
    from celerp.main import _bring_data_current
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS, PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session)
    await pre366.last_started_on_older_release(session)
    # The older release recorded 2 of A against each of the two A lines of this run (4 in all)
    # though 3 were issued.
    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 4

    await _bring_data_current(modules_ready=True)

    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 3
    settled = await _run(session, old, "settle")
    assert Decimal(str(settled["wip_issued"])) == Decimal("13"), settled  # 2 x 5 (A) + 1 x 3 (B)
    assert not settled.get("wip_untracked") and not settled.get("wip_unresolved"), settled
    wip = await assert_wip_carried(session, old["company_id"])
    assert sum(wip.values()) > 0, wip
    assert await _meta(session, PROJECTION_SEMANTICS_KEY) == str(PROJECTION_SEMANTICS)
    assert await _meta(session, "projection_version") == __version__


async def test_a_start_with_current_projection_semantics_rebuilds_nothing(client, session, startup_hooks):
    from celerp.main import _bring_data_current
    from celerp.migrations._data_reconcile import set_meta
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS, PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session)
    await pre366.last_started_on_older_release(session)
    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_SEMANTICS_KEY, str(PROJECTION_SEMANTICS)))
    await session.commit()

    await _bring_data_current(modules_ready=True)

    # The stored projection is untouched: no rebuild replayed the ledger over it.
    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 4
