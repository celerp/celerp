# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GEN-014: the History card is one table with a row per field change.

Columns Date, Who, Event, Field, Old value, New value. An empty value reads "--", never "none";
an event that changed no field (created, finalized, voided) is one row. Field names and event
names are translated, values are shown as stored. Event, Field and Who filters live in the URL.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.components.activity import field_changes, filter_history, history_rows, history_table
from ui.i18n import t

_TS = "2026-10-28T09:15:00+00:00"
EVENT_FINALIZED, EVENT_CREATED = t("event.doc.finalized", "en"), t("event.doc.created", "en")


def _e(eid, event_type, data=None, actor="Noah"):
    return {"id": eid, "event_type": event_type, "ts": _TS, "actor_name": actor,
            "entity_id": "doc:INV-1", "data": data or {}}


_LEDGER = [
    _e(3, "doc.updated", {"fields_changed": {
        "due_date": {"old": "2026-11-01", "new": "2026-11-15"},
        "customer_note": {"old": None, "new": "Thanks"},
        "updated_at": {"old": "a", "new": "b"},  # system field: never a row
    }}, actor="Ana"),
    _e(2, "doc.finalized"),
    _e(1, "doc.created"),
]


def test_each_field_change_is_its_own_row_and_an_empty_value_reads_dashes():
    rows = history_rows(_LEDGER)
    updated = [r for r in rows if r["id"] == 3]
    assert [(r["field"], r["old"], r["new"]) for r in updated] == [
        ("Due date", "2026-11-01", "2026-11-15"), ("Customer note", "--", "Thanks")]
    assert all(r["who"] == "Ana" and r["when"] == "2026-10-28 09:15" for r in updated)
    assert [r["first"] for r in updated] == [True, False]


def test_an_event_that_changed_no_field_is_one_row():
    rows = history_rows(_LEDGER)
    for eid in (1, 2):
        (row,) = [r for r in rows if r["id"] == eid]
        assert (row["field"], row["old"]) == ("--", "--")
    assert [r["event"] for r in rows if r["id"] in (1, 2)] == [EVENT_FINALIZED, EVENT_CREATED]


def test_line_changes_name_the_item_and_show_quantity_and_price():
    changes = field_changes({"fields_changed": {"line_items": {
        "old": [{"line_id": "a", "name": "Ruby", "quantity": 1, "unit_price": 100}],
        "new": [{"line_id": "a", "name": "Ruby", "quantity": 2, "unit_price": 100},
                {"line_id": "b", "name": "Pearl", "quantity": 3, "unit_price": 5}]}}}, "doc.updated", "USD")
    assert [c[0] for c in changes] == ["Item: Ruby", "Item: Pearl"]
    assert changes[0][1].startswith("1 @") and changes[0][2].startswith("2 @")
    assert changes[1][1] == "--"


def test_cost_fields_the_api_stripped_never_appear():
    """The API removes cost amounts for roles that may not see them; nothing is invented here."""
    changes = field_changes({"fields_changed": {"retail_price": {"old": 10, "new": 12}}}, "item.updated", "USD")
    assert [c[0] for c in changes] and all("cost" not in c[0].lower() for c in changes)


def test_filters_match_event_field_and_who():
    rows = history_rows(_LEDGER)
    assert {r["id"] for r in filter_history(rows, {"hist_who": "Ana"})} == {3}
    assert [r["field"] for r in filter_history(rows, {"hist_field": "Due date"})] == ["Due date"]
    assert filter_history(rows, {"hist_who": "Nobody"}) == []


def test_the_table_has_the_six_columns_and_offers_the_filters():
    html = to_xml(history_table(_LEDGER, filter_url="/docs/doc:INV-1/history", filter_target="#h"))
    for header in ("Date", "Who", "Event", "Field", "Old value", "New value"):
        assert f">{header}<" in html
    assert 'name="hist_event"' in html and 'name="hist_field"' in html and 'name="hist_who"' in html
    assert ">Ana<" in html and ">none<" not in html


def test_a_filter_with_no_match_says_so():
    html = to_xml(history_table(_LEDGER, filters={"hist_who": "Nobody"}, filter_url="/x"))
    assert "No history matches these filters." in html


# ── The document card ────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


@pytest.mark.asyncio
async def test_the_document_card_reads_filters_from_the_page_url_and_pushes_changes(ui_client):
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": _LEDGER, "total": 3})), \
         patch("ui.api_client.get_category_labels", new=AsyncMock(return_value={})):
        first = await ui_client.get("/docs/doc:INV-1/history?page=1",
                                    headers={"HX-Current-URL": "http://ui/docs/doc:INV-1?hist_who=Ana"})
        changed = await ui_client.get("/docs/doc:INV-1/history?hist_field=Due+date",
                                      headers={"HX-Current-URL": "http://ui/docs/doc:INV-1?tab=x"})

    assert first.status_code == 200 and "HX-Push-Url" not in first.headers
    assert '<td class="col-user">Ana</td>' in first.text
    assert '<td class="col-user">Noah</td>' not in first.text  # created and finalized were by Noah
    assert changed.headers["HX-Push-Url"] == "/docs/doc:INV-1?tab=x&hist_field=Due+date"
    assert '<td class="col-field">Due date</td>' in changed.text
    assert '<td class="col-field">Customer note</td>' not in changed.text
