# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Merging lots across inventory accounts under real concurrent requests.

A merge and a cost correction to one of its lots, or two deliveries of the same
merge, run as separate requests on their own connections against committed data.
The only acceptable outcome is what one serial order would have produced: the
entry moves exactly the value the lot carried when the merge ran, and one merge
moves its value once.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _post(client, tok: str, path: str, body: dict | None = None, status: int = 200) -> dict:
    r = await client.post(path, headers=auth(tok), json=body)
    assert r.status_code == status, r.text
    return r.json()


async def _books(engine, client):
    from test_helpers import provision_company_books

    user = await owner(engine)
    cid = await company(engine, user, "Merge Race", "merge-race", settings={"currency": "USD"})
    async with maker(engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    tok = await token(engine, user, cid)
    a = await _lot(client, tok, "A", 600.0)
    await _post(client, tok, "/accounting/accounts", {"code": "1131", "name": "Stock 1131",
                                                     "account_type": "asset", "parent_code": "1130"})
    r = await client.put("/accounting/posting-accounts/inventory_purchased", headers=auth(tok), json={"code": "1131"})
    assert r.status_code == 200, r.text
    b = await _lot(client, tok, "B", 400.0)
    return cid, tok, a, b


async def _lot(client, tok: str, sku: str, cost: float) -> str:
    return (await _post(client, tok, "/items", {"sku": sku, "name": "Lot", "quantity": 1, "sell_by": "piece",
                                                "status": "available", "cost_total": cost}))["id"]


async def _rows(engine, cid, sql: str, **params) -> list:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), {"c": uuid.UUID(str(cid)), **params})).all()


async def _reclass_entries(engine, cid) -> dict[str, dict]:
    rows = await _rows(engine, cid, "SELECT entity_id, state FROM projections WHERE company_id = :c "
                                    "AND entity_id LIKE :pattern", pattern="je:auto:%:merge-reclass")
    return {eid: {e["account"]: (e.get("debit") or 0, e.get("credit") or 0) for e in state["entries"]}
            for eid, state in rows}


async def _state(engine, cid, entity_id: str) -> dict:
    (row,) = await _rows(engine, cid, "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e",
                         e=entity_id)
    return row[0]


async def _seq(engine, cid, event_type: str, entity_id: str) -> int:
    (row,) = await _rows(engine, cid, "SELECT min(id) FROM ledger WHERE company_id = :c "
                                      "AND event_type = :t AND entity_id = :e", t=event_type, e=entity_id)
    return row[0]


async def _held(engine, cid):
    """A transaction holding the company row, so both requests queue behind it."""
    conn = await engine.connect()
    tx = await conn.begin()
    await conn.execute(text("SELECT id FROM companies WHERE id = :c FOR UPDATE"), {"c": uuid.UUID(str(cid))})
    return conn, tx


async def _blocked(engine, n: int) -> None:
    for _ in range(400):
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"))).scalar_one()
        if waiting >= n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{n} requests never queued on the company lock")


@pytest.mark.parametrize("correction_first", [True, False])
async def test_a_cost_correction_and_a_merge_serialize(real_engine, real_client, correction_first):
    cid, tok, a, b = await _books(real_engine, real_client)
    merge_body = {"source_entity_ids": [a, b], "target_sku_from": a}
    correction = {"fields_changed": {"cost_total": {"old": 400.0, "new": 450.0}}}

    conn, tx = await _held(real_engine, cid)
    try:
        first = (real_client.patch(f"/items/{b}", headers=auth(tok), json=correction) if correction_first
                 else real_client.post("/items/merge", headers=auth(tok), json=merge_body))
        t1 = asyncio.create_task(first)
        await _blocked(real_engine, 1)
        second = (real_client.post("/items/merge", headers=auth(tok), json=merge_body) if correction_first
                  else real_client.patch(f"/items/{b}", headers=auth(tok), json=correction))
        t2 = asyncio.create_task(second)
        await _blocked(real_engine, 2)
    finally:
        await tx.commit()
        await conn.close()
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=60)
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)

    merged_id = (r2 if correction_first else r1).json()["id"]
    [(je_id, lines)] = (await _reclass_entries(real_engine, cid)).items()
    assert je_id == f"je:auto:{merged_id}:merge-reclass"
    corrected_before_merge = (await _seq(real_engine, cid, "item.created", merged_id)
                              > await _seq(real_engine, cid, "item.updated", b))
    assert corrected_before_merge is correction_first
    moved = 450.0 if correction_first else 400.0
    assert lines == {"1130-P": (moved, 0), "1131": (0, moved)}
    assert (await _state(real_engine, cid, merged_id))["cost_total"] == 1050.0


async def test_two_deliveries_of_one_merge_move_the_value_once(real_engine, real_client):
    cid, tok, a, b = await _books(real_engine, real_client)
    body = {"source_entity_ids": [a, b], "target_sku_from": a, "idempotency_key": f"merge-{uuid.uuid4().hex}"}
    conn, tx = await _held(real_engine, cid)
    try:
        t1 = asyncio.create_task(real_client.post("/items/merge", headers=auth(tok), json=body))
        t2 = asyncio.create_task(real_client.post("/items/merge", headers=auth(tok), json=body))
        await _blocked(real_engine, 2)
    finally:
        await tx.commit()
        await conn.close()
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=60)
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert r1.json() == r2.json()
    merged_id = r1.json()["id"]
    assert await _reclass_entries(real_engine, cid) == {
        f"je:auto:{merged_id}:merge-reclass": {"1130-P": (400.0, 0), "1131": (0, 400.0)}}
    items = await _rows(real_engine, cid, "SELECT entity_id FROM projections WHERE company_id = :c "
                                          "AND entity_type = 'item' AND state ->> 'sku' = 'A'")
    assert sorted(i for (i,) in items) == sorted([a, merged_id])


async def test_a_cost_correction_landing_between_preview_and_confirm_refuses_the_merge(real_engine, real_client):
    cid, tok, a, b = await _books(real_engine, real_client)
    preview = await _post(real_client, tok, "/items/merge/preview", {"source_entity_ids": [a, b], "target_sku_from": a})
    merge_body = {"source_entity_ids": [a, b], "target_sku_from": a,
                  "plan_fingerprint": preview["plan_fingerprint"]}
    correction = {"fields_changed": {"cost_total": {"old": 400.0, "new": 450.0}}}
    created = "SELECT count(*) FROM ledger WHERE company_id = :c AND event_type = 'item.created'"
    (created_before,) = await _rows(real_engine, cid, created)

    conn, tx = await _held(real_engine, cid)
    try:
        t1 = asyncio.create_task(real_client.patch(f"/items/{b}", headers=auth(tok), json=correction))
        await _blocked(real_engine, 1)
        t2 = asyncio.create_task(real_client.post("/items/merge", headers=auth(tok), json=merge_body))
        await _blocked(real_engine, 2)
    finally:
        await tx.commit()
        await conn.close()
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=60)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"] == "The inventory changed since this merge was reviewed. Review the merge again."
    # The correction went through; the merge left nothing behind.
    assert await _rows(real_engine, cid, created) == [created_before]
    assert await _rows(real_engine, cid, "SELECT id FROM ledger WHERE company_id = :c AND event_type IN "
                                         "('item.merged', 'item.source_deactivated')") == []
    assert await _reclass_entries(real_engine, cid) == {}
    assert (await _state(real_engine, cid, b))["status"] == "available"
