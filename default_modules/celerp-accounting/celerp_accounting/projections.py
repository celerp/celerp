# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy


def apply_accounting_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type == "acc.journal_entry.created":
        current.update({"entity_type": "journal_entry", **data})
        # Written again, the entry is live on its own date: an earlier reversal's date
        # belongs to the void that was undone.
        current.pop("reversed_on", None)
        # We don't currently expose a draft-journal-entry workflow in the UI/API.
        # Default to posted so accounting reports are meaningful for normal ERP usage.
        current.setdefault("status", "posted")
    elif event_type == "acc.journal_entry.posted":
        current["status"] = "posted"
    elif event_type == "acc.journal_entry.voided":
        current["status"] = "void"
        if data.get("reason"):
            current["void_reason"] = data["reason"]
        if data.get("reversed_on"):
            current["reversed_on"] = data["reversed_on"]
        if data.get("replaced_by"):
            current["replaced_by"] = data["replaced_by"]

    elif event_type == "acc.period.closed":
        current.update({"entity_type": "period", "period": data["period"], "status": "closed"})
    elif event_type == "acc.period.reopened":
        current.update({"entity_type": "period", "period": data["period"], "status": "open"})
    elif event_type == "acc.reconciliation.reopened":
        current.update({"entity_type": "reconciliation", **data})

    elif event_type == "fund.created":
        current.update({"entity_type": "fund", **data, "archived": False})
    elif event_type == "fund.updated":
        current.update({k: v for k, v in data.items() if v is not None})
    elif event_type == "fund.archived":
        current["archived"] = True
    elif event_type == "fund.unarchived":
        current["archived"] = False
    else:
        raise ValueError(f"Unsupported accounting event: {event_type}")

    return current
