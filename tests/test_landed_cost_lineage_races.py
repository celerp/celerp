# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill going back to its order while its goods are merged or a merge is undone at the same
moment. The revert holds the company from its start, so a merge or an undo begun while it reads
the bill's lots waits for it and then works on what the revert left: the freight is gone from
every lot, the books tie, and no request ends in a server error."""
from __future__ import annotations

import asyncio

import pytest

import celerp_docs.routes as docs_routes
from migration_support import maker, real_client, real_engine  # noqa: F401
from stock_books import assert_settled
from test_company_backup import _bk_local, _bk_setup
from test_helpers import provision_company_books
from test_landed_cost_merge_lineage import _like, _merge, _pools, _shared_lot
from test_landed_cost_pools import _b, _post
from test_landed_cost_removals import _split
from test_receipt_accounting import _return

pytestmark = pytest.mark.asyncio


async def _company(real_engine, monkeypatch, tmp_path):
    _bk_local(monkeypatch, tmp_path)
    _user, cid, tok = await _bk_setup(real_engine)
    async with maker(real_engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    return {"headers": {"Authorization": f"Bearer {tok}"}, "company_id": cid}


def _between_read_and_locks(monkeypatch, act) -> list[asyncio.Task]:
    """Start ``act`` once, after the revert first reads the bill's lots and before it locks them."""
    original, ran = docs_routes.lineage, []

    async def _lineage(session, company_id, roots):
        family = await original(session, company_id, roots)
        if not ran:
            ran.append(asyncio.create_task(act()))
            await asyncio.sleep(0.5)  # the request reaches the company lock and waits there
            assert not ran[0].done(), ran[0]
        return family

    monkeypatch.setattr(docs_routes, "lineage", _lineage)
    return ran


async def test_a_merge_committed_while_the_revert_reads_still_loses_the_freight(
        real_engine, real_client, monkeypatch, tmp_path):
    auth, c = await _company(real_engine, monkeypatch, tmp_path), real_client
    async with maker(real_engine)() as s:
        doc, lot = await _shared_lot(c, auth)
        [part] = await _split(c, auth, lot, 10)
        other = await _like(c, s, auth, lot, 5, 50.0)
        r = await _return(c, auth, doc, lot, 10)
        assert r.status_code == 200, r.text
        before = await _b(s, auth, "5100")
        ran = _between_read_and_locks(monkeypatch, lambda: _merge(c, auth, [part, other]))
        r = await _post(c, auth, doc, "revert-to-draft")
        assert r.status_code == 200, r.text
        x = await asyncio.wait_for(ran[0], 30)
        assert await _pools(s, auth, doc, x, part, lot) == [0.0, 0.0, 0.0]
        assert await _b(s, auth, "5100") == before
        await assert_settled(c, s, auth)


async def test_undoing_a_merge_while_the_revert_reads_ends_without_a_server_error(
        real_engine, real_client, monkeypatch, tmp_path):
    auth, c = await _company(real_engine, monkeypatch, tmp_path), real_client
    async with maker(real_engine)() as s:
        doc, lot = await _shared_lot(c, auth)
        [part] = await _split(c, auth, lot, 10)
        x = await _merge(c, auth, [part, await _like(c, s, auth, lot, 5, 50.0)])
        r = await _return(c, auth, doc, lot, 10)
        assert r.status_code == 200, r.text
        ran = _between_read_and_locks(monkeypatch, lambda: c.post(f"/items/{x}/undo-merge", headers=auth["headers"]))
        r = await _post(c, auth, doc, "revert-to-draft")
        assert r.status_code == 200, r.text
        undo = await asyncio.wait_for(ran[0], 30)
        # The revert took the freight off the merged lot, so the merge is no longer as it was made.
        assert undo.status_code == 409, undo.text
        assert await _pools(s, auth, doc, part, x, lot) == [0.0, 0.0, 0.0]
        await assert_settled(c, s, auth)


async def test_undoing_a_merge_and_reverting_at_once_end_without_a_server_error(
        real_engine, real_client, monkeypatch, tmp_path):
    auth, c = await _company(real_engine, monkeypatch, tmp_path), real_client
    async with maker(real_engine)() as s:
        doc, lot = await _shared_lot(c, auth)
        [part] = await _split(c, auth, lot, 10)
        x = await _merge(c, auth, [part, await _like(c, s, auth, lot, 5, 50.0)])
        r = await _return(c, auth, doc, lot, 10)
        assert r.status_code == 200, r.text
        undo, revert = await asyncio.gather(c.post(f"/items/{x}/undo-merge", headers=auth["headers"]),
                                            _post(c, auth, doc, "revert-to-draft"))
        assert undo.status_code in (200, 409) and revert.status_code == 200, (undo.text, revert.text)
        assert await _pools(s, auth, doc, part, x, lot) == [0.0, 0.0, 0.0]
        await assert_settled(c, s, auth)
