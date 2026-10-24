# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A search never hands LIKE a value with its wildcards live.

A % or _ typed into a search, or carried in a stored value, has to match itself. So no
pattern wraps a value in % without escaping it, and every contains() on a column (which
builds that pattern) escapes with autoescape. Each search's own test shows the behaviour;
this keeps a new search from coming in without it."""
from __future__ import annotations

import ast

from test_helpers import REPO_ROOT


def _sources():
    for pattern in ("celerp/**/*.py", "default_modules/*/*/**/*.py", "ui/**/*.py"):
        for path in REPO_ROOT.glob(pattern):
            if "tests" not in path.relative_to(REPO_ROOT).parts:
                yield path


def _escaped(node) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "_like_escape"


def test_no_search_pattern_leaves_a_value_s_wildcards_live():
    live = []
    for path in _sources():
        name = path.relative_to(REPO_ROOT)
        for node in ast.walk(ast.parse(path.read_text())):
            where = f"{name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.JoinedStr):
                parts = node.values
                for before, value in zip(parts, parts[1:]):
                    if (isinstance(before, ast.Constant) and str(before.value).endswith("%")
                            and isinstance(value, ast.FormattedValue) and not _escaped(value.value)):
                        live.append(f"{where} wraps a value in % unescaped")
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "contains"
                    and len(node.args) == 1 and not isinstance(node.args[0], (ast.List, ast.Dict))
                    and not any(k.arg == "autoescape" for k in node.keywords)):
                live.append(f"{where} contains() without autoescape")
    assert not live, live
