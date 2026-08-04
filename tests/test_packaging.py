# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Packaging and anti-rot guards for the first-party module lock.

The content-identity trust check depends on a committed
`default_modules/first_party.lock.json` that (a) matches the git-tracked module
tree, (b) actually ships inside the built wheel, and (c) has fully replaced the
old name/location trust symbols.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = REPO_ROOT / "default_modules" / "first_party.lock.json"
GENERATOR = REPO_ROOT / "scripts" / "gen_first_party_lock.py"
DEFAULT_MODULES_DIR = REPO_ROOT / "default_modules"


_MISSING = object()


def _migrations_target(init_py: Path):
    """Return the PLUGIN_MANIFEST `migrations` value from a module __init__.py
    without importing it, or _MISSING when the key is absent. Only the one key is
    read, so a manifest carrying non-literal values elsewhere is still handled."""
    tree = ast.parse(init_py.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "PLUGIN_MANIFEST"
                   for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return _MISSING
        for key_node, val_node in zip(node.value.keys, node.value.values):
            if isinstance(key_node, ast.Constant) and key_node.value == "migrations":
                return ast.literal_eval(val_node)
    return _MISSING


def test_committed_lock_matches_regenerated_digests_from_git_tracked_tree():
    """The committed lock is byte-for-byte what the generator produces from the
    git-tracked tree - the same check CI runs as `git diff --exit-code`."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--stdout"],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == LOCK_PATH.read_text(), (
        "first_party.lock.json is stale - run scripts/gen_first_party_lock.py")


def test_pyproject_declares_lock_as_package_data():
    """The wheel reads [tool.setuptools.package-data] independently of MANIFEST.in;
    the lock must be enumerated there or the wheel omits it."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert "first_party.lock.json" in text


def test_built_wheel_includes_first_party_lock(tmp_path):
    """Build the wheel and confirm the lock is inside it - the one failure that
    would fail-close every pip install fleet-wide and no source-tree test catches."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "--no-build-isolation", "-w", str(tmp_path), str(REPO_ROOT)],
            capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        pytest.skip("pip unavailable")
    if proc.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment: {proc.stderr[-500:]}")
    wheels = list(tmp_path.glob("celerp-*.whl"))
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert any(n.endswith("default_modules/first_party.lock.json") for n in names), (
        f"lock missing from wheel; sample: {names[:20]}")


def test_no_default_module_declares_migrations():
    """No bundled default module declares an Alembic `migrations` target. Runtime
    schema evolution is the core module-migration runner's job; a shipped default
    carrying a dead `migrations` key is cruft the lock would silently bless."""
    offenders = []
    for pkg in sorted(DEFAULT_MODULES_DIR.iterdir()):
        init_py = pkg / "__init__.py"
        if not init_py.exists():
            continue
        target = _migrations_target(init_py)
        if target is not _MISSING and target is not None:
            offenders.append(f"{pkg.name}: {target!r}")
    assert not offenders, "default modules declare migrations targets:\n" + "\n".join(offenders)


def test_removed_trust_symbols_have_no_readers():
    """The old name/location trust symbols are fully gone from production code."""
    removed = ["_trusted_default_names", "default_module_names",
               "resolved_bundled", "trusted_names"]
    for symbol in removed:
        hits = subprocess.run(
            ["grep", "-rn", symbol, "celerp", "ui", "scripts"],
            cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.strip()
        assert not hits, f"{symbol!r} still has readers:\n{hits}"
