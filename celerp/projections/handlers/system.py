# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def apply_system_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    # System events are primarily for audit; projections are optional.
    if event_type == "sys.company.created":
        current.update({"entity_type": "company", **data})
    elif event_type == "sys.user.created":
        current.update({"entity_type": "user", **data, "is_active": True})
    elif event_type == "sys.user.deactivated":
        current.update({"entity_type": "user", "is_active": False, **data})
    elif event_type == "sys.api_key.created":
        current.update({"entity_type": "api_key", "status": "active", **data})
    elif event_type == "sys.api_key.revoked":
        current.update({"entity_type": "api_key", "status": "revoked", **data})
    elif event_type == "sys.backup.created":
        current.update({"entity_type": "backup", **data})
    elif event_type == "sys.migration.applied":
        current.update({"entity_type": "migration", **data})
    else:
        raise ValueError(f"Unsupported system event: {event_type}")

    return current
