# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Labor providers — the extension seam for the future auto-labor premium module.

v1 ships the seam only (no providers registered): a recipe's labor is whatever the user
entered manually. A premium module can `register_labor_provider(fn)` with a pure function
`fn(components) -> list[LaborLine-dicts]`; its lines are tagged ``source="auto:<...>"`` and
appended to the manual lines. The cost roll-up sums all labor regardless of source, so manual
and auto lines compose without either clobbering the other.
"""
from __future__ import annotations

from typing import Callable

LaborProvider = Callable[[list[dict]], list[dict]]

_PROVIDERS: list[LaborProvider] = []


def register_labor_provider(fn: LaborProvider) -> None:
    _PROVIDERS.append(fn)


def clear_labor_providers() -> None:
    """Reset the registry (used by tests)."""
    _PROVIDERS.clear()


def apply_labor_providers(components: list[dict], manual_labor: list[dict]) -> list[dict]:
    """Return the recipe's full labor list: the user's manual lines + any provider lines.

    Deterministic and idempotent: existing auto lines are dropped before regenerating, so
    re-applying never duplicates them; manual lines are always preserved untouched.
    """
    result = [dict(l) for l in manual_labor if (l.get("source") or "manual") == "manual"]
    for provider in _PROVIDERS:
        for line in provider(components):
            # Provider lines can never be tagged "manual" — that would let them escape
            # regeneration and accumulate as duplicates. Coerce to an auto source.
            src = line.get("source") or "auto"
            result.append({**line, "source": "auto" if src == "manual" else src})
    return result
