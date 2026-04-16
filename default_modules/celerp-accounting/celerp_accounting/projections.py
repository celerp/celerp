# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def apply_accounting_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "acc.journal_entry.created":
        current.update({"entity_type": "journal_entry", **data})
        # We don't currently expose a draft-journal-entry workflow in the UI/API.
        # Default to posted so accounting reports are meaningful for normal ERP usage.
        current.setdefault("status", "posted")
    elif event_type == "acc.journal_entry.posted":
        current["status"] = "posted"
    elif event_type == "acc.journal_entry.voided":
        current["status"] = "void"
        if data.get("reason"):
            current["void_reason"] = data["reason"]

    elif event_type == "acc.period.closed":
        current.update({"entity_type": "period", "period": data["period"], "status": "closed"})
    elif event_type == "acc.period.reopened":
        current.update({"entity_type": "period", "period": data["period"], "status": "open"})
    else:
        raise ValueError(f"Unsupported accounting event: {event_type}")

    return current
