# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""No route returns a cost to a role without view_inventory_costs in any JSON payload of an event:
its data or its metadata, at the top, nested in objects or inside lists. The routes are walked
from the app's own route table, so a route added later that returns events is covered too, and
an event's ordinary metadata (where it came from, why it was made) still reaches every role."""
from __future__ import annotations

import inspect
import uuid

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from celerp.ai.tools import _api_routes
from celerp.services.activity_redaction import is_cost_key
from test_helpers import invite_user, register_admin

_DOC, _ITEM = "doc:payload-walk", "item:payload-walk"
_AMOUNTS = (91827.36, 82736.45, 73645.54, 64554.63, 55463.72, 46372.81)
_PLAIN_META = {"reason": "category_rename", "automatic": True,
               "backup_actor": {"name": "Restored owner"}, "source_ts": "2026-11-01T00:00:00+00:00"}


def _events(company_id) -> list:
    from celerp.models.ledger import LedgerEntry

    a = _AMOUNTS
    def entry(entity_id, entity_type, event_type, data, meta):
        return LedgerEntry(company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                           event_type=event_type, data=data, metadata_=meta, source="test",
                           idempotency_key=f"payload-walk-{uuid.uuid4()}")
    return [
        entry(_ITEM, "item", "item.updated",
              {"fields_changed": {"name": {"old": "Old", "new": "New"},
                                  "cost_price": {"old": a[0], "new": a[1]}}},
              {**_PLAIN_META, "cost_total": a[2],
               "valued": {"lots": [{"lot": "L1", "unit_cost": a[3]}]}}),
        entry(_DOC, "doc", "doc.updated",
              {"fields_changed": {"notes": {"old": "Before", "new": "After"}}},
              {**_PLAIN_META, "sources": [{"doc": "PO-1", "cost_price": a[4]}],
               "summary": {"totals": {"total_cogs": a[5]}}}),
    ]


_VISIBLE_META = {_ITEM: {**_PLAIN_META, "valued": {"lots": [{"lot": "L1"}]}},
                 _DOC: {**_PLAIN_META, "sources": [{"doc": "PO-1"}], "summary": {"totals": {}}}}


def _event_dicts(value):
    """Every serialized event (a dict naming an event_type and its data) anywhere in a JSON response."""
    if isinstance(value, dict):
        if "event_type" in value and "data" in value:
            yield value
        for v in value.values():
            yield from _event_dicts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _event_dicts(v)


def _cost_paths(value, path=()):
    if isinstance(value, dict):
        for k, v in value.items():
            if is_cost_key(k):
                yield (*path, k)
            yield from _cost_paths(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _cost_paths(v, (*path, i))


def _get_routes(app) -> list[tuple[str, APIRoute]]:
    """(path, route) for every GET route the app serves, from its route table."""
    return [(path, r) for path, methods, r in _api_routes(app)
            if "GET" in methods and not inspect.isasyncgenfunction(r.endpoint)]


def _urls(path: str, route, entry_ids: list[int]) -> list[tuple[str, dict]]:
    """The route called for each seeded event and record: int path parameters take each event's id,
    the others each record's id; a route filtering by record is called unfiltered and for each
    seeded record."""
    params = {}
    query = {p.name for p in route.dependant.query_params}
    if "limit" in query:
        params["limit"] = 100
    if "per_page" in query:
        params["per_page"] = 100
    if "q" in query:
        params["q"] = "payload-walk"
    paths = route.dependant.path_params
    entities = ([None] if not paths else []) + ([_ITEM, _DOC] if ("entity_id" in query or paths) else [])
    out = []
    for entity in entities:
        call = {**params, **({"entity_id": entity} if entity and "entity_id" in query else {})}
        if not paths:
            out.append((path, call))
            continue
        values = entry_ids if any(p.field_info.annotation is int for p in paths) else [entity]
        for v in values:
            out.append((path.format(**{p.name: v for p in paths}), call))
    return list(dict.fromkeys((u, tuple(sorted(c.items()))) for u, c in out))


async def _walk(client, headers, entry_ids, monkeypatch) -> dict[str, list[dict]]:
    """Every event each GET route returns, by route. A route that asks a remote service answers as
    it does when that service is unreachable."""
    from celerp.main import app

    def unreachable(self, request):
        raise httpx.ConnectError("offline", request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unreachable)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unreachable)

    found: dict[str, list[dict]] = {}
    for path, route in _get_routes(app):
        for url, call in _urls(path, route, entry_ids):
            try:
                r = await client.get(url, params=dict(call), headers=headers)
            except Exception:  # a route that cannot answer in a test transaction returns no event
                continue
            if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                continue
            events = list(_event_dicts(r.json()))
            if events:
                found.setdefault(path, []).extend(events)
    return found


async def _setup(client, session):
    from celerp.models.company import Company

    owner = {"Authorization": f"Bearer {await register_admin(client)}"}
    assert (await client.post("/companies/me/demo/reseed", headers=owner)).status_code == 200
    token = await invite_user(client, session, owner, "op@payload-walk.example", "operator")
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    rows = _events(company_id)
    session.add_all(rows)
    await session.flush()
    return owner, {"Authorization": f"Bearer {token}"}, [r.id for r in rows]


@pytest.mark.asyncio
async def test_no_route_returns_an_event_cost_in_data_or_metadata_to_a_role_without_costs(client, session, monkeypatch):
    owner, operator, entry_ids = await _setup(client, session)
    found = await _walk(client, operator, entry_ids, monkeypatch)
    # The walk reached every reader of events: the list and a record's History, a single event,
    # the dashboard activity and its search.
    assert {"/ledger", "/ledger/{entry_id}", "/dashboard/activity", "/dashboard/activity/search"} <= set(found)
    for path, events in found.items():
        for e in events:
            assert not list(_cost_paths(e)), (path, list(_cost_paths(e)))
            assert not any(str(a) in repr(e) for a in _AMOUNTS), (path, e)
            if e.get("entity_id") not in _VISIBLE_META:
                continue
            # Operational metadata reaches every role; only the cost keys inside it go.
            assert e["metadata"] == _VISIBLE_META[e["entity_id"]], (path, e["metadata"])


@pytest.mark.asyncio
async def test_a_role_with_costs_reads_every_event_payload_whole(client, session, monkeypatch):
    owner, operator, entry_ids = await _setup(client, session)
    found = await _walk(client, owner, entry_ids, monkeypatch)
    assert {"/ledger", "/ledger/{entry_id}", "/dashboard/activity", "/dashboard/activity/search"} <= set(found)
    for path, events in found.items():
        shown = repr(events)
        assert all(str(a) in shown for a in _AMOUNTS), path
