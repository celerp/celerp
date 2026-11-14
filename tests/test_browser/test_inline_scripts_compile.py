"""Every literal inline script in ui/ compiles as JavaScript.

A plain (non-f) Python string that carries f-string syntax such as `{icon("x")}` reaches
the browser verbatim, and one syntax error disables every function in that script. This
compiles each literal Script(...) body with Chromium's own parser, so the whole class
fails here instead of on a user's page."""
import ast
import pathlib

import pytest

pytestmark = pytest.mark.browser

UI = pathlib.Path(__file__).resolve().parents[2] / "ui"


def _variants(node):
    """Every string a body built only from literals can take: a literal, `a + b`, or
    `a if cond else b` over literals. None when any part is computed at run time."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _variants(node.left), _variants(node.right)
        return None if left is None or right is None else [a + b for a in left for b in right]
    if isinstance(node, ast.IfExp):
        body, orelse = _variants(node.body), _variants(node.orelse)
        return None if body is None or orelse is None else body + orelse
    return None


def _literal_scripts():
    for path in sorted(UI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Script" and node.args:
                for i, body in enumerate(_variants(node.args[0]) or []):
                    if body.strip():
                        yield f"{path.relative_to(UI.parent)}:{node.lineno}#{i}", body


def test_every_literal_inline_script_compiles(playwright):
    scripts = list(_literal_scripts())
    assert len(scripts) > 20, "no inline scripts found; the walker no longer matches ui/"
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page()
        failures = page.evaluate(
            """(items) => items.flatMap(([where, body]) => {
                 try { new Function(body); return []; }
                 catch (e) { return [where + ': ' + e.message]; }
               })""",
            scripts,
        )
    finally:
        browser.close()
    assert not failures, failures
