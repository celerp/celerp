# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the develop→release lifespan guard (projection rebuild + catalog).

Uses the transactional `session` fixture (rolled back per test). Projection
handlers are registered by the root conftest's autouse slot fixture, so
`ProjectionEngine.rebuild` runs the real item handler here.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, text

from celerp import __version__
from celerp.events.engine import emit_event
from celerp.migrations._data_reconcile import (
    PROJECTION_VERSION_KEY,
    _META_TABLE,
    _ensure_meta_table,
    get_meta,
    set_meta,
)
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.dev_release_guard import (
    PROJECTION_SEMANTICS,
    PROJECTION_SEMANTICS_KEY,
    run_upgrade_guard,
)


@pytest.fixture(autouse=True)
async def _isolate_version_marker(session):
    """The projection markers live in the global `instance_meta` table,
    which is outside the per-test transaction's company scope: a committed write
    from another test (e.g. the app-lifespan guard) can leak in and make the
    "marker is unset" assertions flaky. Clear the key inside this test's own
    transaction so every test starts from a known-unset marker; the delete rolls
    back at teardown like the rest of the test's writes."""
    conn = await session.connection()

    def _clear(c):
        _ensure_meta_table(c)
        c.execute(text(f"DELETE FROM {_META_TABLE} WHERE key IN (:k, :s)"),
                  {"k": PROJECTION_VERSION_KEY, "s": PROJECTION_SEMANTICS_KEY})

    await conn.run_sync(_clear)
    yield


async def _seed_item(session) -> uuid.UUID:
    cid = uuid.uuid4()
    session.add(Company(id=cid, name="GuardCo", slug=f"guardco-{cid.hex[:8]}"))
    await session.flush()
    await emit_event(
        session, company_id=cid, entity_id="item:1", entity_type="item",
        event_type="item.created", data={"sku": "S", "name": "A", "quantity": 1},
        actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    return cid


async def _marker(session) -> str | None:
    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, PROJECTION_VERSION_KEY))


async def _set_marker(session, value: str) -> None:
    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_VERSION_KEY, value))


async def _semantics(session) -> str | None:
    conn = await session.connection()
    return await conn.run_sync(lambda c: get_meta(c, PROJECTION_SEMANTICS_KEY))


async def _set_semantics(session, value: str) -> None:
    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_SEMANTICS_KEY, value))


async def _projection_count(session) -> int:
    return len((await session.execute(select_projections())).all())


def select_projections():
    from sqlalchemy import select
    return select(Projection)


@pytest.mark.asyncio
async def test_guard_rebuilds_on_version_change(session):
    """No marker (develop origin) → guard rebuilds projections and stamps the
    version marker."""
    await _seed_item(session)
    # Simulate projections that don't match the release: wipe them.
    await session.execute(delete(Projection))
    assert await _projection_count(session) == 0
    assert await _marker(session) is None

    result = await run_upgrade_guard(session)

    assert result == {"changed": True, "rebuilt": True, "current": True}
    assert await _projection_count(session) == 1     # rebuilt from the ledger
    assert await _marker(session) == __version__


@pytest.mark.asyncio
async def test_guard_gated_when_version_matches(session):
    """Once stamped for this version, a second run is skipped — proven by wiping
    projections and confirming the guard does NOT rebuild them."""
    await _seed_item(session)
    await run_upgrade_guard(session)            # stamps marker == __version__
    assert await _marker(session) == __version__

    await session.execute(delete(Projection))   # corrupt: drop read-models
    result = await run_upgrade_guard(session)    # same version → must skip

    assert result == {"changed": False, "rebuilt": False, "current": True}
    assert await _projection_count(session) == 0  # NOT rebuilt (gate held)


@pytest.mark.asyncio
async def test_guard_skips_rebuild_for_release_origin(session):
    """A DB last booted by a RELEASE build (marker has no .dev) under the same
    projection semantics is assumed projection-correct: a release→release upgrade
    skips the rebuild entirely."""
    await _seed_item(session)
    await _set_marker(session, "1.0.0")          # previous boot was a release build
    await _set_semantics(session, str(PROJECTION_SEMANTICS))
    await session.execute(delete(Projection))    # corrupt: drop read-models

    result = await run_upgrade_guard(session)

    assert result == {"changed": True, "rebuilt": False, "current": True}
    assert await _projection_count(session) == 0  # NOT rebuilt — release origin
    assert await _marker(session) == __version__  # but the boot version is recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("semantics", [None, str(PROJECTION_SEMANTICS - 1)], ids=["unrecorded", "older"])
