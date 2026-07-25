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


def test_every_endpoint_requires_exactly_the_permission_declared_for_it():
    """Every route in the router, checked against the table, one assertion.

    A route named in EXPECTED must require that permission; every other route
    must require manage_accounting. Three ways to fail land here: a report that
    stopped answering to view_financial_reports, a writer that picked it up, and
    an endpoint left with no permission at all (found is None, which matches
    neither side of the table).
    """
    found = {}
    for route in router.routes:
        path = getattr(route, "path", "")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            found[(method, path)] = _declared_permission(route)

    missing = sorted(k for k in EXPECTED if k not in found)
    assert not missing, f"declared endpoints that no longer exist: {missing}"

    wrong = {
        key: (EXPECTED.get(key, WRITE), got)
        for key, got in sorted(found.items())
        if got != EXPECTED.get(key, WRITE)
    }
    assert not wrong, f"expected vs found, per endpoint: {wrong}"
