# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A browser test never hides a failed route release.

A test that holds requests with page.route has to settle each held route itself. If it
unroutes first, the browser sends the held request on to the server, and the abort that
follows fails with "Route is already handled". Swallowing that error lets the request
reach the server without the test knowing. So a route's abort, continue_, fallback or
fulfill is never wrapped in a try whose handler only passes, and held routes are released
through conftest.release_held_routes, which aborts them before it unroutes."""
from __future__ import annotations

import ast

from test_helpers import REPO_ROOT

_SETTLE = frozenset({"abort", "continue_", "fallback", "fulfill"})


def _settles(body) -> bool:
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in _SETTLE
               for stmt in body for node in ast.walk(stmt))


def _silent(handler: ast.ExceptHandler) -> bool:
    return all(isinstance(stmt, ast.Pass) for stmt in handler.body)


def test_no_browser_test_swallows_a_route_release_error():
    found = []
    for path in sorted(REPO_ROOT.glob("tests/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and _settles(node.body) and any(map(_silent, node.handlers)):
                found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not found, (
        "route releases with their errors swallowed (use conftest.release_held_routes): "
        + ", ".join(found))
