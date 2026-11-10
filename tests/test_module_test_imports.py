# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A module's own tests run on their own: ``pytest default_modules/<module>/tests`` puts
only the repository root and the test file's own directory on the import path, so a module
test that imports a file from ``tests/`` collects only when something under ``tests/`` was
collected first. Helpers shared across both trees live at the repository root."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imported(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_module_tests_import_nothing_from_the_core_test_tree():
    found = []
    for path in sorted(ROOT.glob("default_modules/*/tests/**/*.py")):
        for name in _imported(path):
            if ((ROOT / "tests" / f"{name}.py").exists() and not (path.parent / f"{name}.py").exists()
                    and not (ROOT / f"{name}.py").exists()):
                found.append(f"{path.relative_to(ROOT)} imports {name} from tests/")
    assert not found, found
