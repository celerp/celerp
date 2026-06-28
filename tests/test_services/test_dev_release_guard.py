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
from celerp.services.dev_release_guard import run_upgrade_guard, unknown_event_types


@pytest.fixture(autouse=True)
async def _isolate_version_marker(session):
    """The projection-version marker lives in the global `instance_meta` table,
    which is outside the per-test transaction's company scope: a committed write
    from another test (e.g. the app-lifespan guard) can leak in and make the
    "marker is unset" assertions flaky. Clear the key inside this test's own
    transaction so every test starts from a known-unset marker; the delete rolls
    back at teardown like the rest of the test's writes."""
    conn = await session.connection()

    def _clear(c):
        _ensure_meta_table(c)
        c.execute(text(f"DELETE FROM {_META_TABLE} WHERE key = :k"), {"k": PROJECTION_VERSION_KEY})

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

    assert result == {"changed": True, "rebuilt": True}
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

    assert result == {"changed": False, "rebuilt": False}
    assert await _projection_count(session) == 0  # NOT rebuilt (gate held)


@pytest.mark.asyncio
async def test_guard_skips_rebuild_for_release_origin(session):
    """A DB last booted by a RELEASE build (marker has no .dev) is assumed
    projection-correct: a release→release upgrade skips the rebuild entirely."""
    await _seed_item(session)
    await _set_marker(session, "1.0.0")          # previous boot was a release build
    await session.execute(delete(Projection))    # corrupt: drop read-models

    result = await run_upgrade_guard(session)

    assert result == {"changed": True, "rebuilt": False}
    assert await _projection_count(session) == 0  # NOT rebuilt — release origin
    assert await _marker(session) == __version__  # but the boot version is recorded


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

    assert result == {"changed": True, "rebuilt": True}
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

    assert result["changed"] is True and result["rebuilt"] is False
    assert "zzz.unknown.event" in result["unknown_event_types"]
    assert await _projection_count(session) == before   # untouched
    assert await _marker(session) is None               # not stamped → retries later


@pytest.mark.asyncio
async def test_unknown_event_types_empty_for_known_events(session):
    """All kernel/module event types emitted normally are in the catalog."""
    await _seed_item(session)
    assert await unknown_event_types(session) == set()
