# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every status a document or list can hold has an ``enum.doc_status`` label in every
locale, so no status falls back to English title case on the page title or the badge.

The status universe is read from the code that writes it: every literal the document
projection assigns to ``current["status"]`` or returns as a derived status, the status sets
the balance and line rules key on, and every badge token a list can render."""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from celerp.services import doc_balance
from celerp.services.list_behavior import LIST_STATUSES, LIST_TYPES, status_key
from ui import i18n

_PROJECTIONS = Path(__file__).resolve().parents[1] / "default_modules/celerp-docs/celerp_docs/doc_projections.py"
_DERIVED_STATUS_FUNCS = {"_payment_status", "_status_without_receipts"}


def _literals(node: ast.AST | None) -> set[str]:
    """The string literals an expression can evaluate to (not the keys or tests it reads)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literals(node.body) | _literals(node.orelse)
    if isinstance(node, ast.BoolOp):
        return set().union(*(_literals(v) for v in node.values))
    return set()


def _projected_statuses() -> set[str]:
    tree = ast.parse(_PROJECTIONS.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == "current"
                        and isinstance(target.slice, ast.Constant) and target.slice.value == "status"):
                    found |= _literals(node.value)
        elif isinstance(node, ast.FunctionDef) and node.name in _DERIVED_STATUS_FUNCS:
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return):
                    found |= _literals(ret.value)
    return found


def _balance_statuses() -> set[str]:
    found = set(doc_balance.MEMO_LIVE_STATUSES)
    for table in (doc_balance.AWAITING_PAYMENT_STATUSES, doc_balance.OVERDUE_STATUSES):
        for statuses in table.values():
            found |= statuses
    return found


def _list_badge_tokens() -> set[str]:
    results = (None, "converted", "stock_adjusted", "received", "expired", "written_off", "other")
    milestones = ({}, {"sent_at": "x"}, {"accepted_at": "x"})
    return {
        status_key({"status": s, "list_type": lt, "result": r, **m})
        for s, lt, r, m in itertools.product(LIST_STATUSES, LIST_TYPES, results, milestones)
    }


_ALL_STATUSES = sorted(_projected_statuses() | _balance_statuses() | _list_badge_tokens())


def test_the_status_universe_is_read_from_the_code():
    assert {"draft", "sent", "final", "partial", "paid", "void", "awaiting_payment",
            "received", "partially_received", "returned", "partial_returned", "converted",
            "closed", "finalized", "counting", "in_transit", "issued", "accepted"} <= set(_ALL_STATUSES)


@pytest.mark.parametrize("lang", sorted(i18n._DISK_LANGS))
def test_every_status_has_a_label_in_every_locale(lang):
    catalog = i18n._cached_load(lang)
    missing = [s for s in _ALL_STATUSES if not catalog.get(f"enum.doc_status.{s}")]
    assert not missing, f"{lang} lacks enum.doc_status keys for {missing}"
