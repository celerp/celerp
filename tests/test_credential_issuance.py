# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Credential issuance is one owned decision: every caller states whether it continues a
session or authenticates afresh."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from celerp import credentials

REPO = Path(__file__).resolve().parents[1]
_ISSUERS = ("issue_token_pair", "issue_token_pair_by_id")


def _production_sources():
    for root in ("celerp", "ui", "default_modules"):
        for path in sorted((REPO / root).rglob("*.py")):
            if "tests" in path.relative_to(REPO).parts:
                continue
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_session_freshness_has_no_default():
    """Omitting the session the credentials continue is a call error, never a fresh sign-in."""
    for name in _ISSUERS:
        param = inspect.signature(getattr(credentials, name)).parameters["expected_snonce"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is inspect.Parameter.empty, name


def test_every_issuance_call_states_session_freshness():
    """Every production call of the issuer names the session it continues, or None."""
    calls, silent = 0, []
    for path, tree in _production_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in _ISSUERS:
                calls += 1
                if "expected_snonce" not in {kw.arg for kw in node.keywords}:
                    silent.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert calls > 10
    assert silent == []


def test_credentials_are_created_only_in_the_credentials_module():
    """The functions that create or issue credentials, and every token encoding, live
    in celerp.credentials alone."""
    owned = {name for name, fn in inspect.getmembers(credentials, inspect.isfunction)
             if fn.__module__ == credentials.__name__}
    own_file = REPO / "celerp" / "credentials.py"
    elsewhere = []
    for path, tree in _production_sources():
        if path == own_file:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in owned:
                elsewhere.append(f"{path.relative_to(REPO)}:{node.lineno} defines {node.name}")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "encode" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "jwt"):
                elsewhere.append(f"{path.relative_to(REPO)}:{node.lineno} encodes a token")
    assert {"create_access_token", "create_refresh_token", *_ISSUERS} <= owned
    assert elsewhere == []
