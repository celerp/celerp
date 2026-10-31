# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""An import retried after a dropped response answers as its first write did, for every role.

Every import transport (the browser rows, the uploaded file, the raw record batch with and
without upsert) creates a new item with a cost, then receives the same submission again and
then a changed one. The exact retry writes nothing and gets the same answer whether or not
the importer may see costs. The changed retry is a cost change to an existing item: it
applies for an importer who sees costs and is refused for one who does not.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from test_cost_restatement import _state
from test_import_cost_authorization import _call, _grant, _user
from test_import_preflight_invariants import write_upload  # noqa: F401  (fixture)

_TRANSPORTS = ["rows", "file", "batch-upsert", "batch-insert"]


async def _submit(transport: str, client, h, write_upload, auth, *, sku: str, cost: str, key: str,  # noqa: F811
                  entity_id: str, uploads: dict):
    row = {"sku": sku, "name": "Fresh", "location_name": "Shelf", "sell_by": "piece", "quantity": 1,
           "cost_price_total": cost}
    if transport == "rows":
        _preview, commit = await _call(transport, client, h, write_upload, auth, row, lot=None, key=key)
        return commit
    if transport == "file":
        # A retried commit sends the same body again: the file and the preview it was asked on.
        text = ",".join(row) + "\n" + ",".join(str(v) for v in row.values()) + "\n"
        if text not in uploads:
            fid = write_upload(str(auth["company_id"]), _user(h), text)
            preview = await client.post("/items/import/preview", headers=h, json={"file_id": fid, "upsert": True})
            uploads[text] = {"file_id": fid, "upsert": True, "preview_hash": preview.json().get("preview_hash")}
        return await client.post("/items/import/commit", headers=h, json=uploads[text])
    record = {"entity_id": entity_id, "event_type": "item.created", "idempotency_key": key, "source": "csv_import",
              "data": {"sku": sku, "name": "Fresh", "quantity": 1, "sell_by": "piece", "cost_total": float(cost)}}
    return await client.post("/items/import/batch", headers=h,
                             json={"records": [record], "upsert": transport == "batch-upsert"})


def _answer(r) -> tuple:
    body = r.json()
    return r.status_code, {k: body.get(k) for k in ("created", "updated", "skipped", "errors")}


async def _writes(session, auth) -> int:
    from celerp.models.ledger import LedgerEntry
    session.expire_all()
    return (await session.execute(
        select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == auth["company_id"])
    )).scalar_one()


async def _lot_of(session, auth, sku: str) -> str:
    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection).where(Projection.company_id == auth["company_id"]))).scalars().all()
    [lot] = [p.entity_id for p in rows if (p.state or {}).get("sku") == sku]
    return lot


@pytest.mark.parametrize("transport", _TRANSPORTS)
async def test_an_import_retry_answers_as_its_first_write_for_every_role(client, session, auth, write_upload, transport):  # noqa: F811
    from test_company_settings_owners import _user_with_role
    r = await client.post("/companies/me/locations", headers=auth["headers"], json={"name": "Shelf", "type": "warehouse"})
    assert r.status_code == 200, r.text
    operator = await _user_with_role(client, session, auth["headers"], "operator")

    retries = {}
    for see_costs in (True, False):
        await _grant(session, auth, see_costs=see_costs)
        tag = uuid.uuid4().hex[:6]
        args = {"sku": f"RT-{tag}", "key": f"retry-{tag}", "entity_id": f"item:{uuid.uuid4()}", "uploads": {}}
        first = await _submit(transport, client, operator, write_upload, auth, cost="50", **args)
        assert first.status_code == 200 and first.json()["created"] == 1 and not first.json()["errors"], first.text
        lot = await _lot_of(session, auth, args["sku"])
        writes = await _writes(session, auth)

        exact = await _submit(transport, client, operator, write_upload, auth, cost="50", **args)
        status, answer = _answer(exact)
        assert status == 200 and not answer["errors"] and not answer["created"] and not answer["updated"], (see_costs, exact.text)
        assert await _writes(session, auth) == writes, see_costs
        retries[see_costs] = (status, answer)

        changed = await _submit(transport, client, operator, write_upload, auth, cost="60", **args)
        cost = (await _state(session, auth, lot)).get("cost_total")
        if not see_costs:
            assert "view_inventory_costs" in changed.text, changed.text
            assert await _writes(session, auth) == writes and cost == 50.0, changed.text
        elif transport == "batch-insert":
            # Without upsert a record whose key already created its item writes nothing.
            assert _answer(changed) == (200, {"created": 0, "updated": 0, "skipped": 1, "errors": []}), changed.text
            assert cost == 50.0
        else:
            assert changed.status_code == 200 and changed.json()["updated"] == 1, changed.text
            assert cost == 60.0, changed.text

    assert retries[True] == retries[False]
