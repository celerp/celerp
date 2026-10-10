# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The client reads only the v2 catalog: pinned Community code, never the v1 feed.

v1 (relay /marketplace/catalog, raw index.json) is the frozen feed for older clients.
This client asks only the v2 sources, accepts only a schema_version 2 document, keeps
its own v2 cache, lists a Community entry only with the exact commit it pins, and
downloads exactly that commit. With both v2 sources down it shows the cached v2
catalog or nothing, never v1.
"""
from __future__ import annotations

import json

import httpx
import pytest

from test_marketplace_catalog import GOOD, PIN, ZIP, _host
from ui import marketplace_catalog as mc
from ui.config import RELAY_URL

V2_SOURCES = [f"{RELAY_URL}/marketplace/catalog/v2",
              "https://raw.githubusercontent.com/celerp/community-modules/main/index-v2.json"]
V1_CACHE = "marketplace-catalog.json"


def _doc(version: int, *entries) -> bytes:
    return json.dumps({"schema_version": version, "modules": list(entries)}).encode()


def _serving(body: bytes | None):
    """Every catalog source answers with *body*, or fails when it is None."""
    seen: list[str] = []
    return seen, _host(seen, lambda _req: httpx.Response(500) if body is None else httpx.Response(200, content=body))


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


async def test_the_client_asks_only_the_v2_sources():
    seen, net = _serving(None)
    with net, pytest.raises(ConnectionError):
        await mc.fetch_catalog()
    assert seen == V2_SOURCES


async def test_a_v1_document_is_rejected_and_shows_unavailable(_data_dir):
    with pytest.raises(ValueError):
        mc._parse(_doc(1, GOOD))
    seen, net = _serving(_doc(1, GOOD))
    with net, pytest.raises(ConnectionError):
        await mc.fetch_catalog()
    assert seen == V2_SOURCES
    assert list(_data_dir.iterdir()) == []


async def test_the_v2_cache_is_its_own_and_is_never_filled_from_or_read_as_v1(_data_dir):
    (_data_dir / V1_CACHE).write_text(json.dumps({"fetched_at": 1, "modules": [GOOD]}))
    seen, net = _serving(None)
    with net, pytest.raises(ConnectionError):
        await mc.fetch_catalog()

    seen, net = _serving(_doc(2, GOOD))
    with net:
        assert [m["id"] for m in (await mc.fetch_catalog())[0]] == ["my-module"]
    assert json.loads((_data_dir / V1_CACHE).read_text())["fetched_at"] == 1
    assert mc._cache_path() != _data_dir / V1_CACHE


async def test_both_v2_sources_down_shows_the_cached_v2_catalog(_data_dir):
    seen, net = _serving(_doc(2, GOOD))
    with net:
        await mc.fetch_catalog()
    seen, net = _serving(None)
    with net:
        modules, from_cache = await mc.fetch_catalog()
    assert from_cache is True and [m["id"] for m in modules] == ["my-module"]
    assert seen == V2_SOURCES


@pytest.mark.parametrize("commit", [None, "", "main", "HEAD", PIN[:7], PIN.upper(), PIN + "0", 7])
def test_a_community_entry_without_its_exact_commit_is_not_listed(commit):
    entry = {k: v for k, v in GOOD.items() if k != "commit"}
    if commit is not None:
        entry["commit"] = commit
    assert mc._parse(_doc(2, entry)) == []


def test_a_cached_community_entry_without_its_exact_commit_is_not_listed(_data_dir):
    unpinned = {k: v for k, v in GOOD.items() if k != "commit"}
    mc._cache_path().write_text(json.dumps({"fetched_at": 1, "modules": [unpinned, GOOD]}))
    assert [m.get("commit") for m in mc.read_cached()] == [PIN]


def test_an_official_entry_needs_no_commit():
    official = {**{k: v for k, v in GOOD.items() if k != "commit"}, "tier": "official"}
    assert [m["id"] for m in mc._parse(_doc(2, official))] == ["my-module"]


async def test_the_listed_commit_is_the_one_downloaded_from_codeload():
    seen, net = _serving(_doc(2, GOOD))
    with net:
        listed = (await mc.fetch_catalog())[0][0]
    archives: list[str] = []
    with _host(archives, lambda _req: httpx.Response(200, content=ZIP)):
        token = await mc.download_community_archive(listed["repo"], listed["commit"], listed["id"])
    assert archives == [f"https://codeload.github.com/a/b/zip/{PIN}"]
    assert mc.read_staged_archive(listed["id"], token) == ZIP