async def test_guard_rebuilds_for_release_origin_with_other_semantics(session, semantics):
    """A release→release upgrade across a change in projection semantics rebuilds,
    and records both the version and the semantics it rebuilt under."""
    await _seed_item(session)
    await _set_marker(session, "1.0.0")
    if semantics is not None:
        await _set_semantics(session, semantics)
    await session.execute(delete(Projection))

    result = await run_upgrade_guard(session)

    assert result == {"changed": True, "rebuilt": True, "current": True}
    assert await _projection_count(session) == 1
    assert await _marker(session) == __version__
    assert await _semantics(session) == str(PROJECTION_SEMANTICS)


@pytest.mark.asyncio
@pytest.mark.parametrize("dev_marker", ["1.0.0.dev7", "0.0.0+dev"], ids=["pep440-dev", "local-dev-fallback"])
async def test_guard_rebuilds_for_dev_origin(session, dev_marker):
    """A DB last booted by a DEVELOP build gets a rebuild — the develop→release
    transition this feature exists for. Both dev-version forms count (matching
    the UI's detection): a `.devN` segment and the `+dev` local fallback."""
    await _seed_item(session)
    await _set_marker(session, dev_marker)        # previous boot was a develop build
    await session.execute(delete(Projection))

    result = await run_upgrade_guard(session)

    assert result == {"changed": True, "rebuilt": True, "current": True}
    assert await _projection_count(session) == 1  # rebuilt from the ledger
    assert await _marker(session) == __version__


@pytest.mark.asyncio
async def test_guard_skips_rebuild_on_unknown_event_type(session):
    """An unknown ledger event type (downgrade / missing module) makes the guard
    refuse to rebuild, leaving existing projections and the marker untouched."""
    cid = await _seed_item(session)
    # A ledger event the catalog doesn't know about.
    await session.execute(
        text(
            "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, "
            "data, source, idempotency_key, ts) VALUES "
            "(:cid, 'x:1', 'mystery', 'zzz.unknown.event', '{}'::json, 'test', :idem, now())"
        ),
        {"cid": cid, "idem": f"idem-{uuid.uuid4().hex[:8]}"},
    )
    before = await _projection_count(session)

    result = await run_upgrade_guard(session)

    assert result["changed"] is True and result["rebuilt"] is False and result["current"] is False
    assert "zzz.unknown.event" in result["unknown_event_types"]
    assert await _projection_count(session) == before   # untouched
    assert await _marker(session) is None               # not stamped → retries later
    assert await _semantics(session) is None


@pytest.mark.asyncio
async def test_unreplayable_empty_for_known_events(session):
    """All kernel/module event types emitted normally are replayable."""
    from celerp.projections.engine import ProjectionEngine

    await _seed_item(session)
    assert await ProjectionEngine.unreplayable(session) == set()


# --- replay capability: a module's events replay only through its own handler ----------

_RUN_DATA = {
    "product_sku": "FG", "quantity": 1,
    "inputs": [{"item_id": "item:c", "quantity": 2}],
    "outputs": [{"item_id": "item:fg", "quantity": 1}],
}


async def _seed_run(session) -> uuid.UUID:
    cid = uuid.uuid4()
    session.add(Company(id=cid, name="RunCo", slug=f"runco-{cid.hex[:8]}"))
    await session.flush()
    for event_type, data in (("mfg.order.created", _RUN_DATA), ("mfg.order.started", {})):
        await emit_event(
            session, company_id=cid, entity_id="mfg:1", entity_type="mfg_order",
            event_type=event_type, data=data, actor_id=None, location_id=None,
            source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    return cid


async def _rows(session, cid) -> list[tuple]:
    from sqlalchemy import select
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == cid).order_by(Projection.entity_id)
    )).scalars().all()
    return [(r.entity_id, r.entity_type, r.version, r.state) for r in rows]


@pytest.fixture
def without_manufacturing(monkeypatch):
    """The manufacturing module is not enabled: its projection handler is not registered,
    while its event schemas stay in the catalog."""
    from celerp.modules import slots

    registered = slots.get("projection_handler")

    def disable():
        kept = [c for c in registered if c.get("prefix") != "mfg."]
        monkeypatch.setitem(slots._slots, "projection_handler", kept)

    def enable():
        monkeypatch.setitem(slots._slots, "projection_handler", registered)

    disable.enable = enable
    return disable


