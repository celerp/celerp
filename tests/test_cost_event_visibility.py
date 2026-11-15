# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A cost price shows only to users who can see costs, whatever shape its event was recorded in.

An event that is a cost as a whole (a cost price set on an item, a lot's restated cost, a landed
cost allocated to a lot) reaches anyone else as its few plain fields only: every other field, an
amount under any name, a field from an older shape or a field nobody knows yet, stays out. Its
metadata loses every cost key at any depth. Activity search finds an event only by what the same
reader is shown of it, so an amount or a field name that stays out is never a match either.

Read on a registered company with its demo data, through the raw API responses."""
from __future__ import annotations

import json
import random
import uuid

import pytest
from sqlalchemy import select

from celerp.services.activity_redaction import is_cost_key, serialize_event
from test_helpers import invite_user, register_admin

_COST_NOW, _COST_BEFORE = 54321.09, 43210.98
_LEGACY_PRICE, _LEGACY_OLD, _EXTRA_AMOUNT, _META_COST = 76543.21, 65432.17, 32109.87, 21098.76
_EXTRA_TEXT = "zq-supplier-note"
_HIDDEN_NAMES = ("legacy_total", "supplier_rate", "new_price", "old_price")
_PLAIN_META = {"reason": "price review", "source_ts": "2026-11-01T00:00:00+00:00"}


async def _company(client, session):
    """Owner and a role without costs on a registered company, and one of its demo items with a
    cost price set through the API plus the same cost recorded in an older shape."""
    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry

    owner = {"Authorization": f"Bearer {await register_admin(client)}"}
    operator = {"Authorization": f"Bearer {await invite_user(client, session, owner, 'op@cev.example', 'operator')}"}
    manager = {"Authorization": f"Bearer {await invite_user(client, session, owner, 'mgr@cev.example', 'manager')}"}
    items = (await client.get("/items", params={"limit": 50}, headers=owner)).json()
    items = items.get("items", items) if isinstance(items, dict) else items
    assert items, "the registered company carries demo items"
    item_id = items[0]["id"]
    r = await client.post(f"/items/{item_id}/price", json={"price_type": "cost_price", "new_price": _COST_BEFORE},
                          headers=owner)
    assert r.status_code == 200, r.text
    r = await client.post(f"/items/{item_id}/price", json={"price_type": "cost_price", "new_price": _COST_NOW},
                          headers=owner)
    assert r.status_code == 200, r.text
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    session.add(LedgerEntry(
        company_id=company_id, entity_id=item_id, entity_type="item", event_type="item.pricing.set",
        data={"price_type": "cost", "price": _LEGACY_PRICE, "old_price": _LEGACY_OLD,
              "legacy_total": {"amount": _EXTRA_AMOUNT}, "note": _EXTRA_TEXT,
              "fields_changed": {"supplier_rate": {"old": _LEGACY_OLD, "new": _LEGACY_PRICE}}},
        metadata_={**_PLAIN_META, "valued": {"lines": [{"lot": "L1", "cost_price": _META_COST}]}},
        source="import", idempotency_key=f"cev-{uuid.uuid4()}"))
    await session.flush()
    return owner, operator, manager, item_id


_AMOUNTS = (_COST_NOW, _COST_BEFORE, _LEGACY_PRICE, _LEGACY_OLD, _EXTRA_AMOUNT, _META_COST)


def _cost_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event_type"] == "item.pricing.set"
            and e["data"].get("price_type") in ("cost_price", "cost")]


async def _feeds(client, headers, item_id) -> dict[str, list[dict]]:
    """Every raw event feed a reader has, for the item's events."""
    feeds = {}
    for name, (path, params, key) in {
        "ledger": ("/ledger", {"entity_id": item_id, "limit": 500}, "items"),
        "history": ("/ledger", {"entity_id": item_id, "history": True, "limit": 500}, "items"),
        "dashboard activity": ("/dashboard/activity", {"limit": 100}, "activities"),
    }.items():
        r = await client.get(path, params=params, headers=headers)
        assert r.status_code == 200, (name, r.text[:300])
        feeds[name] = [e for e in r.json()[key] if e["entity_id"] == item_id]
    feeds["single event"] = []
    for e in feeds["ledger"]:
        r = await client.get(f"/ledger/{e['id']}", headers=headers)
        assert r.status_code == 200, r.text[:300]
        feeds["single event"].append(r.json())
    return feeds


