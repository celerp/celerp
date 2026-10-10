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
import os
import shutil
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
    would fail-close every pip install fleet-wide and no source-tree test catches.

    The build writes build/ and celerp.egg-info/ into the source tree; the test
    removes the ones it created, because a leftover egg-info replaces the installed
    package metadata for every later import in this checkout."""
    artifacts = [REPO_ROOT / "build", REPO_ROOT / "celerp.egg-info"]
    created = [p for p in artifacts if not p.exists()]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "--no-build-isolation", "-w", str(tmp_path), str(REPO_ROOT)],
            capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        pytest.skip("pip unavailable")
    finally:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
    if proc.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment: {proc.stderr[-500:]}")
    wheels = list(tmp_path.glob("celerp-*.whl"))
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert any(n.endswith("default_modules/first_party.lock.json") for n in names), (
        f"lock missing from wheel; sample: {names[:20]}")
    tracked = subprocess.run(["git", "ls-files", "ui/static"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, check=True).stdout.split()
    assert tracked and not set(tracked) - set(names), f"static files missing from wheel: {sorted(set(tracked) - set(names))}"


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


# ── Packaged default_modules check ───────────────────────────────────────────

CHECKER = REPO_ROOT / "scripts" / "check_packaged_modules.py"


@pytest.fixture
def packaged_tree(tmp_path):
    """A copy of the shipped default_modules folder, laid out as the build packs it."""
    tree = tmp_path / "app" / "default_modules"
    shutil.copytree(DEFAULT_MODULES_DIR, tree,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tree


def _check(tree, lock=LOCK_PATH):
    from celerp.modules.loader import check_module_tree
    return check_module_tree(tree, lock)


def test_exact_packaged_module_tree_passes(packaged_tree):
    assert _check(packaged_tree) == []
    assert _check(packaged_tree, None) == []


def test_packaged_tree_missing_a_locked_module_fails(packaged_tree):
    name = sorted(json.loads(LOCK_PATH.read_text()))[-1]
    shutil.rmtree(packaged_tree / name)

    assert _check(packaged_tree) == [f"{name}: missing"]


def test_packaged_tree_with_a_modified_locked_module_fails(packaged_tree):
    name = sorted(json.loads(LOCK_PATH.read_text()))[0]
    with (packaged_tree / name / "__init__.py").open("a") as fh:
        fh.write("\n# changed\n")

    assert _check(packaged_tree) == [f"{name}: content does not match the lock"]


def test_packaged_tree_with_an_extra_module_fails(packaged_tree):
    (packaged_tree / "acme-extra").mkdir()
    (packaged_tree / "acme-extra" / "__init__.py").write_text("PLUGIN_MANIFEST = {}\n")

    assert _check(packaged_tree) == ["acme-extra: not in the lock"]


@pytest.mark.parametrize("where", ["inside_tree", "expected"])
def test_stale_or_wrong_lock_fails(where, packaged_tree, tmp_path):
    lock = json.loads(LOCK_PATH.read_text())
    name = sorted(lock)[0]
    stale = {**lock, name: "0" * 64}
    if where == "inside_tree":
        (packaged_tree / "first_party.lock.json").write_text(json.dumps(stale))
        problems = _check(packaged_tree)
        assert problems == ["the lock inside the tree differs from the expected lock"]
        assert _check(packaged_tree, None) == [f"{name}: content does not match the lock"]
    else:
        wrong = tmp_path / "wrong.lock.json"
        wrong.write_text(json.dumps(stale))
        assert _check(packaged_tree, wrong) == [
            "the lock inside the tree differs from the expected lock",
            f"{name}: content does not match the lock"]


@pytest.mark.parametrize("lock", ["missing", "empty", "malformed"])
def test_packaged_tree_without_a_usable_lock_fails(lock, packaged_tree):
    path = packaged_tree / "first_party.lock.json"
    if lock == "missing":
        path.unlink()
    else:
        path.write_text("{}" if lock == "empty" else "[1, 2]")

    assert _check(packaged_tree, None) == [f"no usable lock at {path}"]


def test_packaged_module_checker_script_exit_codes(packaged_tree):
    """The script the build runs against each packaged artifact: 0 on an exact
    tree, 1 naming the module otherwise."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    run = [sys.executable, str(CHECKER), str(packaged_tree), "--lock", str(LOCK_PATH)]

    ok = subprocess.run(run, capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    name = sorted(json.loads(LOCK_PATH.read_text()))[0]
    shutil.rmtree(packaged_tree / name)
    bad = subprocess.run(run, capture_output=True, text=True, env=env)
    assert bad.returncode == 1
    assert f"{name}: missing" in bad.stdout
