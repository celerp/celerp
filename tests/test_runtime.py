# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which release `celerp` runs, and the environment a release runs under."""

from __future__ import annotations

import os

import pytest

from celerp import runtime


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(runtime.metadata, "version", lambda name: "1.0.0")
    return tmp_path


def test_no_pointer_runs_the_base(cfg_dir):
    assert runtime.pointed() is None
    assert runtime.active() is None


def test_pointer_to_a_newer_release_runs_it(cfg_dir):
    runtime.release_dir("1.1.0").mkdir(parents=True)
    runtime.switch("1.1.0")
    assert runtime.active() == runtime.release_dir("1.1.0")


@pytest.mark.parametrize("version", ["1.0.0", "0.9.0", "garbage"])
def test_pointer_not_newer_than_the_base_is_ignored(cfg_dir, version):
    # A pip upgrade of the base past a self-update wins over the older release.
    runtime.release_dir(version).mkdir(parents=True)
    runtime.switch(version)
    assert runtime.active() is None


def test_pointer_to_a_missing_release_is_ignored(cfg_dir):
    runtime.switch("1.1.0")
    assert runtime.active() is None


def test_discard_keeps_the_release_in_use(cfg_dir):
    for v in ("1.1.0", "1.2.0"):
        runtime.release_dir(v).mkdir(parents=True)
        runtime.staging_dir(v).mkdir(parents=True)
    runtime.switch("1.1.0")
    runtime.discard("1.1.0")
    runtime.discard("1.2.0")
    assert runtime.release_dir("1.1.0").is_dir()
    assert not runtime.staging_dir("1.1.0").exists()
    assert not runtime.release_dir("1.2.0").exists()


def test_prune_never_removes_the_pointed_release(cfg_dir):
    for v in ("1.0.5", "1.1.0", "1.2.0"):
        runtime.release_dir(v).mkdir(parents=True)
    runtime.switch("1.1.0")
    runtime.prune({"1.2.0"})
    assert sorted(p.name for p in runtime.release_dir("x").parent.iterdir()) == [
        "1.1.0", "1.2.0", "current"]


def test_release_env_puts_the_release_first_and_base_env_removes_it(tmp_path):
    root = tmp_path / "rel"
    env = runtime.release_env(root, {"PYTHONPATH": "/site/extra", "X": "1"}, ["/mods/a"])
    assert env["PYTHONPATH"].split(os.pathsep) == [str(root), "/mods/a", "/site/extra"]
    assert env[runtime.PKG_ROOT_ENV] == str(root)
    base = runtime.base_env(env)
    assert runtime.PKG_ROOT_ENV not in base
    assert base["PYTHONPATH"].split(os.pathsep) == ["/mods/a", "/site/extra"]
    assert base["X"] == "1"


def test_release_env_for_another_release_drops_the_running_one(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    env = runtime.release_env(new, runtime.release_env(old, {}))
    assert env["PYTHONPATH"] == str(new)
    assert "PYTHONPATH" not in runtime.base_env(runtime.release_env(old, {}))
