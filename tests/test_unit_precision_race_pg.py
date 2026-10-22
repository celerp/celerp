# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A quantity write racing a change to its unit's decimal places never stores a quantity
the unit forbids.

Each case runs on real PostgreSQL with a fresh session per request and a fixed
interleaving: at a chosen point inside request A (an item or document write of 0.27 ct),
request B lowers carat from 2 to 1 decimal places. Whichever commits first, the result is
one a user could reach doing the two one after the other: either A is stored and B is
refused because 0.27 ct now exists, or B is stored and A is refused as too precise.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.db import get_session
from celerp.main import app
from celerp.models.projections import Projection
from test_helpers import company_auth

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def http(factory):
    async def _fresh():
        async with factory() as s:
            yield s

    @asynccontextmanager
    async def _ctx():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _fresh
    with patch("celerp.gateway.client._client", MagicMock()), \
         patch("celerp.gateway.state.get_session_token", return_value="t"), \
         patch("celerp.middleware.get_session_ctx", _ctx), \
         patch("celerp.db.LifecycleSessionLocal", factory):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def auth(factory):
    async with factory() as s:
        return await company_auth(s, uuid.uuid4(), uuid.uuid4())


async def _carat(http, auth, decimals):
    units = (await http.get("/companies/me/units", headers=auth["headers"])).json()
    units = [{**u, "decimals": decimals} if u["name"] == "carat" else u for u in units]
    return await http.put("/companies/me/units", headers=auth["headers"], json={"units": units})


async def _until_blocked_or_done(engine, task: asyncio.Task) -> None:
    """Wait until ``task`` either finished or waits on a row lock (each poll its own snapshot)."""
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the precision change neither finished nor waited")


def _arm(monkeypatch, engine, module, attr, http, auth, *, before=False, b=None):
    """At A's hook point (just after ``module.attr`` returns, or just before it is called
    when ``before``) start B, by default lowering carat to 1 decimal place, and let it run
    until it commits or waits for A's lock."""
    real = getattr(module, attr)
    state = {"armed": True, "b": None}

    async def run_b():
        if state["armed"]:
            state["armed"] = False
            state["b"] = asyncio.ensure_future(b() if b else _carat(http, auth, 1))
            await _until_blocked_or_done(engine, state["b"])

    async def hooked(*args, **kwargs):
        if before:
            await run_b()
        out = await real(*args, **kwargs)
        if not before:
            await run_b()
        return out

    monkeypatch.setattr(module, attr, hooked)
    return state


async def _carat_quantities(factory, auth) -> list[float]:
    async with factory() as s:
        rows = (await s.execute(select(Projection).where(Projection.company_id == auth["company_id"]))).scalars()
        out = []
        for r in rows:
            if r.entity_type == "item" and r.state.get("sell_by") == "carat":
                out.append(float(r.state.get("quantity") or 0))
            if r.entity_type == "doc":
                out += [float(li.get("quantity") or 0) for li in r.state.get("line_items") or []]
        return out


async def _assert_serial(http, factory, auth, a, state, *, a_first: bool):
    """The outcome is one of the two serial orders, the expected one for this interleave."""
    assert state["b"] is not None, "the hook point was never reached"
    b = await state["b"]
    units = (await http.get("/companies/me/units", headers=auth["headers"])).json()
    carat = next(u["decimals"] for u in units if u["name"] == "carat")
    qtys = await _carat_quantities(factory, auth)
    seen = (a.status_code, a.text[:200], b.status_code, b.text[:200], carat, qtys)
    assert not (carat == 1 and 0.27 in qtys), seen
    if a_first:
        assert (a.status_code, b.status_code, carat) == (200, 422, 2), seen
        assert 0.27 in qtys, seen
        assert "carat" in b.text and "1 decimal" in b.text, seen
    else:
        assert (a.status_code, b.status_code, carat) == (422, 200, 1), seen
        assert 0.27 not in qtys, seen
        assert "0.27" in a.text, seen


