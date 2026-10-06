# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for ui.marketplace_catalog - the catalog is untrusted input."""
from __future__ import annotations

import json

import pytest

from celerp.services import staged_downloads
from ui import marketplace_catalog as mc

GOOD = {
    "id": "my-module", "name": "My Module", "description": "Does things.",
    "tier": "community", "repo": "https://github.com/a/b",
    "author": "A", "license": "MIT",
    "data_access": "Its own tables.", "network_calls": "None.",
}


def _doc(*entries):
    return json.dumps({"schema_version": 1, "modules": list(entries)}).encode()


class TestParse:
    def test_valid_entry_passes(self):
        mods = mc._parse(_doc(GOOD))
        assert len(mods) == 1 and mods[0]["id"] == "my-module"
        assert mods[0]["repo"] == "https://github.com/a/b"

    def test_bad_tier_dropped(self):
        assert mc._parse(_doc({**GOOD, "tier": "platinum"})) == []

    def test_missing_required_field_dropped(self):
        bad = dict(GOOD)
        del bad["license"]
        assert mc._parse(_doc(bad)) == []

    def test_bad_id_chars_dropped(self):
        assert mc._parse(_doc({**GOOD, "id": "my module!"})) == []

    def test_non_https_url_field_stripped(self):
        mods = mc._parse(_doc({**GOOD, "tier": "official",
                               "homepage": "javascript:alert(1)"}))
        assert len(mods) == 1 and "homepage" not in mods[0]

    def test_pinned_commit_kept(self):
        pin = "0123456789abcdef0123456789abcdef01234567"
        assert mc._parse(_doc({**GOOD, "commit": pin}))[0]["commit"] == pin

    def test_strings_length_capped(self):
        mods = mc._parse(_doc({**GOOD, "description": "x" * 5000}))
        assert len(mods[0]["description"]) == 300

    def test_unsupported_schema_version_rejected(self):
        with pytest.raises(ValueError):
            mc._parse(json.dumps({"schema_version": 2, "modules": []}).encode())

    def test_oversized_module_list_rejected(self):
        with pytest.raises(ValueError):
            mc._parse(_doc(*[{**GOOD, "id": f"m{i}"} for i in range(501)]))

    def test_not_json_raises(self):
        with pytest.raises(Exception):
            mc._parse(b"<html>not a catalog</html>")

    def test_price_must_be_number(self):
        mods = mc._parse(_doc({**GOOD, "tier": "official",
                               "price_monthly": "9; DROP TABLE"}))
        assert "price_monthly" not in mods[0]

    def test_bool_price_rejected(self):
        # bool is a subclass of int; "price_monthly": true must not become $1/mo.
        mods = mc._parse(_doc({**GOOD, "tier": "official", "price_monthly": True}))
        assert "price_monthly" not in mods[0]

    def test_depends_on_passes_valid_ids_only(self):
        mods = mc._parse(_doc({**GOOD, "depends_on":
                               ["celerp-accounting", "bad id!", 7, "  ", "ok_2"]}))
        assert mods[0]["depends_on"] == ["celerp-accounting", "ok_2"]

    def test_depends_on_absent_or_empty_drops_field(self):
        assert "depends_on" not in mc._parse(_doc(GOOD))[0]
        assert "depends_on" not in mc._parse(_doc({**GOOD, "depends_on": []}))[0]
        assert "depends_on" not in mc._parse(_doc({**GOOD, "depends_on": "not-a-list"}))[0]


class TestLocalState:
    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
        return tmp_path

    def test_read_cached_none_when_absent(self):
        assert mc.read_cached() is None

    def test_read_cached_garbage_is_none(self, _data_dir):
        (_data_dir / "marketplace-catalog.json").write_text("{broken")
        assert mc.read_cached() is None

    def test_cache_entries_revalidated_on_read(self, _data_dir):
        (_data_dir / "marketplace-catalog.json").write_text(json.dumps(
            {"fetched_at": 1, "modules": [GOOD, {**GOOD, "id": "x", "tier": "nope"}]}
        ))
        cached = mc.read_cached()
        assert [m["id"] for m in cached] == ["my-module"]

    def test_community_ack_round_trip(self):
        assert mc.community_acked() is False
        mc.set_community_ack()
        assert mc.community_acked() is True


