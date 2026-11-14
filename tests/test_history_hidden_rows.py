# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""History leaves out what the reader did not do: posting entries the system writes, the
mechanical steps of a split or transform, and a lot's origin row anywhere but its own page."""

from ui.components.activity import history_rows


def _entry(event_type: str, entity_id: str = "item:child", metadata: dict | None = None, **data) -> dict:
    return {"id": f"e-{event_type}", "event_type": event_type, "entity_id": entity_id,
            "ts": "2026-11-14T10:00:00Z", "actor_id": None, "data": data, "metadata_": metadata or {}}


def test_system_posting_entries_are_left_out():
    rows = history_rows([_entry("acc.journal_entry.posted", "je:1"), _entry("item.created", sku="A")],
                        subject_entity_id="item:child")
    assert [r["event"] for r in rows] == [history_rows([_entry("item.created", sku="A")])[0]["event"]]


def test_an_origin_row_shows_on_its_own_lot_only():
    origin = _entry("item.split_from", "item:child", parent_id="item:mother", parent_sku="M-1")
    assert history_rows([origin], subject_entity_id="item:child")
    assert history_rows([origin], subject_entity_id="item:mother") == []
    assert history_rows([origin]) == []


def test_the_steps_a_split_takes_on_its_own_are_left_out():
    consumed = _entry("item.updated", "item:mother", {"reason": "consumed_by_split"},
                      fields_changed={"quantity": {"old": 5, "new": 3}})
    child = _entry("item.created", "item:child", {"parent_id": "item:mother"}, sku="M-1-A")
    assert history_rows([consumed], subject_entity_id="item:mother") == []
    assert history_rows([child], subject_entity_id="item:child") == []
    consumed["metadata_"] = {}
    child["metadata_"] = {}
    assert history_rows([consumed], subject_entity_id="item:mother")
    assert history_rows([child], subject_entity_id="item:child")


def test_a_merged_lot_names_the_lot_it_went_into():
    from fasthtml.common import to_xml
    merged = _entry("item.source_deactivated", "item:old", merged_into="item:target", merged_into_sku="T-1", original_qty=4)
    (row,) = history_rows([merged], subject_entity_id="item:old")
    assert "T-1" in to_xml(row["content"]) and "item:target" in to_xml(row["content"])
