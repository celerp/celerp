# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests check an element's classes and focus with a retrying expect, never with one read.

htmx keeps its settling classes on a swapped element for a moment after the swap, and
scripts add classes and move focus after paint, so a class or focus read once can see the
element mid-change and fail on a loaded machine. expect(...).to_have_class(css_class(...))
and expect(...).to_be_focused() retry until they hold.

A negative class check must name a class the app sets: not_to_have_class on a name no
element ever carries, or on a plain string (an exact match of the whole class attribute),
passes whatever the page shows."""
from __future__ import annotations

import ast
import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_BROWSER_TESTS = _TESTS / "test_browser"
_APP_DIRS = ("ui", "celerp", "default_modules")
_SCRIPT_READS = re.compile(
    r"className|classList(?!\s*\.\s*(?:add|remove|toggle|replace)\b)|getAttribute\(\s*['\"]class['\"]|activeElement")
# Test functions whose class read cannot see a change in flight, each with the reason.
_STABLE_READS = {
    ("test_mobile_reflow.py", "_fits"):
        "the classes only label an overflowing element in the failure message; the check is its geometry",
    ("test_bulk_delete_drafts.py", "test_a_reloaded_row_matches_the_row_the_list_drew"):
        "the row shape drops htmx's transient classes, so what it reads does not change while a swap settles",
    ("test_fixed_assets_journeys.py", "_alignments"):
        "its caller _aligned only checks for cell--number as a substring, which htmx's transient classes cannot remove",
    ("test_fixed_assets_journeys.py", "_aligned"):
        "it checks the classes _alignments read for cell--number as a substring, which htmx's transient classes "
        "cannot remove",
    ("test_data_table_match_column.py", "_assert_aligned"):
        "it is given cell classes read after a full page reload and wait_for_selector, and htmx marks only the "
        "top-level nodes it swaps, never the table's cells",
}


def _string_constants(tree: ast.AST) -> dict[str, str]:
    """Names bound to a string literal anywhere in the file, so a script held in a constant
    is read like one written inline."""
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def _is_read(node: ast.AST, constants: dict[str, str]) -> bool:
    """A call reading class or focus state once: get_attribute of "class" in any argument
    position, or an evaluate-family call whose script reads classes or the focused element."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    values = [*node.args, *(k.value for k in node.keywords)]
    if node.func.attr == "get_attribute":
        return any(isinstance(v, ast.Constant) and v.value == "class" for v in values)
    if node.func.attr.startswith("eval"):
        for v in values:
            script = v.value if isinstance(v, ast.Constant) else constants.get(getattr(v, "id", None))
            if isinstance(script, str) and _SCRIPT_READS.search(script):
                return True
    return False


def _bound_names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _flagged_asserts(scope: list[ast.AST], constants: dict[str, str]) -> set[int]:
    """Assert lines a one-shot read reaches, directly or through names bound from it."""
    nodes = [n for stmt in scope for n in ast.walk(stmt)]

    def tainted(expr: ast.AST, names: set[str]) -> bool:
        return any(_is_read(n, constants) or (isinstance(n, ast.Name) and n.id in names)
                   for n in ast.walk(expr))

    names: set[str] = set()
    while True:
        before = len(names)
        for n in nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                if tainted(n.value, names):
                    for t in targets:
                        names |= _bound_names(t)
            elif isinstance(n, ast.NamedExpr) and tainted(n.value, names):
                names.add(n.target.id)
            elif isinstance(n, (ast.For, ast.comprehension)) and tainted(n.iter, names):
                names |= _bound_names(n.target)
        if len(names) == before:
            break
    return {n.lineno for n in nodes if isinstance(n, ast.Assert) and tainted(n.test, names)}


def _one_shot_class_reads(source: str, stable: frozenset[str] = frozenset()) -> list[int]:
    """Assert lines checking a class or focus read once. Each function is its own scope,
    skipped when named in *stable*; statements outside any function share one."""
    tree = ast.parse(source)
    constants = _string_constants(tree)
    functions = [s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    loose = [s for s in tree.body if s not in functions]
    lines = _flagged_asserts(loose, constants)
    for fn in functions:
        if fn.name not in stable:
            lines |= _flagged_asserts([fn], constants)
    return sorted(lines)


def _app_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="ignore")
                     for top in _APP_DIRS for path in (_TESTS.parent / top).rglob("*")
                     if path.suffix in (".py", ".js", ".css", ".html") and "tests" not in path.parts)


