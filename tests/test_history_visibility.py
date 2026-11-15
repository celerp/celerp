# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What History and the activity feed leave out (posting entries the system writes, the mechanical
steps of a split or transform, a lot's origin row away from its own page) is left out by the
database, so the rows, the totals, the pages and the filter choices all count the same events."""
from __future__ import annotations

import pytest

from test_ledger_history_paging import _ENTITY, _event, _setup
from ui.components.activity import filter_history, history_rows

_CHILD = "item:hist-visibility-child"


def _hidden(company_id) -> list:
    """One of every event History leaves out, all on the record under test."""
    return [
        _event(company_id, 0, event_type="acc.journal_entry.posted"),
        _event(company_id, 0, fields={"quantity": {"old": 5, "new": 3}}, metadata={"reason": "consumed_by_split"}),
        _event(company_id, 0, fields={"weight": {"old": 2, "new": 1}}, metadata={"reason": "split_parent"}),
        _event(company_id, 0, event_type="item.created", metadata={"parent_id": "item:mother"}),
        _event(company_id, 0, event_type="item.split_from", entity_id=_CHILD),
    ]


async def _history(client, headers, entity_id=_ENTITY, **params) -> dict:
    r = await client.get("/ledger", params={"entity_id": entity_id, "history": "true", **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_a_filter_matching_only_left_out_events_finds_nothing(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    session.add_all(_hidden(company_id))
    await session.flush()
    for query in ({}, {"event_type": "item.updated"}, {"field": "quantity"}, {"event_type": "item.created"}):
        body = await _history(client, owner, **query)
        assert (body["total"], body["items"], body["has_older"]) == (0, [], False), query
    facets = (await client.get("/ledger/facets", params={"entity_id": _ENTITY}, headers=owner)).json()
    assert facets["event_types"] == [] and facets["fields"] == []
    # The whole ledger still holds them for an integration reading it as it is.
    assert (await client.get("/ledger", params={"entity_id": _ENTITY}, headers=owner)).json()["total"] == 4


@pytest.mark.asyncio
async def test_the_total_and_the_pages_count_the_events_shown(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    for n in range(7):
        session.add(_event(company_id, n, fields={"name": {"old": f"n{n}", "new": f"n{n + 1}"}}))
        session.add_all(_hidden(company_id)[:4])
    await session.flush()
    shown, before = [], None
    while True:
        body = await _history(client, owner, limit=3, **({"before": before} if before else {}))
        assert body["total"] == 7
        assert body["newer"] == len(shown)
        rows = filter_history(history_rows(body["items"]), {})
        assert {r["id"] for r in rows} == {e["id"] for e in body["items"]}
        shown += [e["id"] for e in body["items"]]
        if not body["has_older"]:
            break
        before = shown[-1]
    assert len(shown) == len(set(shown)) == 7


@pytest.mark.asyncio
async def test_an_origin_row_shows_on_its_own_lot_and_nowhere_else(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    session.add_all(_hidden(company_id))
    await session.flush()
    own = await _history(client, owner, entity_id=_CHILD)
    assert [e["event_type"] for e in own["items"]] == ["item.split_from"] and own["total"] == 1
    feed = (await client.get("/dashboard/activity", params={"limit": 100}, headers=owner)).json()["activities"]
    assert [a for a in feed if a["entity_id"] in (_ENTITY, _CHILD)] == []
    search = (await client.get("/dashboard/activity/search", params={"q": "hist-"}, headers=owner)).json()
    assert (search["total"], search["activities"]) == (0, [])


@pytest.mark.asyncio
async def test_an_event_whose_changes_read_as_nothing_still_shows_one_row(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    session.add(_event(company_id, 0, fields={"notes": {"old": "", "new": None}, "name": {"old": "a", "new": "b"}}))
    session.add(_event(company_id, 1, fields={"notes": {"old": None, "new": " "}}))
    await session.flush()
    for filters in ({}, {"field": "notes"}):
        body = await _history(client, owner, **({"field": "notes"} if filters else {}))
        rows = filter_history(history_rows(body["items"]), filters)
        assert body["total"] == 2 and {r["id"] for r in rows} == {e["id"] for e in body["items"]}
