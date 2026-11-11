# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The remote-access gateway's own replies are never markup.

The gateway passes the app's responses through unchanged (the app encodes those), and builds
a few replies itself: a status with a short body, an event-stream stub and a JSON refusal.
Every content type those self-built replies can carry is plain text, an event stream or JSON,
so no gateway reply is ever rendered as a page.
"""
from __future__ import annotations

import ast
from pathlib import Path

_CLIENT = Path(__file__).resolve().parents[1] / "celerp" / "gateway" / "client.py"
_INERT = {"text/plain", "text/event-stream", "application/json"}


def _header_lists(node: ast.AST):
    """Every literal header list ([[name, value], ...]) below *node*."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.List) and sub.elts and all(
                isinstance(e, ast.List) and len(e.elts) == 2 for e in sub.elts):
            yield sub


def _self_built_types(source: str) -> list[str]:
    """Content types of the replies the gateway builds itself (not the passthrough)."""
    tree = ast.parse(source)
    types: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        builds_reply = any(isinstance(d, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant)
            and v.value == "http.response" for k, v in zip(d.keys, d.values)) for d in ast.walk(fn))
        calls_status = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                           and c.func.attr == "_send_status" for c in ast.walk(fn))
        if not (builds_reply or calls_status):
            continue
        for headers in _header_lists(fn):
            for pair in headers.elts:
                name, value = pair.elts
                if isinstance(name, ast.Constant) and str(name.value).lower() == "content-type":
                    assert isinstance(value, ast.Constant), f"computed reply type in {fn.name}"
                    types.append(str(value.value).split(";")[0].strip().lower())
    return types


def test_gateway_replies_are_not_markup():
    source = _CLIENT.read_text(encoding="utf-8")
    types = _self_built_types(source)
    assert "text/plain" in types and "application/json" in types, types
    assert set(types) <= _INERT, f"a gateway reply would render as a page: {set(types) - _INERT}"

    seeded = source.replace('[["content-type", "text/plain"]]', '[["content-type", "text/html"]]', 1)
    assert seeded != source
    assert "text/html" in _self_built_types(seeded), "the collector reads the status default"