@pytest.mark.asyncio
async def test_an_event_of_a_module_not_enabled_holds_the_upgrade_back(session, without_manufacturing):
    """A manufacturing run in the ledger while manufacturing is not enabled: the events have
    a schema but nothing here can apply them as manufacturing does, so the upgrade is held
    back with the rows and markers as they were. Once manufacturing is enabled again, the
    next start rebuilds the run exactly as manufacturing applies it, not as a plain merge."""
    from celerp_manufacturing.projection_handler import apply_manufacturing_event

    cid = await _seed_run(session)
    await _set_marker(session, "0.0.1")
    await _set_semantics(session, "0")
    rows = await _rows(session, cid)

    without_manufacturing()
    result = await run_upgrade_guard(session)

    assert result["current"] is False and result["rebuilt"] is False
    assert {"mfg.order.created", "mfg.order.started"} <= set(result["unknown_event_types"])
    assert await _rows(session, cid) == rows
    assert await _marker(session) == "0.0.1" and await _semantics(session) == "0"

    without_manufacturing.enable()
    await session.execute(text("UPDATE projections SET state = '{}'::json WHERE company_id = :c"), {"c": cid})

    result = await run_upgrade_guard(session)

    assert result["current"] is True and result["rebuilt"] is True
    expected = apply_manufacturing_event(apply_manufacturing_event({}, "mfg.order.created", _RUN_DATA),
                                         "mfg.order.started", {})
    [(_, _, _, state)] = await _rows(session, cid)
    assert state == expected
    assert state != {**_RUN_DATA}
    assert state["status"] == "in_progress" and state["inputs"][0]["issued_qty"] == 0.0


def test_a_module_event_without_its_handler_is_neither_replayed_nor_written(without_manufacturing):
    """A live write and a replay answer "can this be applied" from one place: an event whose
    module handler is not registered is not replayable, and applying it is refused rather
    than kept as its data."""
    from celerp.projections.engine import ProjectionEngine

    without_manufacturing()
    assert ProjectionEngine.replayable("mfg.order.created") is False
    with pytest.raises(ValueError, match="mfg.order.created"):
        ProjectionEngine._apply({}, "mfg.order.created", _RUN_DATA)


def test_a_schema_alone_never_makes_an_event_replayable(monkeypatch):
    """With no module handler registered, only retired events, the kernel's own events and
    the events no module owns (their projection is the plain merge of their data) can be
    replayed; every module's events, each with a schema in the catalog, cannot."""
    from celerp.events.schemas import EVENT_SCHEMA_MAP
    from celerp.modules import slots
    from celerp.projections.engine import ProjectionEngine

    monkeypatch.setitem(slots._slots, "projection_handler", [])
    for event_type in ("sub.created", "scan.barcode", "payment_batch.recorded", "bom.created",
                       "crm.deal.created", "sys.user.created", "sys.company.created"):
        assert ProjectionEngine.replayable(event_type), event_type
    for event_type in ("mfg.order.created", "item.created", "doc.created",
                       "crm.contact.created", "acc.journal_entry.created"):
        assert event_type in EVENT_SCHEMA_MAP
        assert not ProjectionEngine.replayable(event_type), event_type


def test_every_event_the_catalog_lists_is_replayable_with_every_module_loaded():
    """Every event type that may be written is applied by something once the bundled
    modules are loaded: its module's handler, the kernel, or the plain merge of an event no
    module owns. An event type nothing applies would refuse its own writes and hold back
    every start whose ledger holds one."""
    from celerp.events.schemas import EVENT_SCHEMA_MAP
    from celerp.projections.engine import ProjectionEngine

    assert sorted(t for t in EVENT_SCHEMA_MAP if not ProjectionEngine.replayable(t)) == []


@pytest.mark.asyncio
async def test_a_deal_never_holds_the_upgrade_back(session):
    """Deals belong to no module's projection handler; a ledger holding them is brought
    current like any other, never held back as if a module were missing."""
    cid = await _seed_item(session)
    for event_type, data in (("crm.deal.created", {"name": "Deal", "stage": "lead"}),
                             ("crm.deal.won", {})):
        await emit_event(
            session, company_id=cid, entity_id="deal:1", entity_type="deal",
            event_type=event_type, data=data, actor_id=None, location_id=None,
            source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    await _set_marker(session, "0.0.1")
    await _set_semantics(session, "0")

    result = await run_upgrade_guard(session)

    assert result["current"] is True and result["rebuilt"] is True, result


@pytest.mark.asyncio
async def test_a_manual_rebuild_refuses_events_it_cannot_replay(session, without_manufacturing):
    """Rebuilding from the ledger by hand (Doctor, the ledger rebuild) refuses before it
    deletes anything when a module the ledger needs is not enabled."""
    from fastapi import HTTPException

    from celerp.projections.engine import ProjectionEngine

    cid = await _seed_run(session)
    rows = await _rows(session, cid)
    without_manufacturing()
    with pytest.raises(HTTPException) as err:
        await ProjectionEngine.rebuild(session, company_id=cid)
    assert err.value.status_code == 409 and "Manufacturing" in err.value.detail
    assert await _rows(session, cid) == rows
