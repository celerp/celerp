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


def test_each_filter_is_a_searchable_box_keeping_the_current_choice():
    html = to_xml(history_table(_LEDGER, filters={"hist_who": "Ana"}, filter_url="/x", filter_target="#h"))
    assert html.count('role="combobox"') == 3 and "<select" not in html
    assert 'name="hist_who" data-name="hist_who" value="Ana"' in html
    assert 'data-value="Ana"' in html


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


@pytest.mark.asyncio
async def test_a_list_card_reads_and_pushes_the_list_page_url(ui_client):
    """A List page loads History from its own door; a filter change pushes the List URL, which reopens it."""
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": _LEDGER, "total": 3})), \
         patch("ui.api_client.get_category_labels", new=AsyncMock(return_value={})):
        first = await ui_client.get("/lists/list:QUO-1/history?page=1",
                                    headers={"HX-Current-URL": "http://ui/lists/list:QUO-1?hist_who=Ana"})
        changed = await ui_client.get("/lists/list:QUO-1/history?hist_event=x",
                                      headers={"HX-Current-URL": "http://ui/lists/list:QUO-1"})

    assert '<td class="col-user">Ana</td>' in first.text
    assert '<td class="col-user">Noah</td>' not in first.text
    assert 'hx-get="/lists/list:QUO-1/history' in first.text
    assert 'hx-get="/docs/' not in first.text
    assert changed.headers["HX-Push-Url"] == "/lists/list:QUO-1?hist_event=x"


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["/docs", "/lists"])
@pytest.mark.parametrize("entity_id,page", [("list:QUO-1", "/lists/list:QUO-1"), ("doc:INV-1", "/docs/doc:INV-1")])
async def test_history_pushes_the_page_of_the_record_kind_from_either_door(ui_client, door, entity_id, page):
    """The pushed URL and the filters read from the page follow the record's kind, whichever door served it."""
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": _LEDGER, "total": 3})), \
         patch("ui.api_client.get_category_labels", new=AsyncMock(return_value={})):
        first = await ui_client.get(f"{door}/{entity_id}/history?page=1",
                                    headers={"HX-Current-URL": f"http://ui{page}?hist_who=Ana"})
        changed = await ui_client.get(f"{door}/{entity_id}/history?hist_event=x",
                                      headers={"HX-Current-URL": f"http://ui{page}"})

    assert '<td class="col-user">Noah</td>' not in first.text
    assert f'hx-get="{page}/history' in first.text
    assert changed.headers["HX-Push-Url"] == f"{page}?hist_event=x"


def _paged_ledger(entries):
    async def list_ledger(token, params):
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100))
        return {"items": entries[offset:offset + limit], "total": len(entries)}
    return list_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/docs/doc:INV-1/history?hist_event={ev}",
                                 "/inventory/doc:INV-1/history?hist_event={ev}"])
async def test_history_covers_every_ledger_entry_of_a_long_record(ui_client, url):
    """The oldest event of a record with more entries than one ledger read is still listed and filterable."""
    long_ledger = [_e(i, "doc.updated", {"fields_changed": {"due_date": {"old": "a", "new": "b"}}})
                   for i in range(1300, 1, -1)] + [_e(1, "doc.created")]
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.list_ledger", new=_paged_ledger(long_ledger)), \
         patch("ui.api_client.get_category_labels", new=AsyncMock(return_value={})), \
         patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "doc:INV-1", "name": "N"})), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "USD"})), \
         patch("ui.routes.inventory.deleted_items", new=AsyncMock(return_value=set())):
        r = await ui_client.get(url.format(ev=EVENT_CREATED.replace(" ", "+")))
    assert r.status_code == 200
    assert 'id="evt-1"' in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["abc", "0", "-3", "1%20", "99999"])
@pytest.mark.parametrize("door", ["/lists/list:QUO-1", "/docs/doc:INV-1"])
async def test_the_files_section_reads_any_page_value(ui_client, door, page):
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    doc = {"id": door.rsplit("/", 1)[1], "attachments": []}
    with patch("ui.api_client.get_list", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
        r = await ui_client.get(f"{door}/files/_section?page={page}", headers={"HX-Request": "true"})
    assert r.status_code == 200
