# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A repeat delivery of a merge gets the first merge's result, but once that merge was undone its
result no longer exists as stock: the repeat is refused, and the lots stay as the undo left them."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_posting_roles_merge import _lot


@pytest.mark.asyncio
async def test_replaying_an_undone_merge_is_refused(session, client, auth):
    h = auth["headers"]
    a, b = await _lot(client, auth, 10.0), await _lot(client, auth, 20.0)
    body = {"source_entity_ids": [a, b], "target_sku_from": a, "idempotency_key": f"k-{uuid.uuid4().hex}"}
    fp = (await client.post("/items/merge/preview", headers=h, json=body)).json()["plan_fingerprint"]
    first = await client.post("/items/merge", headers=h, json={**body, "plan_fingerprint": fp})
    assert first.status_code == 200, first.text
    merged = first.json()["id"]
    again = await client.post("/items/merge", headers=h, json={**body, "plan_fingerprint": fp})
    assert (again.status_code, again.json()["id"]) == (200, merged), again.text
    assert (await client.post(f"/items/{merged}/undo-merge", headers=h)).status_code == 200

    r = await client.post("/items/merge", headers=h, json={**body, "plan_fingerprint": fp})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "This merge was undone. Review the merge again."
    assert (await _state(session, auth, merged))["status"] == "archived"
    for lot in (a, b):
        assert (await _state(session, auth, lot))["status"] == "available"
    await assert_settled(client, session, auth)
