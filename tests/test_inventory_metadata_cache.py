# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory static-metadata cache: one snapshot per token per short window.

The inventory pages fetch the same six static getters on every load. The cache
collapses those repeated round-trips without ever serving a stale or poisoned
snapshot: a cold burst runs exactly one gather, the entry expires on a short
TTL, failures are never cached, a caller mutating what it reads cannot corrupt
the stored entry, the key is the whole token (never a decoded claim), and every
write that can change the snapshot invalidates it process-wide.

Company/settings is deliberately outside this cache: it carries authorization
state and is fetched fresh per request, so it is never one of the cached getters.
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
    """Replace the six static getters with counters returning distinct shapes.

    ``company`` is patched too, with its own counter, purely to prove the cache
    never calls it.
    """
    calls = {"count": 0, "company": 0}

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
        calls["company"] += 1
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
async def test_snapshot_has_six_named_fields_without_company(monkeypatch):
    _install_counting_getters(monkeypatch)
    snap = await api.get_inventory_metadata("tok")
    assert snap._fields == (
        "item_schema",
        "category_schemas",
        "category_display_names",
        "column_prefs",
        "locations",
        "units",
    )
    assert "company" not in snap._fields
    assert snap.units == [{"name": "each"}]
    assert snap.locations == {"items": [{"id": "loc1"}]}