def _vacuous_negative_class_checks(source: str, app_text: str) -> list[str]:
    """not_to_have_class calls that are not css_class(<a class the app source names>)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "not_to_have_class"):
            continue
        arg = node.args[0] if node.args else None
        name = (arg.args[0].value if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "css_class"
                and arg.args and isinstance(arg.args[0], ast.Constant) else None)
        if not (isinstance(name, str)
                and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", app_text)):
            found.append(f"{node.lineno} {ast.unparse(arg) if arg else ''}")
    return found


def test_no_browser_test_reads_classes_or_focus_once():
    found = [f"{path.name}:{line}" for path in sorted(_BROWSER_TESTS.glob("*.py"))
             for line in _one_shot_class_reads(path.read_text(encoding="utf-8"),
                                               frozenset(fn for name, fn in _STABLE_READS if name == path.name))]
    assert found == [], found


def test_every_stable_read_names_a_function_that_exists():
    missing = [key for key in _STABLE_READS
               if not (_BROWSER_TESTS / key[0]).exists()
               or f"def {key[1]}(" not in (_BROWSER_TESTS / key[0]).read_text(encoding="utf-8")]
    assert missing == [], missing


def test_every_negative_class_check_names_a_class_the_app_sets():
    app_text = _app_text()
    found = [f"{path.name}:{hit}" for path in sorted(_BROWSER_TESTS.glob("*.py"))
             for hit in _vacuous_negative_class_checks(path.read_text(encoding="utf-8"), app_text)]
    assert found == [], found


def test_the_guard_sees_each_one_shot_spelling():
    cases = [
        'assert "a" in (loc.get_attribute("class") or "")',
        'cls = loc.get_attribute("class")\nassert "a" in cls',
        'assert loc.evaluate("e => e.className").find("a") < 0',
        'assert loc.evaluate("e => e.classList.contains(\'a\')")',
        'seen = page.evaluate("e => e.classList.contains(\'a\')")\nassert seen',
        'seen = page.evaluate("e => e.className")\nparts = seen.split()\nassert "a" in parts',
        'assert "a" in loc.get_attribute(name="class")',
        'assert "a" in page.get_attribute("#x", "class")',
        'assert "a" in loc.evaluate("e => e.getAttribute(\'class\')")',
        'assert "a" in page.eval_on_selector("#x", "e => e.className")',
        'JS = "e => e.className"\nassert "a" in loc.evaluate(JS)',
        'assert page.evaluate("document.activeElement.id") == "x"',
        'def test_t(page):\n    active = page.evaluate("document.activeElement.name")\n    assert active != "x"',
    ]
    assert [bool(_one_shot_class_reads(case)) for case in cases] == [True] * len(cases)
    for fine in ('expect(loc).to_have_class(css_class("a"))',
                 'expect(loc).to_be_focused()',
                 'cls = loc.get_attribute("class")',
                 'if page.evaluate("document.activeElement.id") == "x":\n    pass',
                 'assert loc.get_attribute("href") == "/a"',
                 'assert page.evaluate("getComputedStyle(document.body).color")',
                 'assert page.evaluate("p => { p.classList.add(\'a\'); return 1; }")'):
        assert _one_shot_class_reads(fine) == [], fine


def test_the_guard_sees_each_vacuous_negative_class_check():
    app = '.scroll-more-left { } btn.classList.add("pulse")'
    assert _vacuous_negative_class_checks('expect(x).not_to_have_class(css_class("scroll-more"))', app)
    assert _vacuous_negative_class_checks('expect(x).not_to_have_class("pulse")', app)
    assert _vacuous_negative_class_checks('expect(x).not_to_have_class(re.compile("pulse"))', app)
    assert _vacuous_negative_class_checks('expect(x).not_to_have_class(css_class("scroll-more-left"))', app) == []
    assert _vacuous_negative_class_checks('expect(x).not_to_have_class(css_class("pulse"))', app) == []
