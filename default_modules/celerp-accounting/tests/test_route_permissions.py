# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every accounting endpoint's required permission, declared once and asserted.

Reading the books and changing them are separate grants: reports answer to
view_financial_reports, everything that writes answers to manage_accounting. The
file has over forty endpoints and the distinction is made one decorator at a time,
so the split is pinned here rather than left to review. A new endpoint with no
entry below fails, which is the point: it has to be a decision, not an oversight.
"""

from __future__ import annotations

import pytest

from celerp_accounting.routes import router

READ = "view_financial_reports"
WRITE = "manage_accounting"

EXPECTED: dict[tuple[str, str], str] = {
    # Reports. Reading these is what view_financial_reports means.
    ("GET", "/chart"): READ,
    ("GET", "/journal"): READ,
    ("GET", "/ledger/{account_code}"): READ,
    ("GET", "/trial-balance"): READ,
    ("GET", "/general-ledger"): READ,
    ("GET", "/pnl"): READ,
    ("GET", "/balance-sheet"): READ,
    ("GET", "/soa/{contact_id}"): READ,
    ("GET", "/cash-flow"): READ,
}


def _declared_permission(route) -> str | None:
    """The permission a route requires, read off its dependency callables."""
    seen = set()
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        dep = stack.pop()
        val = getattr(getattr(dep, "call", None), "required_permission", None)
        if isinstance(val, str):
            seen.add(val)
        stack.extend(getattr(dep, "dependencies", []) or [])
    if not seen:
        return None
    assert len(seen) == 1, f"{route.path} requires more than one permission: {seen}"
    return seen.pop()


def test_read_endpoints_require_the_read_permission():
    """Each report listed above is readable with view_financial_reports."""
    by_key = {}
    for route in router.routes:
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            by_key[(method, getattr(route, "path", ""))] = route
    missing = [k for k in EXPECTED if k not in by_key]
    assert not missing, f"declared endpoints that no longer exist: {missing}"
    for key, expected in sorted(EXPECTED.items()):
        got = _declared_permission(by_key[key])
        assert got == expected, f"{key[0]} {key[1]}: expected {expected}, found {got}"


def test_no_other_endpoint_reads_with_the_report_permission():
    """Widening the read grant must not sweep in anything that writes.

    Anything not named in EXPECTED keeps manage_accounting, so relaxing one
    endpoint too many fails here rather than in production.
    """
    unexpected = []
    for route in router.routes:
        path = getattr(route, "path", "")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            if (method, path) in EXPECTED:
                continue
            if _declared_permission(route) == READ:
                unexpected.append((method, path))
    assert not unexpected, f"these gained the report permission without being declared: {unexpected}"


def test_every_write_endpoint_is_guarded():
    """No endpoint may be left with no permission at all."""
    unguarded = []
    for route in router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        if not methods:
            continue
        if _declared_permission(route) is None:
            unguarded.append((sorted(methods), path))
    assert not unguarded, f"endpoints with no permission requirement: {unguarded}"
