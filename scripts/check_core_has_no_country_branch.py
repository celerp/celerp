# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fail when a country code drives a condition in core or the documents module.

    python scripts/check_core_has_no_country_branch.py [root]

Country behaviour is country pack data, so core code never tests for a country. A two-letter
upper-case string literal (or a tuple, list or set made only of them) compared in an ``if``,
``while`` or conditional expression under celerp/ or default_modules/celerp-docs/ (tests
excluded) is reported by file and line.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("celerp", "default_modules/celerp-docs")


def _country_literal(node: ast.AST) -> bool:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_country_literal(item) for item in node.elts)
    return (isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) == 2
            and node.value.isascii() and node.value.isalpha() and node.value.isupper())


def _compares_country(test: ast.AST) -> list[int]:
    return [node.lineno for node in ast.walk(test) if isinstance(node, ast.Compare)
            and any(_country_literal(side) for side in (node.left, *node.comparators))]


def branches(root: Path) -> list[tuple[str, int]]:
    """Each (file, line) under *root* where a country literal is compared in a condition."""
    found = []
    for scanned in SCANNED:
        for path in sorted((Path(root) / scanned).rglob("*.py")):
            if "tests" in path.relative_to(root).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                    found.extend((str(path), line) for line in _compares_country(node.test))
    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    found = branches(Path(args[0]) if args else ROOT)
    for path, line in found:
        print(f"{path}:{line}: a country code drives this condition; put the rule in the country pack")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
