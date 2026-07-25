# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Registry guard: every declared EventType must have an emit-time schema.

``emit_event`` looks the event type up in ``EVENT_SCHEMA_MAP`` and raises
``ValueError: Unknown event_type`` when it is absent, so an ``EventType`` member
with no schema entry is an endpoint that fails on every call. A projection
reducer for the event is not enough: the emit never gets that far.

This caught ``doc.items_returned`` (the supplier-return endpoint), which had an
enum member and a reducer but no schema entry.
"""

from __future__ import annotations

from celerp.events.schemas import EVENT_SCHEMA_MAP
from celerp.events.types import EventType


def test_every_event_type_has_an_emit_schema():
    missing = sorted(e.value for e in EventType if e.value not in EVENT_SCHEMA_MAP)
    assert not missing, (
        "EventType members with no EVENT_SCHEMA_MAP entry: emit_event() raises "
        f"'Unknown event_type' for each of these, so any endpoint emitting one always "
        f"fails: {missing}"
    )
