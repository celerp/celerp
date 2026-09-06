# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the packaged-config atomic writer.

merge_packaged_config replaces several call sites that used to merge one key
at a time (settings_cloud.py's _save_infra_packaged and _restore_db_packaged),
which could leave the file with only some of a related group of keys applied
if a crash landed between two separate single-key writes. These tests cover
the multi-key case directly, plus the no-op and failure-preserves-prior
behavior the single-key writer already had.
"""

from __future__ import annotations

import json
import os
import stat

from celerp import config_store


def test_merge_multiple_keys_atomically(tmp_path, monkeypatch):
    """A single call merging several keys lands all of them together."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"db_mode": "local"}))

    ok = config_store.merge_packaged_config({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://u:p@h:5432/db",
        "external_db_url_backup": "postgresql+asyncpg://u:old@h:5432/db",
    })

    assert ok is True
    persisted = json.loads(config_path.read_text())
    assert persisted["db_mode"] == "external"
    assert persisted["external_db_url"] == "postgresql+asyncpg://u:p@h:5432/db"
    assert persisted["external_db_url_backup"] == "postgresql+asyncpg://u:old@h:5432/db"


def test_merge_no_data_dir_is_noop():
    """With no CELERP_DATA_DIR (dev/server mode), the merge is a no-op that
    reports failure rather than writing anywhere."""
    os.environ.pop("CELERP_DATA_DIR", None)
    assert config_store.merge_packaged_config({"db_mode": "external"}) is False


def test_merge_preserves_0600(tmp_path, monkeypatch):
    """A multi-key merge leaves the config file at mode 0600."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"a": 1}))
    config_path.chmod(0o600)

    config_store.merge_packaged_config({"b": 2, "c": 3})

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"config mode broadened to {oct(mode)}"


def test_merge_failure_preserves_prior_file(tmp_path, monkeypatch):
    """A failure partway through a multi-key merge leaves the prior config
    intact rather than partially applied or truncated."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    prior = {"db_mode": "local", "external_db_url": ""}
    config_path.write_text(json.dumps(prior))

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(config_store.json, "dump", _boom)

    ok = config_store.merge_packaged_config({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://u:p@h:5432/db",
    })

    assert ok is False
    reread = json.loads(config_path.read_text())
    assert reread == prior


def test_merge_empty_updates_is_noop_success(tmp_path, monkeypatch):
    """Merging an empty dict is a successful no-op: nothing to apply, nothing
    to fail on."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    assert config_store.merge_packaged_config({}) is True


def test_read_packaged_config_missing_returns_empty(tmp_path, monkeypatch):
    """Reading with no config file present degrades to {} rather than raising."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    assert config_store.read_packaged_config() == {}


def test_read_packaged_config_roundtrip(tmp_path, monkeypatch):
    """A value written by merge_packaged_config is visible to
    read_packaged_config."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_store.merge_packaged_config({"db_mode": "external"})
    assert config_store.read_packaged_config()["db_mode"] == "external"