async def _search_hits(client, headers, q: str, item_id: str) -> list[int]:
    r = await client.get("/dashboard/activity/search", params={"q": q, "per_page": 200}, headers=headers)
    assert r.status_code == 200, r.text[:300]
    return [e["id"] for e in r.json()["activities"] if e["entity_id"] == item_id]


@pytest.mark.asyncio
async def test_a_cost_price_event_shows_only_its_price_type_to_a_role_without_costs(client, session):
    owner, operator, manager, item_id = await _company(client, session)
    for name, events in (await _feeds(client, operator, item_id)).items():
        cost = _cost_events(events)
        assert len(cost) >= (3 if name != "dashboard activity" else 1), name
        for e in cost:
            assert set(e["data"]) <= {"price_type", "cost_redacted"}, (name, e["data"])
            assert e["data"]["cost_redacted"] is True
        text = json.dumps(events)
        assert not any(f"{a}" in text for a in _AMOUNTS), name
        legacy = [e for e in cost if e["data"]["price_type"] == "cost"]
        for e in legacy:
            assert e["metadata"] == {**_PLAIN_META, "valued": {"lines": [{"lot": "L1"}]}}, (name, e["metadata"])
    for name, events in (await _feeds(client, manager, item_id)).items():
        text = json.dumps(events)
        assert f"{_COST_NOW}" in text and f"{_META_COST}" in text, name


@pytest.mark.asyncio
async def test_activity_search_finds_a_cost_only_for_a_role_that_sees_it(client, session):
    owner, operator, manager, item_id = await _company(client, session)
    # Search reads event data; the metadata cost is no search term for anyone.
    needles = [f"{a}" for a in _AMOUNTS if a != _META_COST] + [_EXTRA_TEXT, *_HIDDEN_NAMES]
    for q in needles:
        assert await _search_hits(client, operator, q, item_id) == [], q
        assert await _search_hits(client, manager, q, item_id), q
    # What the reader is shown stays searchable.
    assert await _search_hits(client, operator, "price_type", item_id)


@pytest.mark.asyncio
async def test_a_field_changed_inside_a_cost_event_is_no_history_filter_without_costs(client, session):
    owner, operator, manager, item_id = await _company(client, session)
    for headers, shown in ((operator, False), (manager, True)):
        r = await client.get("/ledger", params={"entity_id": item_id, "field": "supplier_rate"}, headers=headers)
        assert r.status_code == 200 and (r.json()["total"] > 0) is shown, r.text[:300]
        facets = (await client.get("/ledger/facets", params={"entity_id": item_id}, headers=headers)).json()
        assert ("supplier_rate" in facets["fields"]) is shown, facets


# ── Seeded generative oracle ────────────────────────────────────────────────────────────────

_SAFE = {"item.pricing.set": {"price_type"}, "item.cost_adjusted": {"manufacturing_order_id"},
         "item.landed_cost.allocated": {"source_bill_id", "kind"}}
_KEYS = ("price", "amount", "new_price", "old_price", "price_type", "kind", "source_bill_id",
         "manufacturing_order_id", "cost_price", "unit_cost", "landed_costs", "note", "lines",
         "fields_changed", "qté", "価格", "rate")


def _token(rnd: random.Random) -> str:
    return f"tk{rnd.randrange(16**8):08x}"


def _value(rnd: random.Random, depth: int):
    roll = rnd.random()
    if depth == 0 or roll < 0.3:
        return rnd.choice([_token(rnd), rnd.randint(10**5, 10**6) / 100, f"{rnd.randint(10**5, 10**6)}",
                           json.dumps({"amount": _token(rnd)})])
    if roll < 0.55:
        return [_value(rnd, depth - 1) for _ in range(rnd.randint(1, 3))]
    return {rnd.choice(_KEYS + (_token(rnd),)): _value(rnd, depth - 1) for _ in range(rnd.randint(1, 3))}


