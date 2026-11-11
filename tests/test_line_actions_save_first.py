# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An action that runs on a page's lines posts only after the page's pending line edits are
stored. Every click or submit handler that posts to a document or list action route is built by
persist_then_post, which waits for line cell saves and saves the lines first; a handler string
that posts to an action route itself would run the action on lines the page has not stored."""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HANDLERS = {"onclick", "onsubmit"}
_SAVES_FIRST = "persist_then_post"


def _ui_sources() -> list[pathlib.Path]:
    return sorted({*_ROOT.glob("ui/**/*.py"), *_ROOT.glob("default_modules/*/*/ui*.py"),
                   *_ROOT.glob("default_modules/*/*/ui/**/*.py")})


def _literal_text(node) -> str:
    """The literal text a string expression holds (f-string and + parts joined)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


def _posts_to_an_action(text: str) -> bool:
    compact = text.replace(" ", "").replace('"', "'")
    return "/action/" in text and ("htmx.ajax('POST'" in compact or "hx-post" in compact or "fetch(" in compact)


def _violations(source: str, path: str = "<snippet>") -> list[str]:
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _SAVES_FIRST:
            continue
        if isinstance(node, ast.keyword) and node.arg in _HANDLERS:
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", getattr(value.func, "attr", "")) == _SAVES_FIRST:
                continue
            if "/action/" in _literal_text(value):
                out.append(f"{path}:{node.arg} posts to an action route without saving the lines first")
        elif isinstance(node, (ast.Constant, ast.JoinedStr)) and _posts_to_an_action(_literal_text(node)):
            out.append(f"{path}: a handler string posts to an action route itself")
    return out


def test_line_actions_save_the_lines_first():
    found = []
    for path in _ui_sources():
        found += _violations(path.read_text(encoding="utf-8"), str(path.relative_to(_ROOT)))
    assert not found, "\n".join(found)


def test_the_rule_catches_an_action_posted_directly():
    """Self-check: the rule fails a handler that posts to an action route itself."""
    assert _violations('Button("Go", onclick="htmx.ajax(\'POST\',\'/docs/x/action/y\',{swap:\'none\'})")')
    assert _violations('Button("Go", onclick=f"htmx.ajax(\'POST\',\'/lists/{eid}/action/finalize\')")')
    assert not _violations('Button("Go", onclick=persist_then_post(f"/lists/{eid}/action/finalize"))')
