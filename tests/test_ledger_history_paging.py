# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A record's history is read one bounded page at a time, newest first, from a stable event-id
cursor: pages never repeat or skip an event, even while new events are written between reads.
The Event, Field and Who filters and the filtered total are applied by the database, and the
filter choices cover the whole record."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from test_helpers import invite_user, register_admin

_ENTITY = "item:hist-paging"


async def _setup(client, session):
    owner = {"Authorization": f"Bearer {await register_admin(client)}"}
    viewer = {"Authorization": f"Bearer {await invite_user(client, session, owner, 'v@paging.example', 'viewer')}"}
    from celerp.models.company import Company, User
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    admin_id = (await session.execute(select(User.id).where(User.email == "admin@perm.example"))).scalar_one()
    return owner, viewer, company_id, admin_id


def _event(company_id, n: int, *, event_type="item.updated", fields=None, actor_id=None, metadata=None,
           entity_id=_ENTITY):
    from celerp.models.ledger import LedgerEntry
    data = {"fields_changed": fields} if fields is not None else {}
    return LedgerEntry(company_id=company_id, entity_id=entity_id, entity_type="item", event_type=event_type,
                       data=data, actor_id=actor_id, source="test", idempotency_key=f"paging-{uuid.uuid4()}",
                       metadata_=metadata)


async def _write(session, company_id, count: int, start: int = 0, **kw) -> None:
    session.add_all([_event(company_id, start + i, fields={"name": {"old": f"n{start + i}", "new": f"n{start + i + 1}"}}, **kw)
                     for i in range(count)])
    await session.flush()


