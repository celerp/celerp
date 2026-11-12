# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fund movements at the journal boundary: which entries move money between funds, and who may post them.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank, 4100 sales
revenue, 6200 rent, 3200 retained earnings), funds turned on, which adds the restricted net
assets account. An entry is movement-shaped when (a) a line is on restricted net assets,
(b) its type is a fund movement type or a year close, (c) the net assets of two or more funds
change, or (d) value leaves a restricted fund and arrives in another, whatever the accounts.
Such an entry posts only inside the movement grant post_fund_movement enters, for the
entity it minted, from a user (never the system actor) whose role allows it.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from celerp.services.journal_accounts import RestrictedFundMovement, fund_movement_grant

pytestmark = pytest.mark.asyncio

MOVEMENT_TYPES = ("fund_transfer", "fund_release", "fund_reallocation", "fund_opening", "year_close")
# Every journal type the app writes today, and one it does not.
OTHER_TYPES = ("manual", "auto_posted", "transfer", "import", "opening_balance", "made_up_type")


async def _setup(client, auth) -> dict:
    """Funds on; a restricted fund, two unrestricted (board-designated) funds, and the
    restricted net assets account."""
    r = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": True})
    assert r.status_code == 200, r.text

    async def fund(name, restricted):
        r = await client.post("/accounting/funds", headers=auth["headers"], json={
            "request_key": uuid.uuid4().hex, "name": name, "restricted": restricted,
            "purpose": "Scholarships" if restricted else None})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    accounts = (await client.get("/accounting/posting-accounts", headers=auth["headers"])).json()
    rna = next(a for a in accounts["roles"] if a["role"] == "restricted_net_assets")["code"]
    return {"R": await fund("Library gift", True), "U1": await fund("Board reserve", False),
            "U2": await fund("Building reserve", False), "rna": rna}


def _lines(lines) -> list[dict]:
    return [{"account": a, "debit": d, "credit": c, **({"fund_id": f} if f else {})} for a, d, c, f in lines]


async def _emit(session, auth, lines, *, je_type="manual", entity_id=None, actor="user", restores=False, ts="2026-03-01"):
    from celerp.events.engine import emit_event
    je_id = entity_id or f"je:manual:{uuid.uuid4()}"
    await emit_event(session, company_id=auth["company_id"], entity_id=je_id, entity_type="journal_entry",
                     event_type="acc.journal_entry.created",
                     data={"memo": "Funds", "ts": ts, "entries": _lines(lines), "je_type": je_type,
                           "status": "posted"},
                     actor_id=auth["user_id"] if actor == "user" else actor, location_id=None, source="test",
                     idempotency_key=f"test:{je_id}:{uuid.uuid4().hex}",
                     metadata_={"restores": True} if restores else {})
    return je_id


async def _refused(coro) -> dict:
    with pytest.raises(RestrictedFundMovement) as raised:
        await coro
    return raised.value.detail


def _shapes(f) -> dict:
    """Each movement shape on its own, with a twin that has no shape and posts."""
    R, U1, U2, rna = f["R"], f["U1"], f["U2"], f["rna"]
    return {
        "a": ([("1111", 10, 0, R), (rna, 0, 10, R)], [("1111", 10, 0, R), ("4100", 0, 10, R)]),
        "c": ([("3200", 10, 0, U1), ("1111", 0, 10, U1), ("1111", 10, 0, U2), ("3200", 0, 10, U2)],
              [("3200", 10, 0, U1), ("1111", 0, 10, U1)]),
        "d": ([("1111", 40, 0, None), ("6200", 0, 40, None), ("6200", 40, 0, R), ("1111", 0, 40, R)],
              [("1111", 40, 0, R), ("6200", 0, 40, R), ("6200", 40, 0, R), ("1111", 0, 40, R)]),
    }


@pytest.mark.parametrize("je_type", MOVEMENT_TYPES + OTHER_TYPES)
async def test_movement_shape_refused_without_grant(client, session, auth, je_type):
    """Shapes (a), (c) and (d) are refused whatever the entry's type, and shape (b) is any
    movement type, even on one fund with no net-assets line. Each twin without the shape posts."""
    f = await _setup(client, auth)
    for shape, (lines, twin) in _shapes(f).items():
        detail = await _refused(_emit(session, auth, lines, je_type=je_type))
        assert detail["message_key"] == "posting.fund.movement_outside", (shape, detail)
        if je_type not in MOVEMENT_TYPES:
            await _emit(session, auth, twin, je_type=je_type)
    plain = [("1111", 10, 0, f["U1"]), ("1111", 0, 10, f["U1"])]
    if je_type in MOVEMENT_TYPES:
        assert (await _refused(_emit(session, auth, plain, je_type=je_type)))["message_key"] == \
            "posting.fund.movement_outside"
    else:
        await _emit(session, auth, plain, je_type=je_type)