PIN = "0123456789abcdef0123456789abcdef01234567"
ZIP = b"PK\x05\x06" + b"\x00" * 18


def _host(seen: list[str], respond=None):
    """Stands in for the network: records every URL asked for and answers with
    ``respond(request)`` (a small zip by default)."""
    import httpx
    from unittest.mock import patch

    real = httpx.AsyncClient

    def handler(request):
        seen.append(str(request.url))
        return respond(request) if respond else httpx.Response(200, content=ZIP)

    return patch("ui.marketplace_catalog.httpx.AsyncClient",
                 new=lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


class TestCommunityDownload:
    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
        return tmp_path

    @pytest.mark.asyncio
    async def test_download_rejects_bad_id(self):
        with pytest.raises(ValueError):
            await mc.download_community_archive("https://github.com/a/b", PIN, "bad id!")

    @pytest.mark.asyncio
    async def test_download_fetches_the_pinned_commit_from_codeload(self):
        seen: list[str] = []
        with _host(seen):
            token = await mc.download_community_archive("https://github.com/a/b", PIN, "ok")
        assert seen == [f"https://codeload.github.com/a/b/zip/{PIN}"]
        assert mc.read_staged_archive("ok", token) == ZIP

    @pytest.mark.asyncio
    @pytest.mark.parametrize("repo", [
        "http://github.com/a/b",
        "https://gitlab.com/a/b",
        "https://github.com.example.net/a/b",
        "https://evilgithub.com/a/b",
        "https://user@github.com/a/b",
        "https://github.com:8443/a/b",
        "https://github.com/a/b/tree/main",
        "https://github.com/a/b/",
        "https://github.com/a",
        "https://github.com/a/..",
        "https://github.com/a/b?x=1",
        "https://github.com/a/b.git",
        "https://github.com/a/b.GIT",
        "https://github.com/a/b.Git",
    ], ids=["http", "other-host", "lookalike-suffix", "lookalike-prefix", "userinfo",
            "port", "extra-path", "trailing-slash", "no-repo", "dot-dot", "query", "dot-git",
            "dot-git-upper", "dot-git-mixed"])
    async def test_download_refuses_a_repo_that_is_not_a_canonical_github_repo(self, repo):
        seen: list[str] = []
        with _host(seen), pytest.raises(mc.DownloadRefused) as exc:
            await mc.download_community_archive(repo, PIN, "ok")
        assert exc.value.key == "marketplace.download_not_github"
        assert seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("commit", [None, "", "HEAD", "main", PIN[:12], PIN + "0", "g" * 40,
                                        PIN.upper(), PIN.replace("a", "A")])
    async def test_download_refuses_a_commit_that_is_not_40_hex(self, commit):
        seen: list[str] = []
        with _host(seen), pytest.raises(mc.DownloadRefused) as exc:
            await mc.download_community_archive("https://github.com/a/b", commit, "ok")
        assert exc.value.key == "marketplace.download_unpinned"
        assert seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("location", [
        "https://evil.example/a.zip",
        "https://codeload.github.com.evil.example/a.zip",
        f"https://github.com/a/b/archive/{PIN}.zip",
    ])
    async def test_download_refuses_a_redirect(self, location):
        import httpx
        seen: list[str] = []

        def respond(request):
            if request.url.host == "codeload.github.com":
                return httpx.Response(302, headers={"Location": location})
            return httpx.Response(200, content=ZIP)

        with _host(seen, respond), pytest.raises(mc.DownloadRefused) as exc:
            await mc.download_community_archive("https://github.com/a/b", PIN, "ok")
        assert exc.value.key == "marketplace.download_redirected"
        assert seen == [f"https://codeload.github.com/a/b/zip/{PIN}"]
        assert list(mc._staging_dir().iterdir()) == []

    def test_the_longest_listing_id_can_be_downloaded(self):
        """Download names hold a listing id (up to 64 characters) and its commit;
        anything longer is refused."""
        longest = "m" * mc._STR_LIMITS["id"] + "-" + PIN
        assert staged_downloads.valid_owner(longest)
        assert not staged_downloads.valid_owner("m" * 129)

    @pytest.mark.asyncio
    async def test_a_second_download_does_not_change_what_the_first_imports(self):
        import httpx
        bodies = iter([b"first", b"second"])
        with _host([], lambda r: httpx.Response(200, content=next(bodies))):
            first = await mc.download_community_archive("https://github.com/a/b", PIN, "ok")
            second = await mc.download_community_archive("https://github.com/a/b", PIN, "ok")
        assert first != second
        assert mc.read_staged_archive("ok", first) == b"first"
        assert mc.read_staged_archive("ok", second) == b"second"

    @pytest.mark.asyncio
    async def test_concurrent_downloads_do_not_collide(self):
        import asyncio

        import httpx

        def respond(request):
            return httpx.Response(200, content=request.headers["x-n"].encode())

        real = httpx.AsyncClient
        counter = iter(range(100))

        def client(**kw):
            n = str(next(counter))
            return real(transport=httpx.MockTransport(respond), headers={"x-n": n}, **kw)

        from unittest.mock import patch
        with patch("ui.marketplace_catalog.httpx.AsyncClient", new=client):
            tokens = await asyncio.gather(*(
                mc.download_community_archive("https://github.com/a/b", PIN, "ok")
                for _ in range(8)))
        assert len(set(tokens)) == 8
        assert sorted(mc.read_staged_archive("ok", t) for t in tokens) == sorted(
            str(n).encode() for n in range(8))

    @pytest.mark.parametrize("token", [
        "", "ok", "../secret", f"ok-{PIN}-" + "0" * 32, f"other-{PIN}-" + "0" * 32,
        f"ok-{PIN}-" + "0" * 31 + "/",
    ], ids=["empty", "bare-id", "traversal", "unknown", "other-module", "bad-shape"])
    def test_import_refuses_an_unknown_token(self, token, _data_dir):
        (_data_dir / "secret.zip").write_bytes(b"nope")
        with pytest.raises(mc.DownloadRefused) as exc:
            mc.read_staged_archive("ok", token)
        assert exc.value.key == "marketplace.import_expired"

    @pytest.mark.asyncio
    async def test_import_refuses_an_expired_token(self):
        import os
        with _host([]):
            token = await mc.download_community_archive("https://github.com/a/b", PIN, "ok")
        staged = mc._staging_dir() / f"{token}.zip"
        old = staged.stat().st_mtime - staged_downloads.TTL_SECONDS - 1
        os.utime(staged, (old, old))
        with pytest.raises(mc.DownloadRefused) as exc:
            mc.read_staged_archive("ok", token)
        assert exc.value.key == "marketplace.import_expired"


# ── one GitHub parser for listing, archive and source link ───────────────────

_NOT_CANONICAL = [
    "https://gitlab.com/a/b", "https://github.com.example.net/a/b", "https://evilgithub.com/a/b",
    "https://user@github.com/a/b", "https://github.com:8443/a/b", "https://github.com/a/b/tree/main",
    "https://github.com/a/b/", "https://github.com/a", "https://github.com/a/..",
    "https://github.com/a/b?x=1", "https://github.com/a/b.git",
]


@pytest.mark.parametrize("repo", _NOT_CANONICAL)
def test_catalog_keeps_no_repo_that_is_not_a_canonical_github_repo(repo):
    mods = mc._parse(_doc({**GOOD, "repo": repo, "commit": PIN}))
    assert len(mods) == 1 and "repo" not in mods[0]
    assert mc.source_url(mods[0]) is None


@pytest.mark.parametrize("repo", ["https://github.com/a/b", "https://github.com/acme-co/mod.v2", *_NOT_CANONICAL])
def test_listing_archive_and_source_agree_on_the_repository(repo):
    """The repository a listing keeps is the one its archive is fetched from and the
    one its source link opens, at the same commit."""
    kept = mc._parse(_doc({**GOOD, "repo": repo, "commit": PIN}))[0]
    try:
        archive = mc._archive_url(repo, PIN)
    except mc.DownloadRefused:
        archive = None
    source = mc.source_url({"repo": repo, "commit": PIN})
    assert ("repo" in kept) == (archive is not None) == (source is not None)
    if archive:
        owner_repo = repo.removeprefix("https://github.com/")
        assert archive == f"https://codeload.github.com/{owner_repo}/zip/{PIN}"
        assert source == f"https://github.com/{owner_repo}/tree/{PIN}"


def test_source_link_needs_the_pinned_commit():
    assert mc.source_url({"repo": "https://github.com/a/b"}) is None
    assert mc.source_url({"repo": "https://github.com/a/b", "commit": "main"}) is None
