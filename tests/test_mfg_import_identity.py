# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Importing production runs again.

A record's key identifies one run, the same way a key does on every other door that creates
a run: importing the same record again skips it, and a record that reuses a key for anything
different (another component, product or quantity, or a run created elsewhere under that
key) is refused with its reason and creates nothing. A file an older release imported is
still skipped when imported again; a different record whose id is already taken is refused,
never silently skipped.
"""
from __future__ import annotations

import pytest

from celerp.events.engine import emit_event
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (fixtures)
from test_mfg_creation_contract import _count, _create

pytestmark = pytest.mark.asyncio


def _record(entity_id: str, key: str, raw: str, made: str, *, qty: float = 2, per: float = 1) -> dict:
    return {"entity_id": entity_id, "event_type": "mfg.order.created", "source": "import", "idempotency_key": key,
            "data": {"description": "Imported", "inputs": [{"item_id": raw, "quantity": per}],
                     "output_item_id": made, "quantity": qty}}


async def _import(client, auth, *records: dict) -> dict:
    r = await client.post("/manufacturing/import/batch", headers=auth["headers"], json={"records": list(records)})
    assert r.status_code == 200, r.text
    return r.json()


async def _parts(client, auth) -> tuple[str, str, str, str]:
    return (await _item(client, auth, 100.0, qty=10), await _item(client, auth, 10.0, qty=10),
            await _item(client, auth, 0.0, qty=0), await _item(client, auth, 0.0, qty=0))


async def test_importing_the_same_record_again_skips_it(client, session, auth):
    raw, _, made, _ = await _parts(client, auth)
    rec = _record("mfg:imp-1", "imp-1", raw, made)
    assert (await _import(client, auth, rec))["created"] == 1

    again = await _import(client, auth, rec)

    assert (again["created"], again["skipped"], again["errors"]) == (0, 1, [])
    assert await _count(session, auth, entity_type="mfg_order") == 1


@pytest.mark.parametrize("change", ["component", "output", "quantity", "component_quantity"])
async def test_a_key_imported_again_with_a_different_run_is_refused(client, session, auth, change):
    raw, other, made, made2 = await _parts(client, auth)
    assert (await _import(client, auth, _record("mfg:imp-1", "imp-1", raw, made)))["created"] == 1
    before = await _state(session, auth, "mfg:imp-1")
    changed = {"component": _record("mfg:imp-1", "imp-1", other, made),
               "output": _record("mfg:imp-1", "imp-1", raw, made2),
               "quantity": _record("mfg:imp-1", "imp-1", raw, made, qty=5),
               "component_quantity": _record("mfg:imp-1", "imp-1", raw, made, per=3)}[change]

    out = await _import(client, auth, changed)

    assert (out["created"], out["skipped"]) == (0, 0)
    assert len(out["errors"]) == 1 and "already used" in out["errors"][0], out
    assert await _count(session, auth, entity_type="mfg_order") == 1
    assert await _state(session, auth, "mfg:imp-1") == before


async def test_one_key_twice_in_a_file_imports_once(client, session, auth):
    raw, other, made, _ = await _parts(client, auth)
    same = await _import(client, auth, _record("mfg:a", "k-a", raw, made), _record("mfg:a", "k-a", raw, made))
    differs = await _import(client, auth, _record("mfg:b", "k-b", raw, made), _record("mfg:b", "k-b", other, made))
    other_id = await _import(client, auth, _record("mfg:c", "k-c", raw, made), _record("mfg:d", "k-c", raw, made))

    assert (same["created"], same["skipped"], same["errors"]) == (1, 1, [])
    assert differs["created"] == 1 and len(differs["errors"]) == 1 and "already used" in differs["errors"][0]
    assert other_id["created"] == 1 and len(other_id["errors"]) == 1 and "already used" in other_id["errors"][0]
    assert (await _state(session, auth, "mfg:b"))["inputs"][0]["item_id"] == raw
    assert await _count(session, auth, entity_type="mfg_order") == 3


async def test_a_key_used_by_another_door_is_not_imported_over(client, session, auth):
    raw, _, made, _ = await _parts(client, auth)
    assert (await _create(client, auth, [(raw, 1)], made, qty=2, key="shared")).status_code == 200

    out = await _import(client, auth, _record("mfg:imp-x", "shared", raw, made))

    assert out["created"] == 0 and len(out["errors"]) == 1 and "already used" in out["errors"][0], out
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_file_an_older_release_imported_is_skipped_and_a_different_record_with_its_id_refused(
        client, session, auth):
    """The older release stored an imported record as it came, under its own key."""
    raw, other, made, _ = await _parts(client, auth)
    rec = _record("mfg:old", "old-1", raw, made)
    await emit_event(session, company_id=auth["company_id"], entity_id=rec["entity_id"], entity_type="mfg_order",
                     event_type="mfg.order.created", data=rec["data"], actor_id=auth["user_id"], location_id=None,
                     source="import", idempotency_key=rec["idempotency_key"], metadata_={})
    await session.commit()
    before = await _state(session, auth, "mfg:old")

    again = await _import(client, auth, rec)
    taken = await _import(client, auth, _record("mfg:old", "old-2", other, made))

    assert (again["created"], again["skipped"], again["errors"]) == (0, 1, [])
    assert taken["created"] == 0 and len(taken["errors"]) == 1 and "already exists" in taken["errors"][0], taken
    assert await _state(session, auth, "mfg:old") == before
    assert await _count(session, auth, entity_type="mfg_order") == 1
