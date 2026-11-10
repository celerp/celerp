# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A number an earlier version saved padded stays padded in the ledger, which is never
rewritten: every record replayed from that history stores it stripped (strip_number), so the
upgrade's backfill, a ledger rebuild and a restored company backup all give the record the same
number, and that number stays held."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.json_text import SYNC_TEXT, dump_json
from celerp.migrations._json_compat import update_projection_state
from celerp.models.projections import strip_number
from company_backup_support import download, restore, settle
from migration_support import auth, real_client, real_engine  # noqa: F401
from test_company_backup import _r_env, _r_source

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "r1s2", ROOT / "celerp/migrations/versions/r1s2t3u4v5w6_projections_canonical_number_lookup.py")
r1s2 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r1s2)

# Each space an earlier version could save around a number: ASCII space, tab, NBSP, ideographic.
PADS = {"space": " ", "tab": "\t", "nbsp": "\xa0", "ideographic": "　"}
PADDED = {f"LEG-{i}-0001": f"{pad}LEG-{i}-0001{pad}" for i, pad in enumerate(PADS.values())}
LISTED = ("LEG-L-0001", "\xa0LEG-L-0001　")


async def _legacy_history(engine, cid) -> None:
    """Documents and a List as an earlier version saved them: the padded number in the event and
    in the record."""
    events = [("doc", "doc.created", {"doc_type": "quotation", "ref_id": padded, "status": "draft",
                                      "line_items": [], "total": 0}) for padded in PADDED.values()]
    events.append(("list", "list.created", {"ref_id": LISTED[1], "status": "draft", "line_items": []}))
    async with engine.begin() as conn:
        for kind, event_type, data in events:
            eid = f"{kind}:{uuid.uuid4()}"
            await conn.execute(text(
                "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key) "
                "VALUES (:c, :e, :t, :y, CAST(:d AS json), 'api', :k)"),
                {"c": cid, "e": eid, "t": kind, "y": event_type, "d": json.dumps(data), "k": f"k-{eid}"})
    await settle(engine, cid)
    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT entity_id, state::text FROM projections "
                                        "WHERE company_id = :c AND entity_type IN ('doc', 'list')"), {"c": cid})).all()
        padded = {strip_number(n): n for n in (*PADDED.values(), LISTED[1])}
        for eid, state in rows:
            state = json.loads(state)
            state["ref_id"] = padded[strip_number(state["ref_id"])]
            await conn.execute(text("UPDATE projections SET state = CAST(:s AS json) WHERE company_id = :c "
                                    "AND entity_id = :e"), {"s": dump_json(state), "c": cid, "e": eid})


def _upgrade_backfill(engine) -> None:
    """The upgrade's backfill on a psycopg2 connection, as alembic runs it."""
    sync = create_engine(engine.url.set(drivername="postgresql+psycopg2"), **SYNC_TEXT)
    try:
        with sync.begin() as conn:
            update_projection_state(conn, r1s2._strip_numbers, where="entity_type IN ('doc', 'list')")
    finally:
        sync.dispose()


async def _states(engine, cid) -> dict:
    async with engine.connect() as conn:
        return {eid: json.loads(s) for eid, s in (await conn.execute(text(
            "SELECT entity_id, state::text FROM projections WHERE company_id = :c"), {"c": cid})).all()}


async def _numbers(engine, cid) -> list[str]:
    return sorted(s["ref_id"] for s in (await _states(engine, cid)).values() if "ref_id" in s)


async def _holders(engine, cid) -> dict:
    from celerp_docs.sequences import number_holder
    async with AsyncSession(engine) as s:
        return {n: await number_holder(s, str(cid), n) for n in (*PADDED, LISTED[0])}


async def _restored(engine, source) -> uuid.UUID:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT company_id FROM projections WHERE company_id <> :c "
                                        "AND entity_type = 'doc' LIMIT 1"), {"c": source})).scalar_one()


async def test_upgraded_history_equals_its_replay_rebuilds_and_restores_with_every_number_held(
        real_engine, real_client, tmp_path, monkeypatch):
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _legacy_history(real_engine, cid)
    _upgrade_backfill(real_engine)
    upgraded = await _states(real_engine, cid)
    expected = sorted((*PADDED, LISTED[0]))
    assert await _numbers(real_engine, cid) == expected

    r = await real_client.post("/ledger/rebuild", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert await _states(real_engine, cid) == upgraded  # the backfill is what a replay stores
    holders = await _holders(real_engine, cid)
    assert all(holders.values()), holders

    r = await restore(real_client, tok, await download(real_client, tok), mode="new_company")
    assert r.status_code == 201, r.text
    copy = await _restored(real_engine, cid)
    assert await _numbers(real_engine, copy) == expected
    assert all((await _holders(real_engine, copy)).values())


async def test_a_backup_made_before_the_upgrade_restores_with_every_number_stripped_and_held(
        real_engine, real_client, tmp_path, monkeypatch):
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _legacy_history(real_engine, cid)
    r = await restore(real_client, tok, await download(real_client, tok), mode="new_company")
    assert r.status_code == 201, r.text
    copy = await _restored(real_engine, cid)
    assert await _numbers(real_engine, copy) == sorted((*PADDED, LISTED[0]))
    assert all((await _holders(real_engine, copy)).values())
