# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Two people saving a lot's cost from the same opened copy, at the same moment, on real
Postgres. Each sends the version of the lot it opened; the cost writer checks it under the
lot's lock, so exactly one save lands. The other is refused (409) and leaves nothing behind:
no value, no change to whether the goods cost is known, no journal entry. Sending either
again with its own key changes nothing more."""
from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

import celerp_inventory.services as inventory_services
from migration_support import count, maker, real_client, real_engine  # noqa: F401
from test_company_backup import _bk_local, _bk_setup
from test_helpers import provision_company_books

pytestmark = pytest.mark.asyncio

MARK = "goods_cost_unknown"


async def _company(real_engine, monkeypatch, tmp_path):
    _bk_local(monkeypatch, tmp_path)
    _user, cid, tok = await _bk_setup(real_engine)
    async with maker(real_engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    return {"headers": {"Authorization": f"Bearer {tok}"}, "company_id": cid}


async def _lot(engine, auth, lot: str) -> tuple[dict, int]:
    from celerp.models.projections import Projection
    async with maker(engine)() as s:
        row = await s.get(Projection, {"company_id": auth["company_id"], "entity_id": lot})
        return dict(row.state), row.version


def _slow_first_restatement(monkeypatch) -> None:
    """Hold the first cost write inside its locked operation long enough for the second to arrive."""
    original, calls = inventory_services._restatement, []

    async def _restatement(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(inventory_services, "_restatement", _restatement)


async def test_two_cost_saves_from_one_opened_version_land_once(real_engine, real_client, monkeypatch, tmp_path):
    auth, c = await _company(real_engine, monkeypatch, tmp_path), real_client
    r = await c.post("/items", headers=auth["headers"], json={
        "sku": f"STALE-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "sell_by": "piece",
        "category": "Beads", "status": "available", "cost_total": 100.0})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    before, version = await _lot(real_engine, auth, lot)
    events = await count(real_engine, "ledger", "entity_id = :e", e=lot)
    keys = [f"stale-{uuid.uuid4().hex[:8]}" for _ in range(2)]

    def _save(key: str, value: float):
        return c.patch(f"/items/{lot}", headers=auth["headers"], json={
            "fields_changed": {"cost_total": {"old": 100.0, "new": value}},
            "expected_version": version, "idempotency_key": key})

    _slow_first_restatement(monkeypatch)
    first, second = await asyncio.gather(_save(keys[0], 120.0), _save(keys[1], 140.0))
    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
    (won, won_key, value), (lost, lost_key) = (
        ((first, keys[0], 120.0), (second, keys[1])) if first.status_code == 200
        else ((second, keys[1], 140.0), (first, keys[0])))
    after, _ = await _lot(real_engine, auth, lot)
    assert after["cost_total"] == value and after.get(MARK) == before.get(MARK), after
    assert "100" not in lost.text and str(value) not in lost.text, lost.text
    written = await count(real_engine, "ledger", "entity_id = :e", e=lot)
    assert written == events + 1
    assert await count(real_engine, "ledger", "idempotency_key = :k", k=lost_key) == 0
    # A restatement names its entries by its key; the refused one has none.
    lost_identity = hashlib.sha256(lost_key.encode()).hexdigest()[:24]
    assert await count(real_engine, "projections", "entity_id LIKE :j", j=f"%{lost_identity}%") == 0
    jes = await count(real_engine, "projections", "entity_type = 'journal_entry'")

    again = await _save(won_key, value)
    assert again.status_code == 200 and again.json()["event_id"] == won.json()["event_id"], again.text
    assert (await _save(lost_key, 140.0 if value == 120.0 else 120.0)).status_code == 409
    assert (await _lot(real_engine, auth, lot))[0]["cost_total"] == value
    assert await count(real_engine, "ledger", "entity_id = :e", e=lot) == written
    assert await count(real_engine, "projections", "entity_type = 'journal_entry'") == jes
    # The version a browser sends is the one the item read gives it.
    assert (await c.get(f"/items/{lot}", headers=auth["headers"])).json()["_version"] == (await _lot(real_engine, auth, lot))[1]
    # The version guards a cost: sent with no cost change it is refused, and nothing is written.
    r = await c.patch(f"/items/{lot}", headers=auth["headers"], json={
        "fields_changed": {"name": {"old": "Beads", "new": "Glass beads"}}, "expected_version": version})
    assert r.status_code == 422 and "expected_version" in r.text, r.text
    assert await count(real_engine, "ledger", "entity_id = :e", e=lot) == written
