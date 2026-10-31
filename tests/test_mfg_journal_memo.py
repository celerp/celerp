# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A production run's journal entries name the run the way people see it.

The app shows a run by its reference (WO- and the start of its id), so the memo of every
entry a run posts names that reference, never the internal id. Entries an older release
stored with the internal id read the same way on every journal view, and the stored memo
is never rewritten.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from celerp.services.ledger_display import run_reference
from mfg_runs import WIP, complete, issue, receive, role
from test_cost_restatement import _state
from test_mfg_wip import _job


def test_a_run_reference_is_wo_and_the_start_of_its_id():
    assert run_reference("mfg:155c8df3-0d6e-4b0a-9a51-3f7c1f0e2a11") == "WO-155c8df3"


async def _memos(client, auth, *paths: str) -> dict[str, list[str]]:
    """Every memo each journal view shows."""
    out = {}
    for path in paths:
        r = await client.get(path, headers=auth["headers"])
        assert r.status_code == 200, r.text
        out[path] = list(_memo_values(r.json()))
    return out


def _memo_values(node):
    if isinstance(node, dict):
        if isinstance(node.get("memo"), str):
            yield node["memo"]
        for value in node.values():
            yield from _memo_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _memo_values(value)


@pytest.mark.asyncio
async def test_every_entry_a_run_posts_names_its_reference(client, session, auth):
    raw, _, order = await _job(client, session, auth, cost=100.0, stock=10, per=2, qty=5)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    assert (await receive(client, auth, order, 3, key="r")).status_code == 200
    assert (await complete(client, auth, order, key="c")).status_code == 200

    ref = run_reference(order)
    memos = [(await _state(session, auth, f"je:auto:{order}:{m}")).get("memo") or ""
             for m in ("issue:i", "receive:r")]
    assert memos == [f"Components issued to production run {ref}", f"Output received from production run {ref}"]
    for path, shown in (await _memos(client, auth, "/accounting/journal", "/accounting/extended-journal")).items():
        run_rows = [m for m in shown if "production run" in m.lower()]
        assert run_rows, path
        assert all(ref in m and order not in m for m in run_rows), (path, run_rows)


@pytest.mark.asyncio
async def test_a_stored_memo_with_the_run_id_reads_as_the_reference_and_stays_stored(client, session, auth):
    """An entry an older release posted names the run by its id: every journal view shows the
    reference, and the stored memo keeps the text that was posted."""
    order = f"mfg:{uuid.uuid4()}"
    wip = await role(session, auth, WIP)
    je_id = f"je:auto:{order}:receive:old"
    stored = f"Output received from production run {order}"
    await emit_event(
        session, company_id=auth["company_id"], entity_id=je_id, entity_type="journal_entry",
        event_type="acc.journal_entry.created", actor_id=None, location_id=None, source="system",
        idempotency_key=f"{je_id}:created", metadata_={},
        data={"memo": stored, "ts": "2026-01-15", "entries": [
            {"account": "1200", "debit": 25.0, "credit": 0.0}, {"account": wip, "debit": 0.0, "credit": 25.0}]})
    await session.commit()

    ref = run_reference(order)
    shown = await _memos(client, auth, "/accounting/journal", "/accounting/extended-journal",
                         f"/accounting/ledger/{wip}", "/accounting/general-ledger?include_lines=true")
    for path, memos in shown.items():
        assert f"Output received from production run {ref}" in memos, (path, memos)
        assert not any(order in m for m in memos), path
    assert (await _state(session, auth, je_id))["memo"] == stored