async def _item(http, auth, qty=6):
    r = await http.post("/items", headers=auth["headers"], json={
        "sku": f"R-{uuid.uuid4().hex[:6]}", "name": "Parcel", "quantity": qty, "sell_by": "carat",
        "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
async def two_dp(http, auth):
    assert (await _carat(http, auth, 2)).status_code == 200


async def test_creating_an_item_while_decimals_drop_is_checked_against_the_committed_unit(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    import celerp_inventory.routes as inv
    state = _arm(monkeypatch, committed_engine, inv, "_get_company_units", http, auth)
    a = await http.post("/items", headers=auth["headers"], json={
        "sku": "R-NEW", "name": "Parcel", "quantity": 0.27, "sell_by": "carat", "status": "available"})
    await _assert_serial(http, factory, auth, a, state, a_first=True)


async def test_patching_a_quantity_while_decimals_drop_is_checked_against_the_committed_unit(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    item = await _item(http, auth)
    import celerp_inventory.routes as inv
    state = _arm(monkeypatch, committed_engine, inv, "_get_company_units", http, auth)
    a = await http.patch(f"/items/{item}", headers=auth["headers"], json={
        "fields_changed": {"quantity": {"old": 6, "new": 0.27}}})
    await _assert_serial(http, factory, auth, a, state, a_first=True)


async def test_splitting_while_decimals_drop_is_checked_against_the_committed_unit(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    item = await _item(http, auth)
    import celerp_inventory.routes as inv
    state = _arm(monkeypatch, committed_engine, inv, "_get_company_units", http, auth)
    a = await http.post(f"/items/{item}/split", headers=auth["headers"], json={"children": [{"quantity": 0.27}]})
    await _assert_serial(http, factory, auth, a, state, a_first=True)


async def test_a_draft_line_while_decimals_drop_is_checked_against_the_committed_unit(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    item = await _item(http, auth)
    import celerp_docs.routes as docs
    state = _arm(monkeypatch, committed_engine, docs, "_get_unit_map", http, auth)
    a = await http.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"item_id": item, "name": "Stone", "quantity": 0.27,
                                               "unit": "carat", "sell_by": "carat", "unit_price": 10.0}]})
    await _assert_serial(http, factory, auth, a, state, a_first=True)


# B commits in the instant before A takes the company lock. A's request already loaded the
# Company row (the settings dependency), so the units must be re-read after the lock.
async def test_a_patch_after_decimals_dropped_just_before_its_lock_sees_the_new_unit(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    item = await _item(http, auth)
    import celerp.services.company_lock as cl
    state = _arm(monkeypatch, committed_engine, cl, "lock_company", http, auth, before=True)
    a = await http.patch(f"/items/{item}", headers=auth["headers"], json={
        "fields_changed": {"quantity": {"old": 6, "new": 0.27}}})
    await _assert_serial(http, factory, auth, a, state, a_first=False)


async def test_a_draft_line_after_decimals_dropped_just_before_its_lock_sees_the_new_unit(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    item = await _item(http, auth)
    import celerp.services.company_lock as cl
    state = _arm(monkeypatch, committed_engine, cl, "lock_company", http, auth, before=True)
    a = await http.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"item_id": item, "name": "Stone", "quantity": 0.27,
                                               "unit": "carat", "sell_by": "carat", "unit_price": 10.0}]})
    await _assert_serial(http, factory, auth, a, state, a_first=False)


async def test_a_batch_import_after_decimals_dropped_just_before_its_lock_refuses_the_record(
        http, factory, auth, two_dp, committed_engine, monkeypatch):
    import celerp.services.company_lock as cl
    state = _arm(monkeypatch, committed_engine, cl, "lock_company", http, auth, before=True)
    a = await http.post("/items/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": "csv_import",
        "idempotency_key": f"csv:{uuid.uuid4().hex}",
        "data": {"sku": f"I-{uuid.uuid4().hex[:6]}", "name": "Parcel", "quantity": 0.27, "sell_by": "carat",
                 "status": "available"}}]})
    assert state["b"] is not None, "the hook point was never reached"
    b = await state["b"]
    units = (await http.get("/companies/me/units", headers=auth["headers"])).json()
    qtys = await _carat_quantities(factory, auth)
    seen = (a.status_code, a.text[:300], b.status_code, b.text[:200], qtys)
    # The batch answers 200 and reports a refused record among its errors.
    assert (a.status_code, b.status_code) == (200, 200), seen
    assert a.json()["created"] == 0 and len(a.json()["errors"]) == 1 and "0.27" in a.text, seen
    assert next(u["decimals"] for u in units if u["name"] == "carat") == 1, seen
    assert 0.27 not in qtys, seen


