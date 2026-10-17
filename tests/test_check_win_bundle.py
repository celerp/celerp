# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""An installed Windows folder must hold exactly the build's files, byte for
byte, and the uninstaller the installer writes: the Windows smokes read every install
through check_win_bundle.py installed."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "electron" / "scripts" / "check_win_bundle.py"
_spec = importlib.util.spec_from_file_location("check_win_bundle", _SCRIPT)
_bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bundle)


def _tree(root: Path, names: list[str]) -> Path:
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(name, "utf-8")
    return root


_BUILD = ["Celerp.exe", "resources/app.asar", "resources/python-x64/python/python.exe"]


def test_an_install_holding_the_build_and_its_uninstaller_passes(tmp_path, capsys):
    build = _tree(tmp_path / "build", _BUILD)
    folder = _tree(tmp_path / "installed", _BUILD + ["Uninstall Celerp.exe"])

    assert _bundle.installed(build, folder) == 0
    assert "the build's 3 files, byte for byte (SHA-256), and the uninstaller" in capsys.readouterr().out


@pytest.mark.parametrize("names, shown", [
    (_BUILD + ["resources/app/celerp/__pycache__/main.cpython-312.pyc"], "extra: resources/app/celerp/__pycache__/main.cpython-312.pyc"),
    (_BUILD[:-1], "missing: resources/python-x64/python/python.exe"),
])
def test_an_install_with_files_left_over_or_missing_fails(tmp_path, capsys, names, shown):
    build = _tree(tmp_path / "build", _BUILD)
    folder = _tree(tmp_path / "installed", names)

    assert _bundle.installed(build, folder) == 1
    assert shown in capsys.readouterr().out


def test_an_installed_file_whose_bytes_differ_from_the_build_fails(tmp_path, capsys):
    # A file of the same name and size but other bytes: a partial overwrite, or a
    # file kept from an earlier version.
    build = _tree(tmp_path / "build", _BUILD)
    folder = _tree(tmp_path / "installed", _BUILD + ["Uninstall Celerp.exe"])
    (folder / "resources/app.asar").write_text("resources/app.asaX", "utf-8")

    assert _bundle.installed(build, folder) == 1
    out = capsys.readouterr().out
    assert "differs: resources/app.asar" in out
    assert "1 differ" in out
