# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""ui.api_client.get_items_metadata chunks large id sets into bounded requests.

The server caps a single POST /items/metadata at MAX_ITEMS_METADATA ids and
rejects an over-cap body with 422. A list can hold more unique items than that
cap, so the client dedups and splits the ids into batches within the cap and
merges the per-batch maps. Without chunking, a single over-cap request would 422
and the whole list would lose its metadata.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest


class _FakeResp:
    is_error = False
    is_redirect = False

    def __init__(self, ids):
        self._ids = ids

    def json(self):
        return {"items": {i: {"id": i} for i in self._ids}}


def _fake_client(calls):
    class _FakeClient:
        async def post(self, path, json):
            batch = json["entity_ids"]
            calls.append(batch)
            return _FakeResp(batch)

    @asynccontextmanager
    async def _ctx(token, timeout: float = 10.0):
        yield _FakeClient()

    return _ctx


@pytest.mark.asyncio
async def test_get_items_metadata_chunks_over_cap(monkeypatch):
    """More unique ids than the server cap resolve in bounded batches, each within
    the cap, merged into one map covering every id."""
    import ui.api_client as api
    from celerp_inventory.routes import MAX_ITEMS_METADATA

    calls: list[list[str]] = []
    monkeypatch.setattr(api, "_api_client", _fake_client(calls))

    ids = [f"item:{i}" for i in range(MAX_ITEMS_METADATA + 1)]
    out = await api.get_items_metadata("tok", ids)

    assert set(out.keys()) == set(ids), "merged map must cover every requested id"
    assert len(calls) >= 2, "an over-cap id set must be split into multiple requests"
    assert all(len(b) <= MAX_ITEMS_METADATA for b in calls), "every batch must be within the server cap"
    assert sum(len(b) for b in calls) == len(ids), "no id sent twice and none dropped"


@pytest.mark.asyncio
async def test_get_items_metadata_dedups_and_skips_empty(monkeypatch):
    """Duplicate ids collapse before the request, and an empty id set makes no
    request at all."""
    import ui.api_client as api

    calls: list[list[str]] = []
    monkeypatch.setattr(api, "_api_client", _fake_client(calls))

    assert await api.get_items_metadata("tok", []) == {}
    assert calls == [], "an empty id set must not issue a request"

    out = await api.get_items_metadata("tok", ["item:a", "item:a", "item:b"])
    assert set(out.keys()) == {"item:a", "item:b"}
    assert calls[-1] == ["item:a", "item:b"], "duplicate ids must be deduped before the request"
