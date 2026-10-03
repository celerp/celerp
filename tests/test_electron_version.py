# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The version a desktop build installs (scripts/electron_version.py)."""
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "electron_version.py"
_spec = importlib.util.spec_from_file_location("electron_version", _SCRIPT)
electron_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(electron_version)


@pytest.mark.parametrize("tag, version", [("v2.5.4", "2.5.4"), ("v10.0.12", "10.0.12")])
def test_a_release_tag_installs_exactly_its_version(tag, version):
    assert electron_version.release_version(tag) == version


@pytest.mark.parametrize("tag", [
    "v2.5.4-rc1", "v2.5.4-beta.1", "v2.5.4+build", "v2.5", "v2.5.4.1", "2.5.4", "v2.5.x", "v2.5.4 ",
])
def test_a_release_tag_that_is_not_x_y_z_stops_the_build(tag):
    with pytest.raises(ValueError, match="is not vX.Y.Z"):
        electron_version.release_version(tag)


def test_a_development_build_installs_the_next_patch_with_a_dev_suffix():
    assert electron_version.dev_version("v2.5.3-14-gabc1234", "900", "abc1234") == "2.5.4-dev.14+gabc1234"
    assert electron_version.dev_version("", "900", "abc1234") == "0.0.1-dev.900+gabc1234"


def _run(tmp_path, monkeypatch, capsys, ref, ref_name):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"name": "celerp", "version": "1.0.0"}))
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setenv("GITHUB_REF_NAME", ref_name)
    code = electron_version.main(pkg)
    return code, capsys.readouterr(), json.loads(pkg.read_text())["version"]


def test_the_release_build_fails_on_a_suffixed_tag_and_leaves_the_version(tmp_path, monkeypatch, capsys):
    code, out, version = _run(tmp_path, monkeypatch, capsys, "refs/tags/v2.5.4-rc1", "v2.5.4-rc1")
    assert code == 1
    assert "::error::release tag 'v2.5.4-rc1' is not vX.Y.Z" in out.err
    assert version == "1.0.0"


def test_the_release_build_stamps_a_plain_tag(tmp_path, monkeypatch, capsys):
    code, out, version = _run(tmp_path, monkeypatch, capsys, "refs/tags/v2.5.4", "v2.5.4")
    assert code == 0
    assert version == "2.5.4"
