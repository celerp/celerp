"""Old preset weights fold onto the core weight only when they are a finite number.

A release before the core weight field stored a category's Weight (g) as given, so the ledger
can hold any JSON value there: text, nan, an infinity, a number too large for a float, true, a
list. Every event and every rebuild folds that value. A value that is not a finite number stays
in the attributes as stored; it never becomes a weight the database refuses, so neither the
upgrade rebuild nor a save of such an item is refused.

The values are generated from fixed seeds and checked against a reference written apart from
the fold (decimal parsing, not float()).
"""
from __future__ import annotations

import random
from decimal import Decimal, InvalidOperation

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

_WORDS = ["nan", "NaN", "inf", "-inf", "+inf", "Infinity", "-Infinity", "infinity", "1e400", "-1e400",
          "1e308", "4.9e-324", "abc", "", " ", "  7 ", "0", "-5", "12.5", "1,5", "0x10", "1e", "--1"]
_SEEDS = range(40)
_DB_SEEDS = (11, 23, 37)


def _hostile(rng: random.Random):
    kind = rng.randrange(10)
    if kind == 0:
        return rng.choice(_WORDS)
    if kind == 1:
        return rng.choice(["", " ", "-", "+"]) + "9" * rng.randint(300, 2000)
    if kind == 2:
        return f"{rng.choice(['', '-'])}{rng.randint(1, 9)}e{rng.randint(-400, 400)}"
    if kind == 3:
        return rng.choice([True, False])
    if kind == 4:
        return rng.choice([10 ** rng.randint(300, 400), -(10 ** 309), rng.randint(-10 ** 6, 10 ** 6)])
    if kind == 5:
        return rng.uniform(-1e6, 1e6)
    if kind == 6:
        return [rng.choice(_WORDS), rng.randint(0, 9)]
    if kind == 7:
        return {"a": rng.choice(_WORDS)}
    if kind == 8:
        return None
    return rng.choice(["nan", "inf", "infinity"]).upper() if rng.random() < .5 else rng.choice(_WORDS).title()


def _reference_weight(value) -> float | None:
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


def _expect(value, core_weight=None) -> tuple:
    """(weight, weight_unit, attributes) the fold leaves for an item that stored ``value``."""
    if core_weight is not None:
        return core_weight, "kilogram", {}
    grams = _reference_weight(value)
    if grams is not None:
        return grams, "gram", {}
    return None, None, ({} if value in (None, "") else {"weight_g": value})


def _folded(state: dict) -> tuple:
    return state.get("weight"), state.get("weight_unit"), state.get("attributes") or {}


def _read(item: dict) -> tuple:
    """The same view of an item read through the API, which lifts attributes to the top level."""
    return item.get("weight"), item.get("weight_unit"), {k: item[k] for k in ("weight_g",) if k in item}


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_fold_moves_only_a_finite_number_onto_the_weight(seed):
    from celerp_inventory.projections import apply_item_event
    rng = random.Random(seed)
    for _ in range(60):
        value = _hostile(rng)
        core = rng.choice([None, None, 3.0])
        data = {"sku": "S", "name": "n", "attributes": {"weight_g": value}}
        if core is not None:
            data.update(weight=core, weight_unit="kilogram")
        state = apply_item_event({}, "item.created", data)
        assert _folded(state) == _expect(value, core), (seed, repr(value)[:80])


async def _headers(client) -> dict:
    from test_entity_files import _headers as register
    return await register(client)


async def _item(client, headers, sku: str, attributes: dict) -> tuple:
    response = await client.post("/items", headers=headers, json={
        "sku": sku, "name": "Old preset lot", "quantity": 1, "sell_by": "piece", "attributes": attributes})
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", _DB_SEEDS)
async def test_a_rebuild_over_any_stored_old_weight_finishes(client, session, seed):
    """The upgrade rebuild replays events an older release stored with any Weight (g) value."""
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    headers = await _headers(client)
    company_id = headers["X-Company-Id"]
    rng = random.Random(seed)
    stored = {}
    for n in range(25):
        response = await _item(client, headers, f"OLD-{seed}-{n}", {"weight_g": "abc"})
        assert response.status_code == 200, response.text
        stored[response.json()["id"]] = _hostile(rng)
    for entry in (await session.execute(select(LedgerEntry).where(
            LedgerEntry.entity_id.in_(list(stored)), LedgerEntry.event_type == "item.created"))).scalars():
        entry.data = {**entry.data, "attributes": {"weight_g": stored[entry.entity_id]}}
        flag_modified(entry, "data")
    await session.commit()

    await lock_company(session, company_id)
    await ProjectionEngine.rebuild(session, company_id)
    await session.commit()
    session.expire_all()

    rows = (await session.execute(select(Projection).where(Projection.entity_id.in_(list(stored))))).scalars().all()
    assert len(rows) == len(stored)
    for row in rows:
        assert _folded(row.state) == _expect(stored[row.entity_id]), repr(stored[row.entity_id])[:80]
    after = await _item(client, headers, f"AFTER-{seed}", {})
    assert after.status_code == 200, after.text


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", _DB_SEEDS)
async def test_saving_any_old_weight_is_never_a_server_error(client, seed):
    """A client still sending Weight (g) gets the same fold on create and on edit, never a 500."""
    headers = await _headers(client)
    rng = random.Random(seed)
    for n in range(15):
        value = _hostile(rng)
        created = await _item(client, headers, f"LIVE-{seed}-{n}", {"weight_g": value})
        assert created.status_code == 200, (repr(value)[:80], created.text[:200])
        item = (await client.get("/items/" + created.json()["id"], headers=headers)).json()
        assert _read(item) == _expect(value), repr(value)[:80]
        plain = await _item(client, headers, f"EDIT-{seed}-{n}", {})
        value = _hostile(rng)
        edited = await client.patch("/items/" + plain.json()["id"], headers=headers,
                                    json={"fields_changed": {"weight_g": {"old": None, "new": value}}})
        assert edited.status_code == 200, (repr(value)[:80], edited.text[:200])
        item = (await client.get("/items/" + plain.json()["id"], headers=headers)).json()
        assert _read(item) == _expect(value), repr(value)[:80]
