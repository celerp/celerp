# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""The chart of accounts as a tree of balances, to any depth.

A report section is the accounts of its types, nested as the chart nests them.
A header carries the total of everything under it. An account that was posted to
before it gained sub-accounts keeps those postings, shown once as its own line
under its header. Every account's balance therefore counts exactly once in the
section total, however deep the chart goes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from celerp_accounting.models import Account


def account_tree_lines(
    accounts: Iterable[Account], balances: dict[str, Decimal], types: set[str], *, credit_normal: bool,
) -> tuple[list[dict], Decimal]:
    """The section's lines in chart order, and its total.

    ``balances`` is each account's debit minus credit. Each line carries ``depth``
    (0 for a top-level account); a header's line has ``is_parent`` and the total
    under it. Accounts with no postings anywhere under them are left out. An account
    whose parent is outside the section, or on a loop in the chart, is shown at the
    top level rather than dropped.
    """
    nodes = {a.code: a for a in accounts if a.account_type in types}
    children: dict[str | None, list[str]] = {}
    for code in sorted(nodes):
        parent = nodes[code].parent_code
        children.setdefault(parent if parent in nodes else None, []).append(code)
    sign = -1 if credit_normal else 1
    own = {code: sign * balances[code] for code in nodes if code in balances}
    seen: set[str] = set()

    def line(code: str, amount: Decimal, depth: int, **extra) -> dict:
        acc = nodes[code]
        return {"code": code, "name": acc.name, "account_type": acc.account_type,
                "amount": float(amount), "depth": depth, **extra}

    def walk(code: str, depth: int) -> tuple[list[dict], Decimal] | None:
        seen.add(code)
        below: list[dict] = []
        total = Decimal(0)
        for child in children.get(code, []):
            if child not in seen and (sub := walk(child, depth + 1)) is not None:
                below += sub[0]
                total += sub[1]
        if not below:
            return ([line(code, own[code], depth)], own[code]) if code in own else None
        if code in own:
            below.insert(0, line(code, own[code], depth + 1))
            total += own[code]
        return [line(code, total, depth, is_parent=True), *below], total

    lines: list[dict] = []
    section_total = Decimal(0)
    for code in [*children.get(None, []), *sorted(nodes)]:
        if code not in seen and (sub := walk(code, 0)) is not None:
            lines += sub[0]
            section_total += sub[1]
    return lines, section_total