def _event(rnd: random.Random) -> tuple[str, dict]:
    event_type = rnd.choice(["item.pricing.set", "item.pricing.set", "item.cost_adjusted",
                             "item.landed_cost.allocated", "item.updated"])
    data = {rnd.choice(_KEYS + (_token(rnd),)): _value(rnd, 3) for _ in range(rnd.randint(1, 6))}
    if event_type == "item.pricing.set":
        data["price_type"] = rnd.choice(["cost_price", "cost", "landed_price", "Cost Price", "cost_total",
                                         "retail_price", "wholesale_price"])
    return event_type, data


def _visible_text(value) -> list[str]:
    """Every key and scalar a search can match in a shown payload, as Postgres renders them."""
    if isinstance(value, dict):
        return [t for k, v in value.items() if k != "cost_redacted" for t in [k, *_visible_text(v)]]
    if isinstance(value, list):
        return [t for v in value for t in _visible_text(v)]
    return [value if isinstance(value, str) else json.dumps(value)]


def _all_text(value) -> list[str]:
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in [k, *_all_text(v)]]
    if isinstance(value, list):
        return [t for v in value for t in _all_text(v)]
    return [value if isinstance(value, str) else json.dumps(value)]


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


@pytest.mark.asyncio
async def test_random_cost_events_show_and_match_only_their_plain_fields(client, session):
    """Random payloads of cost events, any keys at any depth: a role without costs reads at most
    the event's plain fields, never a cost key anywhere, and a search over the data matches an
    event exactly when the needle is in what that role is shown (a role that sees costs is shown
    the whole payload)."""
    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry
    from celerp.services.activity_redaction import event_data_matches

    owner, operator, manager, item_id = await _company(client, session)
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    rnd = random.Random(20261115)
    entries = []
    for n in range(40):
        event_type, data = _event(rnd)
        entries.append(LedgerEntry(company_id=company_id, entity_id=f"item:oracle-{n}", entity_type="item",
                                   event_type=event_type, data=data, metadata_={"note": _value(rnd, 2)},
                                   source="test", idempotency_key=f"oracle-{uuid.uuid4()}"))
    session.add_all(entries)
    await session.flush()
    feed = {e["id"]: e for e in (await client.get(
        "/ledger", params={"entity_type": "item", "limit": 500}, headers=operator)).json()["items"]}
    for entry in entries:
        shown = feed[entry.id]
        assert not any(is_cost_key(k) for k in _keys(shown["data"]) - {"cost_redacted"}), shown["data"]
        assert not any(is_cost_key(k) for k in _keys(shown["metadata"])), shown["metadata"]
        expected = serialize_event(entry, {}, "operator")
        assert (shown["data"], shown["metadata"]) == (expected["data"], expected["metadata"])
        safe = _SAFE.get(entry.event_type)
        if entry.event_type == "item.pricing.set" and entry.data["price_type"] in ("retail_price", "wholesale_price"):
            safe = None
        if safe is not None:
            assert set(shown["data"]) <= safe | {"cost_redacted"}, (entry.event_type, shown["data"])
        visible = [t.lower() for t in _visible_text(shown["data"])]
        needles = rnd.sample(_all_text(entry.data), min(6, len(_all_text(entry.data))))
        for needle in needles:
            hit = (await session.execute(select(LedgerEntry.id).where(
                LedgerEntry.id == entry.id, event_data_matches(needle, {}, "operator")))).first() is not None
            assert hit is any(needle.lower() in t for t in visible), (entry.event_type, needle, shown["data"])
            hit = (await session.execute(select(LedgerEntry.id).where(
                LedgerEntry.id == entry.id, event_data_matches(needle, {}, "manager")))).first() is not None
            assert hit is any(needle.lower() in t.lower() for t in _all_text(entry.data)), needle
