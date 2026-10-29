# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests check an element's classes with a retrying expect, never with one read.

htmx keeps its settling classes on a swapped element for a moment after the swap, and
scripts add classes after paint, so a class read once can see the element mid-change and
fail on a loaded machine. expect(...).to_have_class(css_class(...)) retries until it holds."""
from __future__ import annotations

import ast
from pathlib import Path

_BROWSER_TESTS = Path(__file__).resolve().parent / "test_browser"


def _one_shot_class_reads(source: str) -> list[int]:
    """Lines reading class once: get_attribute("class"), or an assert on an evaluate whose
    script reads className or classList."""
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_attribute" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "class"):
            lines.append(node.lineno)
        if isinstance(node, ast.Assert):
            for call in ast.walk(node.test):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "evaluate" and call.args
                        and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
                        and ("className" in call.args[0].value or "classList" in call.args[0].value)):
                    lines.append(node.lineno)
    return sorted(set(lines))


def test_no_browser_test_reads_classes_once():
    found = [f"{path.name}:{line}" for path in sorted(_BROWSER_TESTS.glob("*.py"))
             for line in _one_shot_class_reads(path.read_text(encoding="utf-8"))]
    assert found == [], found


def test_the_guard_sees_each_one_shot_spelling():
    cases = [
        'assert "a" in (loc.get_attribute("class") or "")',
        'cls = loc.get_attribute("class")',
        'assert loc.evaluate("e => e.className").find("a") < 0',
        'assert loc.evaluate("e => e.classList.contains(\'a\')")',
    ]
    assert [bool(_one_shot_class_reads(case)) for case in cases] == [True] * len(cases)
    assert _one_shot_class_reads('expect(loc).to_have_class(css_class("a"))') == []