async def _page(client, headers, **params) -> dict:
    r = await client.get("/ledger", params={"entity_id": _ENTITY, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _walk(client, headers, limit: int, between=None, **filters) -> list[int]:
    ids, before = [], None
    while True:
        body = await _page(client, headers, limit=limit, **filters, **({"before": before} if before else {}))
        page = [e["id"] for e in body["items"]]
        assert len(page) <= limit
        assert page == sorted(page, reverse=True)
        ids += page
        if not body["has_older"]:
            return ids
        before = page[-1]
        if between:
            await between()


@pytest.mark.asyncio
async def test_a_history_over_many_batches_reads_every_event_once_in_pages(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    await _write(session, company_id, 1210)
    ids = await _walk(client, owner, 500)
    assert len(ids) == 1210 == len(set(ids))
    assert ids == sorted(ids, reverse=True)
    first = await _page(client, owner, limit=500)
    assert first["total"] == 1210 and first["newer"] == 0 and first["has_older"]


@pytest.mark.asyncio
async def test_events_written_between_page_reads_neither_repeat_nor_skip(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    await _write(session, company_id, 700)
    before_walk = {e["id"] for e in (await _page(client, owner, limit=500))["items"]}
    written = 0

    async def insert():
        nonlocal written
        await _write(session, company_id, 7, start=10_000 + written)
        written += 7

    ids = await _walk(client, owner, 120, between=insert)
    assert written > 0
    assert len(ids) == len(set(ids)) == 700, "every event that existed when the walk began, once"
    assert before_walk <= set(ids)
    newest = await _page(client, owner, limit=500)
    assert newest["total"] == 700 + written


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 7, 10, 25])
async def test_page_boundaries_and_the_way_back(client, session, limit):
    owner, _, company_id, _ = await _setup(client, session)
    await _write(session, company_id, 25)
    ids = await _walk(client, owner, limit)
    assert len(ids) == 25 == len(set(ids))
    # From the last page, the newer pages lead back to the first, in order.
    last = await _page(client, owner, limit=limit, before=ids[-1] + 1)
    assert last["items"][0]["id"] == ids[-1]
    back, after = [], ids[-1]
    while True:
        body = await _page(client, owner, limit=limit, after=after)
        if not body["items"]:
            break
        page = [e["id"] for e in body["items"]]
        assert page == sorted(page, reverse=True)
        back = page + back
        after = page[0]
        assert body["newer"] == len([i for i in ids if i > page[0]])
    assert back == ids[:-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 501, -1])
async def test_a_page_is_capped(client, session, limit):
    owner, _, _, _ = await _setup(client, session)
    r = await client.get("/ledger", params={"entity_id": _ENTITY, "limit": limit}, headers=owner)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_the_ledger_no_longer_pages_by_offset(client, session):
    owner, _, company_id, _ = await _setup(client, session)
    await _write(session, company_id, 5)
    a = await _page(client, owner, limit=2, offset=2)
    b = await _page(client, owner, limit=2)
    assert [e["id"] for e in a["items"]] == [e["id"] for e in b["items"]]


@pytest.mark.asyncio
async def test_filters_and_filtered_totals_come_from_the_database(client, session):
    owner, _, company_id, admin_id = await _setup(client, session)
    session.add_all(
        [_event(company_id, i, fields={"name": {"old": "a", "new": "b"}}, actor_id=admin_id) for i in range(600)]
        + [_event(company_id, i, fields={"weight": {"old": 1, "new": 2}, "name": {"old": "b", "new": "c"}})
           for i in range(40)]
        + [_event(company_id, i, event_type="item.created") for i in range(3)]
        + [_event(company_id, i, fields={"weight": {"old": 2, "new": 3}}, actor_id=admin_id,
                  metadata={"automatic": True}) for i in range(11)]
        + [_event(company_id, i, fields={"sku": {"old": "x", "new": "y"}},
                  metadata={"backup_actor": {"name": "Old Clerk"}}) for i in range(4)]
        + [_event(company_id, i, fields=["weight"]) for i in range(2)]  # not an object: no field matches
        + [_event(company_id, i, entity_id="item:other", fields={"weight": {"old": 0, "new": 1}}) for i in range(9)]
    )
    await session.flush()
    assert (await _page(client, owner, limit=5))["total"] == 660
    assert (await _page(client, owner, limit=5, event_type="item.created"))["total"] == 3
    weight = await _page(client, owner, limit=500, field="weight")
    assert weight["total"] == 51 and len(weight["items"]) == 51
    assert (await _page(client, owner, limit=5, field=["weight", "sku"]))["total"] == 55
    assert (await _page(client, owner, limit=5, who=str(admin_id)))["total"] == 600
    assert (await _page(client, owner, limit=5, who="auto"))["total"] == 11
    assert (await _page(client, owner, limit=5, who="h:Old Clerk"))["total"] == 4
    assert (await _page(client, owner, limit=5, who="not-a-key"))["total"] == 0
    assert (await _page(client, owner, limit=5, who=str(admin_id), field="name"))["total"] == 600
    ids = await _walk(client, owner, 13, field="weight")
    assert len(ids) == 51 == len(set(ids))
    # Each entry says who made it in the same terms the filter takes.
    keys = {e["actor_key"] for e in (await _page(client, owner, limit=500, who="auto", resolve="true"))["items"]}
    assert keys == {"auto"}

    facets = (await client.get("/ledger/facets", params={"entity_id": _ENTITY}, headers=owner)).json()
    assert set(facets["event_types"]) == {"item.updated", "item.created"}
    assert set(facets["fields"]) == {"name", "weight", "sku"}
    by_key = {a["actor_key"]: a for a in facets["actors"]}
    assert set(by_key) == {str(admin_id), "auto", "h:Old Clerk", ""}
    assert by_key[str(admin_id)]["actor_name"] == "Admin"
    assert by_key["h:Old Clerk"]["actor_historical"] is True


@pytest.mark.asyncio
async def test_cost_stays_hidden_from_a_role_without_cost_permission(client, session):
    owner, viewer, company_id, _ = await _setup(client, session)
    session.add_all([_event(company_id, i, fields={"cost_price": {"old": 1, "new": 2}, "name": {"old": "a", "new": "b"}})
                     for i in range(3)] + [_event(company_id, 9, fields={"cost_price": {"old": 2, "new": 5}})])
    await session.flush()
    seen = await _page(client, viewer, limit=50)
    assert seen["total"] == 4
    assert all("cost_price" not in (e["data"].get("fields_changed") or {}) for e in seen["items"])
    # The cost field is no filter choice and filters nothing in for this role.
    assert (await _page(client, viewer, limit=50, field="cost_price"))["total"] == 0
    facets = (await client.get("/ledger/facets", params={"entity_id": _ENTITY}, headers=viewer)).json()
    assert "cost_price" not in facets["fields"]
    assert (await _page(client, owner, limit=50, field="cost_price"))["total"] == 4
    assert "cost_price" in (await client.get("/ledger/facets", params={"entity_id": _ENTITY}, headers=owner)).json()["fields"]


@pytest.mark.asyncio
async def test_facets_follow_the_read_policy_and_need_a_record(client, session):
    owner, viewer, company_id, _ = await _setup(client, session)
    session.add_all([_event(company_id, 1, event_type="je.posted", fields={"memo": {"old": "a", "new": "b"}}),
                     _event(company_id, 2, fields={"name": {"old": "a", "new": "b"}})])
    await session.flush()
    facets = (await client.get("/ledger/facets", params={"entity_id": _ENTITY}, headers=viewer)).json()
    assert facets["event_types"] == ["item.updated"] and facets["fields"] == ["name"]
    assert (await client.get("/ledger/facets", headers=owner)).status_code == 422


@pytest.mark.asyncio
async def test_a_large_history_page_is_fast(client, session):
    """A 5,000-event record: the newest page, a filtered page, a page near the oldest and the
    filter choices each cost one bounded query, whatever the record's length."""
    owner, _, company_id, _ = await _setup(client, session)
    await _write(session, company_id, 5000)
    oldest = (await _page(client, owner, limit=1, after=0))["items"][0]["id"]
    calls = {
        "first page (50)": lambda: _page(client, owner, limit=50),
        "filtered page (50, field=name)": lambda: _page(client, owner, limit=50, field="name"),
        "page near the oldest (50)": lambda: _page(client, owner, limit=50, before=oldest + 60),
        "filter choices": lambda: client.get("/ledger/facets", params={"entity_id": _ENTITY}, headers=owner),
    }
    timings = {}
    for name, call in calls.items():
        start = time.perf_counter()
        await call()
        timings[name] = time.perf_counter() - start
    if out := os.environ.get("HISTORY_BENCH_OUT"):
        Path(out).write_text("5,000-event record\n" + "".join(f"{k}: {v * 1000:.1f} ms\n" for k, v in timings.items()))
    assert max(timings.values()) < 2.0, timings
