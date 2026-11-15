# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""History rows for lot lifecycle events. What History leaves out is left out by the ledger read
(test_history_visibility.py)."""

from ui.components.activity import history_rows


def _entry(event_type: str, entity_id: str = "item:child", metadata: dict | None = None, **data) -> dict:
    return {"id": f"e-{event_type}", "event_type": event_type, "entity_id": entity_id,
            "ts": "2026-11-14T10:00:00Z", "actor_id": None, "data": data, "metadata_": metadata or {}}


def test_a_merged_lot_names_the_lot_it_went_into():
    from fasthtml.common import to_xml
    merged = _entry("item.source_deactivated", "item:old", merged_into="item:target", merged_into_sku="T-1", original_qty=4)
    (row,) = history_rows([merged])
    assert "T-1" in to_xml(row["content"]) and "item:target" in to_xml(row["content"])
