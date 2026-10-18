# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The work a module leaves open when a company turns it off (the turn_off_preflight slot).

Blocking items refuse turning the module off, naming them; listed items are shown beside
Turn off and kept for when the module is on again.
"""
from __future__ import annotations


async def outstanding(session, company_id, module: str) -> dict:
    """{"blocking": [...], "listed": [...]} from the module's own preflight, each item
    {"label", "href"}; both empty for a module that declares none."""
    from celerp.modules.slots import get as get_slot, resolve_handler

    found = {"blocking": [], "listed": []}
    for entry in get_slot("turn_off_preflight"):
        if entry["_module"] != module:
            continue
        result = await resolve_handler(entry["handler"])(session=session, company_id=company_id)
        for kind in found:
            found[kind] += result.get(kind) or []
    return found