# B removes carat, or makes it a piece unit, while A creates an item sold by it. Either the item
# exists and the change is refused because it is in use, or the change is stored first and the
# item is checked against it: refused for a unit that no longer exists, or created in carat as a
# piece unit, the same as doing the two one after the other.

def _without_carat(units):
    return [u for u in units if u["name"] != "carat"]


def _carat_as_pieces(units):
    return [{**u, "unit_type": "pieces"} if u["name"] == "carat" else u for u in units]


_UNIT_CHANGES = {"removed": (_without_carat, "units.unit_removed_in_use"),
                 "retyped": (_carat_as_pieces, "units.unit_type_changed_in_use")}


def _change_carat(http, auth, edit):
    async def put():
        units = (await http.get("/companies/me/units", headers=auth["headers"])).json()
        return await http.put("/companies/me/units", headers=auth["headers"], json={"units": edit(units)})
    return put


async def _create_carat_item(http, auth):
    return await http.post("/items", headers=auth["headers"], json={
        "sku": "R-NEW", "name": "Parcel", "quantity": 3.5, "sell_by": "carat", "status": "available",
        "allow_splitting": True})


async def _assert_unit_serial(http, factory, auth, a, state, change, *, a_first: bool):
    assert state["b"] is not None, "the hook point was never reached"
    b = await state["b"]
    edit, key = _UNIT_CHANGES[change]
    units = (await http.get("/companies/me/units", headers=auth["headers"])).json()
    carat = next((u for u in units if u["name"] == "carat"), None)
    stored = await _carat_quantities(factory, auth)
    seen = (a.status_code, a.text[:200], b.status_code, b.text[:200], carat, stored)
    if a_first:
        assert (a.status_code, b.status_code) == (200, 422), seen
        assert b.json()["detail"]["message_key"] == key and "R-NEW" in b.text, seen
        assert carat == {**carat, "unit_type": "weight"} and stored == [3.5], seen
    elif change == "removed":
        assert (a.status_code, b.status_code) == (422, 200), seen
        assert units == edit(units) and stored == [], seen
    else:  # the item is created in carat as it now is, a piece unit
        assert (a.status_code, b.status_code) == (200, 200), seen
        assert units == edit(units) and stored == [3.5], seen


@pytest.mark.parametrize("change", list(_UNIT_CHANGES))
async def test_creating_an_item_while_its_unit_changes_is_checked_against_the_committed_units(
        http, factory, auth, committed_engine, monkeypatch, change):
    import celerp_inventory.routes as inv
    state = _arm(monkeypatch, committed_engine, inv, "_get_company_units", http, auth,
                 b=_change_carat(http, auth, _UNIT_CHANGES[change][0]))
    a = await _create_carat_item(http, auth)
    await _assert_unit_serial(http, factory, auth, a, state, change, a_first=True)


@pytest.mark.parametrize("change", list(_UNIT_CHANGES))
async def test_an_item_created_after_its_unit_changed_just_before_its_lock_sees_the_new_units(
        http, factory, auth, committed_engine, monkeypatch, change):
    import celerp.services.company_lock as cl
    state = _arm(monkeypatch, committed_engine, cl, "lock_company", http, auth, before=True,
                 b=_change_carat(http, auth, _UNIT_CHANGES[change][0]))
    a = await _create_carat_item(http, auth)
    await _assert_unit_serial(http, factory, auth, a, state, change, a_first=False)


# Only reads that validate nothing may use the unlocked units read; every writer goes through
# get_company_units (inventory) or _get_unit_map (documents), both read under the company lock.
_DISPLAY_READERS = {
    "default_modules/celerp-inventory/celerp_inventory/routes.py": {
        "query_items", "items_metadata", "get_item", "split_preview", "export_items_csv"},
    "default_modules/celerp-inventory/celerp_inventory/search.py": {"flatten_item_rows"},
    "default_modules/celerp-labels/celerp_labels/routes.py": {"_unit_map"},
}


def test_only_display_reads_skip_the_units_lock():
    found: dict[str, set[str]] = {}
    for path in sorted((_ROOT / "default_modules").rglob("*.py")) + sorted((_ROOT / "celerp").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = {n.func.id for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "read_company_units" in calls and fn.name != "read_company_units":
                found.setdefault(str(path.relative_to(_ROOT)), set()).add(fn.name)
    assert found == _DISPLAY_READERS