@pytest.mark.asyncio
async def test_cold_burst_runs_one_six_call_gather(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    results = await asyncio.gather(*[api.get_inventory_metadata("tok") for _ in range(8)])
    # Eight concurrent cold callers coalesce onto one six-call fetch, not eight,
    # and company is never fetched inside the cache.
    assert calls["count"] == 6
    assert calls["company"] == 0
    assert all(r.units == [{"name": "each"}] for r in results)


@pytest.mark.asyncio
async def test_warm_hit_makes_zero_static_getter_calls(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 6
    await api.get_inventory_metadata("tok")
    # The warm second read is served from cache with no new static getter calls.
    assert calls["count"] == 6
    assert calls["company"] == 0


@pytest.mark.asyncio
async def test_expiry_refetches(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 6
    key = api._metadata_key("tok")
    ts, snap = api._metadata_cache[key]
    api._metadata_cache[key] = (ts - (api._METADATA_TTL_SECONDS + 1.0), snap)
    await api.get_inventory_metadata("tok")
    assert calls["count"] == 12


@pytest.mark.asyncio
async def test_failure_is_never_cached(monkeypatch):
    _install_counting_getters(monkeypatch)

    async def _boom(_token):
        raise api.APIError(500, "upstream down")

    monkeypatch.setattr(api, "get_units", _boom)
    with pytest.raises(api.APIError):
        await api.get_inventory_metadata("tok")
    assert api._metadata_key("tok") not in api._metadata_cache
    assert api._metadata_key("tok") not in api._metadata_inflight


@pytest.mark.asyncio
async def test_all_waiters_cancel_then_fetch_fails_self_cleans(monkeypatch):
    gate = asyncio.Event()

    async def _blocking_fetch(_token):
        await gate.wait()
        raise api.APIError(500, "down")

    monkeypatch.setattr(api, "_fetch_inventory_metadata", _blocking_fetch)
    waiters = [asyncio.ensure_future(api.get_inventory_metadata("tok")) for _ in range(3)]
    await asyncio.sleep(0.02)
    assert api._metadata_key("tok") in api._metadata_inflight
    for w in waiters:
        w.cancel()
    for w in waiters:
        with pytest.raises(asyncio.CancelledError):
            await w
    gate.set()
    await asyncio.sleep(0.05)
    # The shared fetch, not a waiter, cleaned itself up after failing with no
    # waiters left.
    assert api._metadata_key("tok") not in api._metadata_inflight
    assert not api._metadata_tasks


def _tagged_fetch(tag, gate):
    async def _f(_token):
        await gate.wait()
        return api.InventoryMetadata([{"tag": tag}], {}, {}, {}, {}, [])

    return _f


@pytest.mark.asyncio
async def test_invalidation_during_inflight_caches_only_the_new_fetch(monkeypatch):
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()
    key = api._metadata_key("tok")

    monkeypatch.setattr(api, "_fetch_inventory_metadata", _tagged_fetch("A", gate_a))
    a_task = asyncio.ensure_future(api.get_inventory_metadata("tok"))
    await asyncio.sleep(0.02)
    assert key in api._metadata_inflight

    # A metadata write invalidates while fetch A is still running.
    api._invalidate_inventory_metadata()

    # A post-write caller starts fetch B.
    monkeypatch.setattr(api, "_fetch_inventory_metadata", _tagged_fetch("B", gate_b))
    b_task = asyncio.ensure_future(api.get_inventory_metadata("tok"))
    await asyncio.sleep(0.02)

    gate_a.set()
    await a_task  # A finishes first, but its generation is stale
    gate_b.set()
    await b_task

    ts, snap = api._metadata_cache[key]
    assert snap.item_schema == [{"tag": "B"}], "the stale pre-write fetch must not win"


@pytest.mark.asyncio
async def test_mutation_clears_static_metadata_for_all_tokens(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    await api.get_inventory_metadata("tok-a")
    await api.get_inventory_metadata("tok-b")
    assert api._metadata_key("tok-a") in api._metadata_cache
    assert api._metadata_key("tok-b") in api._metadata_cache

    # A settings write by token A invalidates everyone's static metadata, because
    # a settings change can alter what another user sees.
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
    await api.patch_units("tok-a", [{"name": "each"}])
    assert api._metadata_key("tok-a") not in api._metadata_cache
    assert api._metadata_key("tok-b") not in api._metadata_cache


@pytest.mark.asyncio
async def test_expired_entries_for_unrelated_tokens_are_pruned(monkeypatch):
    calls = _install_counting_getters(monkeypatch)
    other = api._metadata_key("stale-token")
    snap = api.InventoryMetadata([], {}, {}, {}, {}, [])
    import time as _time

    api._metadata_cache[other] = (
        _time.monotonic() - (api._METADATA_TTL_SECONDS + 1.0),
        snap,
    )
    # A read for a different token prunes the unrelated expired entry globally.
    await api.get_inventory_metadata("tok")
    assert other not in api._metadata_cache


@pytest.mark.asyncio
async def test_shutdown_clears_and_cancels_metadata_tasks(monkeypatch):
    gate = asyncio.Event()

    async def _blocking_fetch(_token):
        await gate.wait()
        return api.InventoryMetadata([], {}, {}, {}, {}, [])

    monkeypatch.setattr(api, "_fetch_inventory_metadata", _blocking_fetch)
    task = asyncio.ensure_future(api.get_inventory_metadata("tok"))
    await asyncio.sleep(0.02)
    assert api._metadata_tasks, "a metadata task should be inflight"
    await api.close_shared_client()
    assert not api._metadata_tasks
    assert not api._metadata_inflight
    assert not api._metadata_cache
    with pytest.raises((asyncio.CancelledError, api.APIError)):
        await task


@pytest.mark.asyncio
async def test_returned_snapshot_is_defensively_copied(monkeypatch):
    _install_counting_getters(monkeypatch)
    first = await api.get_inventory_metadata("tok")
    first.units.append({"name": "poisoned"})
    first.locations["items"].append({"id": "poison"})
    second = await api.get_inventory_metadata("tok")
    # A caller mutating what it read cannot corrupt the stored snapshot.
    assert second.units == [{"name": "each"}]
    assert second.locations == {"items": [{"id": "loc1"}]}


@pytest.mark.asyncio
async def test_key_is_token_hash_not_a_claim(monkeypatch):
    _install_counting_getters(monkeypatch)
    key = api._metadata_key("some-access-token")
    assert key == hashlib.sha256(b"some-access-token").digest()
    assert isinstance(key, bytes) and len(key) == 32
    assert api._metadata_key("token-a") != api._metadata_key("token-b")