async def test_movement_shape_refused_with_funds_turned_off(client, session, auth):
    """Once restricted net assets exists, a line on it or a movement type is still judged
    after funds are turned off."""
    f = await _setup(client, auth)
    r = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": False})
    assert r.status_code == 200, r.text
    await _refused(_emit(session, auth, [("1111", 10, 0, None), (f["rna"], 0, 10, None)]))
    await _refused(_emit(session, auth, [("1111", 10, 0, None), ("1111", 0, 10, None)], je_type="fund_transfer"))


async def test_movement_refused_for_system_actor(client, session, auth):
    """The system actor cannot move fund money, even inside a grant for that actor."""
    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 10, 0, f["U1"]), ("4100", 0, 10, f["U1"])])
    lines = _shapes(f)["c"][0]
    je_id = f"je:fund:{uuid.uuid4()}"
    async with fund_movement_grant(auth["company_id"], je_id, None):
        detail = await _refused(_emit(session, auth, lines, entity_id=je_id, actor=None, je_type="fund_transfer"))
    assert detail["message_key"] == "posting.fund.movement_needs_user"


async def test_grant_consumed_once_and_scoped_to_entity(client, session, auth):
    """A grant passes only the entity and actor it was entered for, and only once."""
    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 10, 0, f["U1"]), ("4100", 0, 10, f["U1"])])
    lines = _shapes(f)["c"][0]
    je_id = f"je:fund:{uuid.uuid4()}"
    async with fund_movement_grant(auth["company_id"], je_id, auth["user_id"]):
        await _refused(_emit(session, auth, lines, je_type="fund_transfer"))
        await _refused(_emit(session, auth, lines, entity_id=je_id, actor=uuid.uuid4(), je_type="fund_transfer"))
        await _emit(session, auth, lines, entity_id=je_id, je_type="fund_transfer")
        await _refused(_emit(session, auth, lines, entity_id=f"je:fund:{uuid.uuid4()}", je_type="fund_transfer"))


async def test_restore_of_movement_needs_grant(client, session, auth):
    """A restored copy of a movement-shaped entry is judged like a new one."""
    f = await _setup(client, auth)
    detail = await _refused(_emit(session, auth, _shapes(f)["a"][0], restores=True))
    assert detail["message_key"] == "posting.fund.movement_outside"


async def test_grant_not_usable_from_child_task(client, session, auth):
    """A task spawned inside the grant cannot use it for another entity, and a task spawned
    before the movement posted finds the grant spent once it has. The movement itself posts
    in the task that entered the grant, as post_fund_movement does; each spawned task posts
    on a session of its own, as it would in production."""
    from celerp import db

    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 10, 0, f["U1"]), ("4100", 0, 10, f["U1"])])
    lines = _shapes(f)["c"][0]
    je_id = f"je:fund:{uuid.uuid4()}"
    posted = asyncio.Event()

    async def child(entity_id=None, wait=False):
        if wait:
            await posted.wait()
        async with db.LifecycleSessionLocal() as own:
            return await _emit(own, auth, lines, entity_id=entity_id, je_type="fund_transfer")

    async with fund_movement_grant(auth["company_id"], je_id, auth["user_id"]):
        await _refused(asyncio.create_task(child()))
        late = asyncio.create_task(child(je_id, wait=True))
        await _emit(session, auth, lines, entity_id=je_id, je_type="fund_transfer")
        posted.set()
        await _refused(late)


async def test_value_cannot_cross_funds_outside_a_movement(client, session, auth):
    """Money spent from a restricted fund and earned by another in one entry is refused from the
    manual journal and from an import alike; an entry split between a restricted fund and the
    unrestricted group that moves nothing between them posts."""
    from celerp_accounting import import_service

    f = await _setup(client, auth)
    cross = [("6200", 25, 0, f["R"]), ("1111", 0, 25, f["R"]), ("1111", 25, 0, None), ("4100", 0, 25, None)]
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-01", "memo": "Cross", "entries": _lines(cross), "idempotency_token": uuid.uuid4().hex})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "posting.fund.movement_outside"

    record = import_service.AccImportRecord(
        entity_id=f"je:imp:{uuid.uuid4()}", event_type="acc.journal_entry.created", source="import",
        idempotency_key=f"imp:{uuid.uuid4().hex}",
        data={"memo": "Cross", "ts": "2026-03-01", "entries": _lines(cross), "je_type": "import"})
    outcome = await import_service.import_journal_records(session, auth["company_id"], auth["user_id"], [record],
                                                            origin="file_import")
    assert [o.status for o in outcome.records] in (["rejected"], ["failed"]), outcome.records
    assert "moves only through Transfer between funds" in str(outcome.records[0].message)

    split = [("6200", 30, 0, f["R"]), ("1111", 0, 30, f["R"]), ("6200", 70, 0, None), ("1111", 0, 70, None)]
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-01", "memo": "Split", "entries": _lines(split), "idempotency_token": uuid.uuid4().hex})
    assert r.status_code in (200, 201), r.text
