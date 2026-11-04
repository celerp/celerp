# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A route that turns a ValueError into an HTTP refusal answers a keyed refusal (Refused is a
ValueError) with its key, never flattened to its English text: the detail is the
exception's ``detail`` when it has one."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _flattening_sites() -> list[str]:
    sites = []
    for path in [*(ROOT / "celerp").rglob("*.py"), *(ROOT / "default_modules").rglob("*.py"), *(ROOT / "ui").rglob("*.py")]:
        for handler in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                continue
            caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            if not any(isinstance(t, ast.Name) and t.id in ("ValueError", "Exception") for t in caught):
                continue
            for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HTTPException"):
                    continue
                for kw in node.keywords:
                    v = kw.value
                    if (kw.arg == "detail" and isinstance(v, ast.Call) and getattr(v.func, "id", None) == "str"
                            and v.args and isinstance(v.args[0], ast.Name) and v.args[0].id == handler.name):
                        sites.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return sites


def test_no_value_error_handler_flattens_a_keyed_refusal():
    assert _flattening_sites() == []
