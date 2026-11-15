# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Gate rules outside require_permission that the agent compiler can read.

A dependency that refuses a caller for a reason require_permission does not express
(the installation owner, the company owner) is tagged here where it is defined, so
compile drops the tool for a caller who fails it instead of failing only when run.
Kept free of imports: the auth services that define these gates load before the
compiler does.
"""

from __future__ import annotations

from typing import Any

AGENT_GATES = frozenset({"owner", "install_owner"})


def agent_gate(dependency: Any, rule: str) -> Any:
    """Tag ``dependency`` with ``rule`` and return it."""
    if rule not in AGENT_GATES:
        raise ValueError(f"unknown agent gate: {rule!r}")
    dependency.agent_gate = rule
    return dependency
