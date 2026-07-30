# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for ui.marketplace_catalog - the catalog is untrusted input."""
from __future__ import annotations

import json

import pytest

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


class TestCommunityDownload:
    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
        return tmp_path

    @pytest.mark.asyncio
    async def test_download_rejects_bad_id(self):
        with pytest.raises(ValueError):
            await mc.download_community_archive("https://github.com/a/b", "bad id!")

    @pytest.mark.asyncio
    async def test_download_rejects_non_https_repo(self):
        with pytest.raises(ValueError):
            await mc.download_community_archive("http://github.com/a/b", "ok")

    def test_read_staged_rejects_path_outside_staging(self, _data_dir):
        outside = _data_dir / "secret.zip"
        outside.write_bytes(b"nope")
        with pytest.raises(ValueError):
            mc.read_staged_archive(str(outside))

    def test_read_staged_reads_inside_staging(self, _data_dir):
        staged = mc._staging_dir() / "ok.zip"
        staged.write_bytes(b"payload")
        assert mc.read_staged_archive(str(staged)) == b"payload"
