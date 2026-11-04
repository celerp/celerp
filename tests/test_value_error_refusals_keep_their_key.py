# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A route that turns a ValueError into an HTTP refusal answers a keyed refusal (Refused is a
ValueError) with its key, never flattened to its English text: whatever the spelling, a
detail made from the caught exception goes through ``refusal_detail``."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from celerp.accounting_roles import Refused, refusal_detail

ROOT = Path(__file__).resolve().parents[1]

# Handlers that put the caught exception inside another message (a param of a wider
# refusal, or text). Each file's count is pinned, so a new one fails here.
_NESTED = {
    "celerp/routers/companies.py": 1,
    "default_modules/celerp-connectors/celerp_connectors/routes.py": 2,
    "default_modules/celerp-docs/celerp_docs/routes.py": 8,
}


def _sites() -> Counter:
    """Per file, the HTTPException details in a ValueError or Exception handler that use the
    caught exception other than as ``refusal_detail(exc)``."""
    sites = Counter()
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
                    if kw.arg != "detail" or not any(isinstance(n, ast.Name) and n.id == handler.name for n in ast.walk(v)):
                        continue
                    if (isinstance(v, ast.Call) and getattr(v.func, "id", None) == "refusal_detail"
                            and len(v.args) == 1 and isinstance(v.args[0], ast.Name) and v.args[0].id == handler.name):
                        continue
                    sites[str(path.relative_to(ROOT))] += 1
    return sites


def test_no_value_error_handler_flattens_a_keyed_refusal():
    assert dict(_sites()) == _NESTED


def test_refusal_detail_keeps_a_keyed_refusal_and_reads_plain_text():
    keyed = Refused("posting.choose_account", "Choose an account.")
    assert refusal_detail(keyed) == keyed.detail
    assert refusal_detail(ValueError("Bad date.")) == "Bad date."
