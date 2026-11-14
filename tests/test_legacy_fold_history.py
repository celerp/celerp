# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Old preset fields (Weight (g), Origin Country) fold onto their core fields event by event.

Earlier releases kept a lot's weight in grams and its origin under their own attribute keys,
and a client of such a release can still write them. Each event's write of an old key lands on
the core field at the moment it is applied, in event order: a later update or clear of the old
key updates or clears the core field. A core field written in the same event wins over the old
key there; any later event still applies. A weight that is not a finite number stays in the
attributes as given.

Histories are generated from fixed seeds and replayed through the projection, then compared
with a reference fold that keeps only the four values involved.
"""
from __future__ import annotations

import random
from decimal import Decimal, InvalidOperation

import pytest
from sqlalchemy import select

_SEEDS = range(60)
_HISTORIES_PER_SEED = 25
_ABSENT = object()


def _grams(value):
    """A finite number of grams, read by decimal parsing; None for anything else."""
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        return None
    try:
        number = Decimal(value.strip()) if isinstance(value, str) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or abs(number) > Decimal("1.7976931348623157e308"):
        return None
    return float(number)


def _weight_value(rng):
    return rng.choice([rng.choice([250, 500, 750, 1000]), str(rng.randint(1, 900)), "12.5",
                       "abc", "nan", "inf", True, None, "", "1e400"])


def _origin_value(rng):
    return rng.choice(["Thailand", "Vietnam", "Japan", None, ""])


def _writes(rng, *, create: bool) -> dict:
    out = {}
    if rng.random() < .6:
        out["weight_g"] = _weight_value(rng)
    if rng.random() < .3:
        out["weight"] = rng.choice([1.5, 2.0, 3.25] if create else [1.5, 2.0, None, ""])
    if rng.random() < .5:
        out["origin_country"] = _origin_value(rng)
    if rng.random() < .3:
        out["country_of_origin"] = _origin_value(rng)
    if rng.random() < .3:
        out["brand"] = rng.choice(["Acme", "Zen"])
    return out


def _history(rng) -> list[tuple[str, dict]]:
    """(kind, writes) per event: a create, then edits and CSV upserts, each of which can set or
    clear any of the fields."""
    events = [("item.created", _writes(rng, create=True))]
    for _ in range(rng.randint(1, 8)):
        kind = rng.choice(["item.updated", "item.updated", "item.updated", "item.patched"])
        events.append((kind, _writes(rng, create=False) or {"brand": "Other"}))
    return events


def _event(kind: str, writes: dict) -> dict:
    """The event data each producer records for these writes."""
    if kind == "item.updated":
        return {"fields_changed": {k: {"old": None, "new": v} for k, v in writes.items()}}
    core = {k: v for k, v in writes.items() if k == "weight"}
    if core.get("weight") not in (None, ""):
        core["weight_unit"] = "kilogram"
    attrs = {k: v for k, v in writes.items() if k != "weight"}
    if kind == "item.created":
        return {"sku": "S", "name": "n", **core, "attributes": attrs}
    return {**core, **attrs}  # a CSV upsert carries its columns at the top level


def _reference(history) -> tuple:
    """(weight, weight_unit, country_of_origin, weight_g left in attributes) after the history."""
    weight = unit = origin = _ABSENT
    left = _ABSENT
    for kind, writes in history:
        given = writes if kind != "item.created" else {k: v for k, v in writes.items() if v not in (None, "")}
        if "weight" in given:
            if given["weight"] in (None, ""):
                weight = _ABSENT
            else:  # a create or CSV upsert gives the unit with it; an edit keeps the unit held
                weight, unit = given["weight"], unit if kind == "item.updated" else "kilogram"
        if "country_of_origin" in given:
            origin = _ABSENT if given["country_of_origin"] in (None, "") else given["country_of_origin"]
        if "origin_country" in given and "country_of_origin" not in given:
            origin = _ABSENT if given["origin_country"] in (None, "") else given["origin_country"]
        if "weight_g" in given:
            value = given["weight_g"]
            if "weight" in given:
                left = _ABSENT
            elif value in (None, ""):
                weight, left = _ABSENT, _ABSENT
            elif _grams(value) is None:
                left = value
            else:
                weight, unit, left = _grams(value), "gram", _ABSENT
    norm = lambda v: None if v is _ABSENT else v  # noqa: E731
    return norm(weight), norm(unit) if weight is not _ABSENT else None, norm(origin), left


def _replay(history, apply) -> dict:
    state: dict = {}
    for kind, writes in history:
        state = apply(state, kind, _event(kind, writes))
    return state


def _seen(state: dict) -> tuple:
    attrs = state.get("attributes") or {}
    assert "origin_country" not in attrs and "weight_g" not in state, state
    empty = lambda v: None if v in (None, "") else v  # noqa: E731  (a CSV upsert stores an empty cell as "")
    weight = empty(state.get("weight"))
    return weight, state.get("weight_unit") if weight is not None else None, \
        empty(attrs.get("country_of_origin")), attrs.get("weight_g", _ABSENT)


def _disagreements(apply) -> list:
    out = []
    for seed in _SEEDS:
        rng = random.Random(seed)
        for n in range(_HISTORIES_PER_SEED):
            history = _history(rng)
            if _seen(_replay(history, apply)) != _reference(history):
                out.append((seed, n, history))
    return out


def test_every_history_folds_as_the_reference_does():
    from celerp_inventory.projections import apply_item_event
    bad = _disagreements(apply_item_event)
    assert bad == [], bad[:3]


def test_a_core_value_never_vetoes_a_later_old_key_write():
    """The reference catches a fold that lets a held core value veto later old-key writes."""
    from celerp.services.field_schema import LEGACY_CATEGORY_FIELDS
    from celerp_inventory.projections import apply_item_event

    def vetoing(state, kind, data):
        after = apply_item_event(state, kind, data)
        if state.get("weight") not in (None, "") and "weight" not in data.get("fields_changed", data):
            after["weight"], after["weight_unit"] = state["weight"], state.get("weight_unit")
        return after

    assert LEGACY_CATEGORY_FIELDS["weight_g"] == "weight"
    assert _disagreements(vetoing) != []


def test_weight_g_500_then_750_then_1000_reads_1000_and_a_clear_clears_it():
    from celerp_inventory.projections import apply_item_event
    history = [("item.created", {"weight_g": 500, "origin_country": "Thailand"}),
               ("item.updated", {"weight_g": 750}), ("item.updated", {"weight_g": 1000})]
    state = _replay(history, apply_item_event)
    assert (state["weight"], state["weight_unit"]) == (1000.0, "gram")
    state = apply_item_event(state, "item.updated", _event("item.updated", {"weight_g": None, "origin_country": ""}))
    assert state.get("weight") is None and "country_of_origin" not in state.get("attributes", {})


def test_a_core_write_in_the_same_event_wins_and_a_later_old_key_write_still_applies():
    from celerp_inventory.projections import apply_item_event
    history = [("item.created", {"weight": 2.0, "weight_g": 500}), ("item.updated", {"weight_g": 900})]
    first = _replay(history[:1], apply_item_event)
    assert (first["weight"], first["weight_unit"], first.get("attributes")) == (2.0, "kilogram", {})
    assert (_replay(history, apply_item_event)["weight"], _replay(history, apply_item_event)["weight_unit"]) == (900.0, "gram")


async def _headers(client) -> dict:
    from test_entity_files import _headers as register
    return await register(client)


async def _old_history(client, headers, sku: str) -> str:
    """A lot an older client kept writing Weight (g) on: 500, then 750, then 1000."""
    created = await client.post("/items", headers=headers, json={
        "sku": sku, "name": "Old preset lot", "quantity": 1, "sell_by": "piece",
        "attributes": {"weight_g": 500, "origin_country": "Thailand"}})
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]
    for grams in (750, 1000):
        edited = await client.patch(f"/items/{item_id}", headers=headers,
                                    json={"fields_changed": {"weight_g": {"old": None, "new": grams}}})
        assert edited.status_code == 200, edited.text
    return item_id


@pytest.mark.asyncio
async def test_a_rebuild_reads_the_last_old_key_write_and_a_second_rebuild_changes_nothing(client, session):
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    headers = await _headers(client)
    item_id = await _old_history(client, headers, "OLD-HIST")
    live = (await client.get(f"/items/{item_id}", headers=headers)).json()
    assert (live["weight"], live["weight_unit"], live["country_of_origin"]) == (1000, "gram", "Thailand")

    states = []
    for _ in range(2):
        await lock_company(session, headers["X-Company-Id"])
        await ProjectionEngine.rebuild(session, headers["X-Company-Id"])
        await session.commit()
        session.expire_all()
        row = (await session.execute(select(Projection).where(Projection.entity_id == item_id))).scalar_one()
        states.append(dict(row.state))
        session.expunge_all()  # the next rebuild deletes and rewrites the row this session read
    assert states[0] == states[1]
    assert (states[0]["weight"], states[0]["weight_unit"]) == (1000.0, "gram")


@pytest.mark.asyncio
async def test_an_upgrade_from_the_previous_projection_semantics_reads_the_last_write(client, session):
    """The rows an earlier release left (the old key in the attributes, no core weight) are
    rebuilt by the upgrade start, and the lot reads its last Weight (g) write."""
    from sqlalchemy.orm.attributes import flag_modified

    from celerp.migrations._data_reconcile import PROJECTION_VERSION_KEY, set_meta
    from celerp.models.projections import Projection
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS_KEY, run_upgrade_guard

    headers = await _headers(client)
    item_id = await _old_history(client, headers, "OLD-UPGRADE")
    row = (await session.execute(select(Projection).where(Projection.entity_id == item_id))).scalar_one()
    old = {k: v for k, v in row.state.items() if k not in ("weight", "weight_unit")}
    old["attributes"] = {"weight_g": 1000, "origin_country": "Thailand"}
    row.state = old
    flag_modified(row, "state")
    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_SEMANTICS_KEY, "2"))
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_VERSION_KEY, "0.0.1"))
    await session.commit()

    summary = await run_upgrade_guard(session)
    await session.commit()
    assert summary["rebuilt"] is True
    session.expire_all()
    row = (await session.execute(select(Projection).where(Projection.entity_id == item_id))).scalar_one()
    assert (row.state["weight"], row.state["weight_unit"]) == (1000.0, "gram")
    assert row.state["attributes"] == {"country_of_origin": "Thailand"}
