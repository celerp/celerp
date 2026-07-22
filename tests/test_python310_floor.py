# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Guard the declared Python floor (pyproject ``requires-python >= 3.10``).

A few names the codebase uses only exist from 3.11: ``datetime.UTC`` and
``enum.StrEnum``. Importing either on 3.10 fails at import time, so a single
such line in shipped code silently breaks the whole package there - and the
3.10 CI leg runs a narrow, ``--noconftest`` subset that never imports most of
it. This test scans the shipped packages by AST so any reintroduction fails
here, on every interpreter, instead of only for a user on 3.10.

Sanctioned fallbacks live in one place each: ``enum.StrEnum`` behind the version
gate in ``celerp/compat.py``, and ``tomllib`` behind the ``ImportError`` fallback
in ``celerp/config.py``. Both are exempt below.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from celerp.compat import StrEnum

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIPPED_ROOTS = [_REPO_ROOT / "celerp", _REPO_ROOT / "default_modules"]
_EXEMPT = {_REPO_ROOT / "celerp" / "compat.py"}


def _shipped_py_files() -> list[Path]:
    files: list[Path] = []
    for root in _SHIPPED_ROOTS:
        files.extend(p for p in root.rglob("*.py") if p not in _EXEMPT)
    return files


def _from_imports(tree: ast.AST) -> list[tuple[str, str]]:
    """Every (module, imported_name) pair from ``from X import Y`` statements."""
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            pairs.extend((node.module, alias.name) for alias in node.names)
    return pairs


@pytest.mark.parametrize("path", _shipped_py_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_no_python311_only_imports(path: Path):
    """No shipped module imports a 3.11-only stdlib name outside its sanctioned
    single-source fallback."""
    tree = ast.parse(path.read_text())
    imports = _from_imports(tree)
    assert ("enum", "StrEnum") not in imports, (
        f"{path.relative_to(_REPO_ROOT)} imports enum.StrEnum (3.11+); "
        "import StrEnum from celerp.compat instead")
    assert ("datetime", "UTC") not in imports, (
        f"{path.relative_to(_REPO_ROOT)} imports datetime.UTC (3.11+); "
        "use datetime.timezone.utc instead")
    src = path.read_text()
    assert "datetime.UTC" not in src, (
        f"{path.relative_to(_REPO_ROOT)} uses datetime.UTC (3.11+); "
        "use datetime.timezone.utc instead")


def test_compat_strenum_stringifies_to_value():
    """The 3.10 backport must behave like enum.StrEnum: members are real str and
    stringify to their value, which serialization and comparisons depend on."""
    class Color(StrEnum):
        RED = "red"

    assert isinstance(Color.RED, str)
    assert Color.RED == "red"
    assert str(Color.RED) == "red"
    assert f"{Color.RED}" == "red"
