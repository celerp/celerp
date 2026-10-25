# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests share one database across tests, and a document id (doc:<ref>) is unique only
within its company. So every statement a browser test runs on a projection by its entity id
also names the company; otherwise it reads, or rewrites, another test's document."""
from __future__ import annotations

import ast
from pathlib import Path


def test_every_browser_test_statement_on_a_projection_names_the_company():
    root = Path(__file__).parent / "test_browser"
    unscoped = [
        f"{path.name}:{node.lineno}"
        for path in sorted(root.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "projections" in node.value and "entity_id" in node.value and "company_id" not in node.value
    ]
    assert not unscoped, unscoped
