# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from copy import deepcopy


def apply_scanning_event(state: dict, event_type: str, data: dict) -> dict:
    current = deepcopy(state)

    if event_type in {"scan.barcode", "scan.rfid", "scan.nfc"}:
        current.update({
            "entity_type": "scan",
            "last_scan_type": event_type,
            "last_code": data["code"],
            "last_location_id": data.get("location_id"),
            "last_raw": data.get("raw", {}),
        })
    elif event_type == "scan.resolved":
        current.update(
            {
                "entity_type": "scan",
                "last_code": data["code"],
                "resolved_entity_id": data["entity_id"],
                "resolved_entity_type": data["entity_type"],
            }
        )
    else:
        raise ValueError(f"Unsupported scan event: {event_type}")

    return current
