# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every document and List line has a line id, by construction.

A line stored before line ids existed gets the id derived from its record id and its
position, wherever the record's state is built: a live write, a rebuild, the projection
integrity check and a backup restore all derive the same id the one-time backfill wrote,
so a hold naming that id never loses its line."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_helpers import company_auth

_KEPT = "11111111-1111-4111-8111-111111111111"


def _lines(*names: str) -> list[dict]:
    return [{"description": n, "quantity": 1, "unit_price": 10.0, "sell_by": "piece"} for n in names]


def _without_ids(value):
    """``value`` with every ``line_id`` taken out, as data written before line ids existed."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k != "line_id"}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


async def _make_legacy(session, company_id, entity_id: str) -> None:
    """Store the record and the events behind it without line ids, as an older version wrote them."""
    session.expire_all()
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    proj.state = _without_ids(proj.state)
    await session.commit()
    for row in (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id))).scalars():
        row.data = _without_ids(row.data)
    await session.commit()


async def _backfill(session, company_id, entity_id: str) -> dict:
    """Apply the backfill's fill to the stored record; returns the state it stored."""
    from celerp.services.document_lines import fill_line_ids
    session.expire_all()
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    state = dict(proj.state)
    assert fill_line_ids(state, entity_id)
    proj.state = state
    await session.commit()
    return state


async def _stored(session, company_id, entity_id: str) -> dict:
    session.expire_all()
    return (await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})).state


async def _legacy_invoice(client, session) -> tuple[dict, str]:
    """A finalized invoice with several line edits behind it, stored without line ids."""
    a = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    h = a["headers"]
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": _lines("A", "B")})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    current = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    r = await client.patch(f"/docs/{doc_id}", headers=h, json={"expected_version": current["version"], "fields_changed": {
        "line_items": {"new": [*current["line_items"], *_lines("GIA Certificate")]}}})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert r.status_code == 200, r.text
    await _make_legacy(session, a["company_id"], doc_id)
    return a, doc_id


def test_derived_line_id_is_frozen():
    from celerp.services.document_lines import derived_line_id
    assert derived_line_id("doc:PF-2611-0001", 5) == "7c751ce5-47a1-5fd4-8218-d61c0caeb43a"


def test_fill_line_ids_gives_only_idless_lines_their_derived_id():
    from celerp.services.document_lines import derived_line_id, fill_line_ids
    lines = [{"sku": "A"}, {"sku": "B", "line_id": ""}, {"sku": "C", "line_id": _KEPT}, {"sku": "D", "line_id": "x-1"}]
    originals = [dict(li) for li in lines]
    state = {"line_items": lines}
    assert fill_line_ids(state, "doc:D-1") is True
    assert [li["line_id"] for li in state["line_items"]] == [
        derived_line_id("doc:D-1", 0), derived_line_id("doc:D-1", 1), _KEPT, derived_line_id("doc:D-1", 3)]
    assert lines == originals, "the line dicts handed in are not changed"
    filled = state["line_items"]
    assert fill_line_ids(state, "doc:D-1") is False
    assert state["line_items"] is filled
    assert fill_line_ids({"line_items": []}, "doc:D-1") is False
    assert fill_line_ids({}, "doc:D-1") is False


def test_no_action_gives_a_stored_line_an_id_of_its_own():
    """A stored line always has its id, so no action falls back to a new one for a line that
    lacks it: a random id there would differ from the derived id every hold names. Red
    statement: a scan adding quantity to a List line gave it ``line.get("line_id") or uuid4``."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    fallback = re.compile(r"""\.get\(\s*["']line_id["']\s*\)\s*or\s*(str\(\s*)?uuid""")
    found = [f"{p.relative_to(root)}:{n}" for base in ("celerp", "ui", "default_modules")
             for p in (root / base).rglob("*.py") if "tests" not in p.parts
             for n, text in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if fallback.search(text)]
    assert found == []


def test_replay_of_legacy_document_derives_line_ids():
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.document_lines import derived_line_id
    created = LedgerEntry(entity_id="doc:L-1", entity_type="doc", event_type="doc.created",
                          data={"doc_type": "invoice", "line_items": _lines("A", "GIA Certificate")})
    expected = [derived_line_id("doc:L-1", 0), derived_line_id("doc:L-1", 1)]
    replayed = ProjectionEngine.replay({}, created)
    assert [li.get("line_id") for li in replayed["line_items"]] == expected
    fields = ProjectionEngine._next_fields({}, created, 1)
    assert [li.get("line_id") for li in fields["state"]["line_items"]] == expected
    item = LedgerEntry(entity_id="item:I-1", entity_type="item", event_type="item.created",
                       data={"sku": "S-1", "name": "Stone", "quantity": 1})
    assert ProjectionEngine.replay({}, item) == ProjectionEngine._apply({}, item.event_type, item.data)


@pytest.mark.asyncio
async def test_rebuild_after_backfill_matches_backfilled_state(client, session):
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company
    a, doc_id = await _legacy_invoice(client, session)
    backfilled = await _backfill(session, a["company_id"], doc_id)
    assert len(backfilled["line_items"]) == 3
    await lock_company(session, a["company_id"])
    await ProjectionEngine.rebuild(session, a["company_id"])
    await session.commit()
    assert await _stored(session, a["company_id"], doc_id) == backfilled


@pytest.mark.asyncio
async def test_integrity_check_reports_no_drift_after_backfill(client, session):
    from celerp_admin.routes import _check_stale_projections
    a, doc_id = await _legacy_invoice(client, session)
    backfilled = await _backfill(session, a["company_id"], doc_id)
    report = await _check_stale_projections(session, a["company_id"], None, fix=False)
    assert doc_id not in [d["entity_id"] for d in report["details"]], report
    await _check_stale_projections(session, a["company_id"], None, fix=True)
    await session.commit()
    assert (await _stored(session, a["company_id"], doc_id))["line_items"] == backfilled["line_items"]


# ── Backup restore ────────────────────────────────────────────────────────────

from company_backup_support import download, restore, settle  # noqa: E402
from migration_support import real_client, real_engine  # noqa: E402,F401
from test_company_backup import _bk_modules_running, _r_created, _r_env, _r_rows, _r_source, _r_with_rows  # noqa: E402,F401


@pytest.mark.asyncio
async def test_restore_of_backup_with_legacy_document_derives_line_ids(real_engine, real_client, tmp_path, monkeypatch):
    """A backup made before line ids existed holds document lines without them; it restores,
    and the restored lines carry the ids a replay of the document derives."""
    from sqlalchemy import text
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    doc_id = "doc:INV-LEGACY-1"
    async with real_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, actor_id, source, "
            "idempotency_key) VALUES (:c, :e, 'doc', 'doc.created', CAST(:d AS json), :u, 'api', 'legacy-doc')"),
            {"c": cid, "e": doc_id, "u": user,
             "d": json.dumps({"doc_type": "invoice", "ref_id": "INV-LEGACY-1", "line_items": _lines("A", "GIA Certificate")})})
    await settle(real_engine, cid)
    data = _r_with_rows(await download(real_client, tok), "projections",
                        lambda row: {**row, "state": _without_ids(row["state"])} if row["entity_id"] == doc_id else row)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    [restored] = [r for r in await _r_rows(real_engine, "projections", uuid.UUID(body["company_id"]))
                  if r["entity_id"] == doc_id]
    from celerp.services.document_lines import derived_line_id
    assert [li.get("line_id") for li in restored["state"]["line_items"]] == [
        derived_line_id(doc_id, 0), derived_line_id(doc_id, 1)]
