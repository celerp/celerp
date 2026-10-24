# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A LIKE never runs with a value's wildcards live.

A % or _ typed into a search, or carried in an id or another stored value, has to match itself.
So a pattern built from a value goes through startswith, endswith or contains with autoescape,
and like() only ever takes a fixed pattern written in the code. Each search's own test shows
the behaviour; this keeps a new search or id lookup from coming in without it."""
from __future__ import annotations

import ast

from test_helpers import REPO_ROOT

_LIKE = frozenset({"like", "ilike", "not_like", "notlike", "not_ilike", "notilike"})
_AFFIX = frozenset({"startswith", "endswith"})


def _sources():
    for pattern in ("celerp/**/*.py", "default_modules/*/*/**/*.py", "ui/**/*.py"):
        for path in REPO_ROOT.glob(pattern):
            if "tests" not in path.relative_to(REPO_ROOT).parts:
                yield path


def _constants(tree) -> dict[str, object]:
    """The module's own names bound once to a literal (``_PATTERN = "je:auto:%"``)."""
    bound: dict[str, list] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(node.value)
    return {k: v[0].value for k, v in bound.items() if len(v) == 1 and isinstance(v[0], ast.Constant)}


def _fixed(node, constants: dict[str, object]) -> bool:
    """A pattern written out in the code, directly or through one of the module's literal
    names, with no ``_`` in it, so it can only match what it says."""
    value = constants.get(node.id) if isinstance(node, ast.Name) else getattr(node, "value", None)
    return isinstance(node, (ast.Constant, ast.Name)) and isinstance(value, str) and "_" not in value


def _sql(receiver) -> bool:
    """Whether a startswith/endswith receiver is a SQL expression: it is reached from a mapped
    class (Projection.entity_id, Company.slug), or built by a SQL call (as_string(), func.lower(),
    cast()). A str's own startswith (row.entity_id, name.lower()) is plain Python."""
    node = receiver
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr.startswith("as_"):
                return True
            if isinstance(callee, ast.Name):
                return callee.id == "cast"
            node = callee
        node = node.value
    return isinstance(node, ast.Name) and (node.id in ("func", "_func", "sa", "_sa")
                                           or (node.id[:1].isupper() and not node.id.isupper()))


def _escapes(call) -> bool:
    return any(k.arg == "autoescape" for k in call.keywords)


def test_no_like_leaves_a_value_s_wildcards_live():
    live = []
    for path in _sources():
        name = path.relative_to(REPO_ROOT)
        tree = ast.parse(path.read_text())
        constants = _constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for before, value in zip(node.values, node.values[1:]):
                    if (isinstance(before, ast.Constant) and str(before.value).endswith("%")
                            and isinstance(value, ast.FormattedValue)):
                        live.append(f"{name}:{node.lineno} wraps a value in %")
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
                continue
            attr, arg = node.func.attr, node.args[0]
            if attr in _LIKE and not _fixed(arg, constants):
                live.append(f"{name}:{node.lineno} {attr}() on a pattern built from a value")
            if attr in _AFFIX and _sql(node.func.value) and not _escapes(node) and not _fixed(arg, constants):
                live.append(f"{name}:{node.lineno} {attr}() without autoescape")
            if (attr == "contains" and len(node.args) == 1 and not isinstance(arg, (ast.List, ast.Dict))
                    and not _escapes(node)):
                live.append(f"{name}:{node.lineno} contains() without autoescape")
    assert not live, live
