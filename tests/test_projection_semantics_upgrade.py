# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Upgrading from an older release whose projection handlers computed state differently.

The database starts as the older release left it (``pre366``): its ledger, the projection
rows its handlers wrote, and a marker saying it last started on an older RELEASE. The normal
start of this release rebuilds the projections from the ledger, because the handlers'
semantics changed, and only then lets the modules settle data on them: the run the older
release issued to is carrying its work in progress at the end of that same start.

A start whose projection semantics are already current rebuilds nothing.

The ledger can hold events no release emits anymore (BOM history from before recipes); they
replay as the release that emitted them applied them, so they never hold the upgrade back. A
start that cannot bring the projections current (an event of a module that is not
installed, or a rebuild that fails) settles nothing on them: it leaves the markers as they
were, tells every company in the notification bell, and the next start retries.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import pre366
from celerp.models.projections import Projection
from stock_books import assert_wip_carried

pytestmark = pytest.mark.asyncio


async def _meta(session, key: str) -> str | None:
    from celerp.migrations._data_reconcile import get_meta

    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, key))


async def _state(session, company_id, entity_id: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    return row.state


async def _held_back(session, company_id) -> list:
    """The unread notices telling the company that a start held its updates back."""
    from sqlalchemy import select

    from celerp.main import _HELD_BACK_TITLE
    from celerp.models.notification import Notification

    return list((await session.execute(select(Notification).where(
        Notification.company_id == company_id, Notification.priority == "high",
        Notification.title == _HELD_BACK_TITLE, Notification.read == False))).scalars())  # noqa: E712


def _fired(monkeypatch) -> list[str]:
    """The lifecycle slots fired from now on (each still runs)."""
    from celerp.modules import slots

    fired, real = [], slots.fire_lifecycle

    async def spy(slot, **kwargs):
        fired.append(slot)
        return await real(slot, **kwargs)

    monkeypatch.setattr(slots, "fire_lifecycle", spy)
    return fired


def _settled(state: dict) -> bool:
    return state.get("wip_issued") is not None


async def _run(session, old, name: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": old["company_id"], "entity_id": old["runs"][name]})
    return row.state


def _issued(state: dict, item: str) -> float:
    return sum(float(i.get("issued_qty") or 0) for i in state["inputs"] if i["item_id"] == item)


async def test_release_upgrade_rebuilds_projections_and_settles_runs_in_the_same_start(client, session):
    from celerp import __version__
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS, PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session)
    await pre366.last_started_on_older_release(session)
    # The older release recorded 2 of A against each of the two A lines of this run (4 in all)
    # though 3 were issued.
    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 4

    await pre366.start()

    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 3
    settled = await _run(session, old, "settle")
    assert Decimal(str(settled["wip_issued"])) == Decimal("13"), settled  # 2 x 5 (A) + 1 x 3 (B)
    assert not settled.get("wip_untracked") and not settled.get("wip_unresolved"), settled
    wip = await assert_wip_carried(session, old["company_id"])
    assert sum(wip.values()) > 0, wip
    assert await _meta(session, PROJECTION_SEMANTICS_KEY) == str(PROJECTION_SEMANTICS)
    assert await _meta(session, "projection_version") == __version__


async def test_a_start_with_current_projection_semantics_rebuilds_nothing(client, session):
    from celerp.migrations._data_reconcile import set_meta
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS, PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session)
    await pre366.last_started_on_older_release(session)
    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_SEMANTICS_KEY, str(PROJECTION_SEMANTICS)))
    await session.commit()

    await pre366.start()

    # The stored projection is untouched: no rebuild replayed the ledger over it.
    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 4


async def test_bom_history_replays_as_written_and_the_runs_settle_in_the_same_start(client, session):
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS, PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session, "mixed")
    await pre366.last_started_on_older_release(session)
    boms = {k: await _state(session, old["company_id"], old["items"][k]) for k in ("BOM_KEPT", "BOM_DROPPED")}
    assert boms["BOM_DROPPED"]["deleted"] is True and boms["BOM_KEPT"]["entity_type"] == "bom", boms

    await pre366.start()

    assert await _meta(session, PROJECTION_SEMANTICS_KEY) == str(PROJECTION_SEMANTICS)
    for k, before in boms.items():  # rebuilt exactly as the release that wrote them left them
        assert await _state(session, old["company_id"], old["items"][k]) == before, k
    # Settled in this same start: the tangled run received before value was tracked, and the
    # value it holds in the same inventory account leaves the books disagreeing on the other
    # until it is unwound (test_pre366_permutations).
    assert (await _run(session, old, "tangle"))["wip_unresolved"] == "received before tracking"
    assert (await _run(session, old, "recipe"))["wip_unresolved"] == "books disagree"
    assert not await _held_back(session, old["company_id"])


async def test_an_event_of_a_missing_module_holds_the_upgrade_and_its_settlement_back(client, session, monkeypatch):
    from celerp.models.ledger import LedgerEntry
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session)
    session.add(LedgerEntry(company_id=old["company_id"], entity_id="gadget:1", entity_type="gadget",
                            event_type="gadget.assembled", data={}, actor_id=None, location_id=None,
                            source="api", idempotency_key="gadget-1", metadata_={}))
    await session.commit()
    await pre366.last_started_on_older_release(session)
    fired = _fired(monkeypatch)

    await pre366.start()

    assert "on_modules_ready" not in fired
    assert await _meta(session, PROJECTION_SEMANTICS_KEY) is None
    assert await _meta(session, "projection_version") == pre366.OLDER_RELEASE
    assert not _settled(await _run(session, old, "settle"))
    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 4  # not rebuilt
    assert len(await _held_back(session, old["company_id"])) == 1


async def test_a_failed_rebuild_settles_nothing_and_the_next_start_rebuilds_then_settles(
        client, session, monkeypatch):
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS, PROJECTION_SEMANTICS_KEY

    old = await pre366.load(session)
    await pre366.last_started_on_older_release(session)

    async def broken(session, company_id=None):
        raise RuntimeError("disk full")

    fired = _fired(monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(ProjectionEngine, "rebuild", staticmethod(broken))
        await pre366.start()
        await pre366.start()  # a second failed start does not repeat the notice

    assert "on_modules_ready" not in fired
    assert await _meta(session, PROJECTION_SEMANTICS_KEY) is None
    assert await _meta(session, "projection_version") == pre366.OLDER_RELEASE
    assert not _settled(await _run(session, old, "settle"))
    assert len(await _held_back(session, old["company_id"])) == 1

    await pre366.start()

    assert fired == ["on_modules_ready"]
    assert await _meta(session, PROJECTION_SEMANTICS_KEY) == str(PROJECTION_SEMANTICS)
    assert _issued(await _run(session, old, "dup_issued"), old["items"]["A"]) == 3
    assert Decimal(str((await _run(session, old, "settle"))["wip_issued"])) == Decimal("13")
