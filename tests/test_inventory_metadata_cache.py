# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory static-metadata cache: one snapshot per token per short window.

The inventory pages fetch the same seven getters on every load. The cache
collapses those repeated round-trips without ever serving a stale or poisoned
snapshot: a cold burst runs exactly one gather, the entry expires on a short
TTL, failures are never cached, a caller mutating what it reads cannot corrupt
the stored entry, the key is the whole token (never a decoded claim), and every
write that can change the snapshot invalidates it centrally.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

import ui.api_client as api


@pytest.fixture(autouse=True)
def _reset_metadata_cache():
    api._reset_metadata_cache_for_tests()
    yield
    api._reset_metadata_cache_for_tests()


def _install_counting_getters(monkeypatch):
    """Replace the seven getters with counters returning distinct shapes."""
    calls = {"count": 0}

    async def _one(_token):
        calls["count"] += 1
        await asyncio.sleep(0)
        return None

    async def _item_schema(_token):
        calls["count"] += 1
        return [{"key": "sku"}]

    async def _category_schemas(_token):
        calls["count"] += 1
        return {"ring": [{"key": "carat"}]}

    async def _display_names(_token):
        calls["count"] += 1
        return {"ring": "Rings"}

    async def _column_prefs(_token):
        calls["count"] += 1
        return {"__all__": ["sku", "name"]}

    async def _company(_token):
        calls["count"] += 1
        return {"currency": "USD", "settings": {"vertical": "jewelry"}}

    async def _locations(_token):
        calls["count"] += 1
        return {"items": [{"id": "loc1"}]}

    async def _units(_token):
        calls["count"] += 1
        return [{"name": "each"}]

    monkeypatch.setattr(api, "get_item_schema", _item_schema)
    monkeypatch.setattr(api, "get_all_category_schemas", _category_schemas)
    monkeypatch.setattr(api, "get_category_display_names", _display_names)
    monkeypatch.setattr(api, "get_column_prefs", _column_prefs)
    monkeypatch.setattr(api, "get_company", _company)
    monkeypatch.setattr(api, "get_locations", _locations)
    monkeypatch.setattr(api, "get_units", _units)
    return calls


@pytest.mark.asyncio
async def test_snapshot_has_seven_named_fields(monkeypatch):
    _install_counting_getters(monkeypatch)
    snap = await api.get_inventory_metadata("tok")
    assert snap._fields == (
        "item_schema",
        "category_schemas",
        "category_display_names",
        "column_prefs",
        "company",
        "locations",
        "units",
    )
    assert snap.units == [{"name": "each"}]
    assert snap.locations == {"items": [{"id": "loc1"}]}


@pytest.mark.asyncio
async def test_second_read_within_ttl_serves_cache(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 7
    await api.get_inventory_metadata("tok")
    # No new upstream calls: the second read is served from the cached snapshot.
    assert calls["count"] == 7


@pytest.mark.asyncio
async def test_cold_burst_runs_one_gather(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    results = await asyncio.gather(*[api.get_inventory_metadata("tok") for _ in range(8)])
    # Eight concurrent cold callers coalesce onto one seven-call fetch, not eight.
    assert calls["count"] == 7
    assert all(r.units == [{"name": "each"}] for r in results)


@pytest.mark.asyncio
async def test_expiry_refetches(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 7
    # Advance past the TTL by rewinding the stored timestamp.
    key = api._metadata_key("tok")
    ts, snap = api._metadata_cache[key]
    api._metadata_cache[key] = (ts - (api._METADATA_TTL_SECONDS + 1.0), snap)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 14


@pytest.mark.asyncio
async def test_failure_is_never_cached(monkeypatch):
    _install_counting_getters(monkeypatch)

    async def _boom(_token):
        raise api.APIError(500, "upstream down")

    monkeypatch.setattr(api, "get_company", _boom)
    with pytest.raises(api.APIError):
        await api.get_inventory_metadata("tok")
    # The failed fetch left no entry and no inflight marker behind.
    assert api._metadata_key("tok") not in api._metadata_cache
    assert api._metadata_key("tok") not in api._metadata_inflight


@pytest.mark.asyncio
async def test_returned_snapshot_is_defensively_copied(monkeypatch):
    _install_counting_getters(monkeypatch)
    first = await api.get_inventory_metadata("tok")
    first.units.append({"name": "poisoned"})
    first.company["currency"] = "ZZZ"
    second = await api.get_inventory_metadata("tok")
    # A caller mutating what it read cannot corrupt the stored snapshot.
    assert second.units == [{"name": "each"}]
    assert second.company["currency"] == "USD"


@pytest.mark.asyncio
async def test_key_is_token_hash_not_a_claim(monkeypatch):
    _install_counting_getters(monkeypatch)
    key = api._metadata_key("some-access-token")
    assert key == hashlib.sha256(b"some-access-token").digest()
    assert isinstance(key, bytes) and len(key) == 32
    # Two different tokens are two different keys even if they claim one company.
    assert api._metadata_key("token-a") != api._metadata_key("token-b")


@pytest.mark.asyncio
async def test_write_wrapper_invalidates_snapshot(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 7

    # A metadata write wrapper (patch_units) must drop the cached snapshot so the
    # next read reflects the write. Stub the underlying HTTP so no real call runs.
    async def _fake_patch_units(_token, _units):
        return _units

    # Exercise the real wrapper's invalidation by calling it through a stubbed client.
    class _FakeResp:
        def json(self):
            return [{"name": "each"}]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(api, "_api_client", lambda _token: _FakeClient())
    monkeypatch.setattr(api, "_raise", lambda r: r)
    await api.patch_units("tok", [{"name": "each"}])
    assert api._metadata_key("tok") not in api._metadata_cache
    await api.get_inventory_metadata("tok")
    # The read after the write did a fresh fetch (seven more calls).
    assert calls["count"] == 14
