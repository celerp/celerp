# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A cost written back as it was shown changes nothing, at every door that takes one.

A lot's cost is shown as its whole value (freight included), as a total or per unit, at the
decimals each door shows or exports: the inventory list's unit and total cells, the item's
Pricing tab unit and total, the transform page and the item file. Saved back unchanged, at any
quantity and for values that do not divide by it, it writes no event and books nothing, and
freight stays freight. A changed value is the lot's new whole value, its goods cost what is
left without the freight, and a value below the freight is refused. Every door reads it through
one owner, and every surface that shows a lot's cost shows the value it carries with "goods
cost unknown" beside a value whose goods cost is not known."""
from __future__ import annotations

import ast
import csv
import io
import math
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from test_cost_restatement import _state
from test_landed_cost_removals import _seed
from test_merge_unknown_cost import (MARK, _browser, _cost_column, _counts, _item, _marked, _read, _token,
                                     _with_freight)
from ui.i18n import t

ROOT = Path(__file__).resolve().parents[1]
_UI = sorted((ROOT / "ui").rglob("*.py"))


def _functions_where(paths: list[Path], hit) -> set[tuple[str, str]]:
    """(file, innermost function) of every node in ``paths`` for which ``hit(node)`` holds."""
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text())

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if hit(child):
                    found.add((path.relative_to(ROOT).as_posix(), name))
                walk(child, name)
        walk(tree, "<module>")
    return found


# ── one owner ────────────────────────────────────────────────────────────────

_INV = "default_modules/celerp-inventory/celerp_inventory/services.py"
_DOORS = {(_INV, "_pop_cost_change"), ("ui/routes/inventory.py", "_field_patch"),
          ("ui/routes/inventory.py", "item_price"), ("ui/routes/inventory.py", "bulk_item_transform")}


def test_every_cost_door_reads_the_cost_through_its_one_owner():
    calls = _functions_where(
        [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".venv" not in p.parts
         and "site-packages" not in p.parts],
        lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "cost_written_back")
    assert calls == _DOORS


def test_every_cost_door_hands_the_owner_the_text_the_user_typed():
    """No door compares or parses the cost itself: each passes the owner the text it received,
    so the owner judges "no change" at the decimals the user typed."""
    def _parsed(n) -> bool:
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "cost_written_back"):
            return False
        shown = n.args[1]
        return isinstance(shown, ast.Constant) or (
            isinstance(shown, ast.Call) and getattr(shown.func, "id", None) in ("float", "round", "int", "Decimal"))
    paths = [ROOT / _INV, ROOT / "ui/routes/inventory.py"]
    assert _functions_where(paths, _parsed) == set()
    from celerp_inventory.services import cost_written_back
    with pytest.raises(TypeError):
        cost_written_back({"quantity": 1, "cost_total": 1.0}, 1.0, dp=2)


def test_no_ui_route_builds_a_cost_patch_itself():
    """A cost key written by the UI is either cleared or the change the owner returned."""
    def _sets_cost(n) -> bool:
        if not isinstance(n, ast.Dict):
            return False
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value in ("cost_total", "cost_price") and isinstance(v, ast.Dict):
                new = dict(zip((getattr(x, "value", None) for x in v.keys), v.values)).get("new")
                if new is not None and not (isinstance(new, ast.Constant) and new.value is None):
                    return True
        return False
    assert _functions_where(_UI, _sets_cost) == set()


# ── saved as shown, at every door ────────────────────────────────────────────

def _input(page: str, **attr) -> str:
    [(k, v)] = attr.items()
    tag = next(m for m in re.findall(r"<input[^>]*>", page) if f'{k}="{v}"' in m)
    found = re.search(r'value="([^"]*)"', tag)
    return found.group(1) if found else ""


def _money_text(cell: str) -> str:
    return re.sub(r"[^0-9.\-]", "", re.search(r'class="cell-money">([^<]*)<', cell).group(1))


async def _exported_row(client, auth, lot: str, cols: str | None = None) -> dict:
    """The lot's row of the item file, with the list's columns ``cols`` or the default ones."""
    sku = (await _read(client, auth, lot))["sku"]
    r = await client.get("/items/export/csv", headers=auth["headers"], params={"cols": cols} if cols else {})
    own = {"id", "status", "created_at", "updated_at"}
    [row] = [{k: v for k, v in row.items() if k not in own and v not in ("", None)}
             for row in csv.DictReader(io.StringIO(r.text)) if row.get("sku") == sku]
    return row


async def _import(client, auth, row: dict):
    return await client.post("/items/import/rows", headers=auth["headers"], json={
        "rows": [row], "upsert": True, "idempotency_key": f"wb-{uuid.uuid4().hex[:6]}"})


async def _save_as_shown(door: str, client, auth, lot: str) -> str:
    """Read the lot's cost where the door shows it and save it back unchanged; the value saved."""
    if door.startswith("import"):
        key = "cost_price_total" if door == "import_total" else "cost_price"
        row = await _exported_row(client, auth, lot, f"sku,name,quantity,sell_by,{key}" if door == "import_total" else None)
        assert key in row, row
        r = await _import(client, auth, row)
        assert r.status_code == 200 and not r.json().get("errors"), r.text
        return row[key]
    async with _browser(_token(auth)) as ui:
        if door == "cell_total":
            shown = _input((await ui.get(f"/api/items/{lot}/field/cost_price_total/edit")).text, name="value")
            await ui.patch(f"/api/items/{lot}/field/cost_price_total", data={"value": shown})
        elif door in ("cell_unit_edit", "cell_unit_display"):
            if door == "cell_unit_edit":
                shown = _input((await ui.get(f"/api/items/{lot}/field/cost_price/edit")).text, name="value")
            else:
                shown = _money_text((await ui.get(f"/api/items/{lot}/field/cost_price/display")).text)
            await ui.patch(f"/api/items/{lot}/field/cost_price", data={"value": shown})
        elif door in ("pricing_unit", "pricing_total"):
            page = (await ui.get(f"/inventory/{lot}?tab=pricing")).text
            if door == "pricing_unit":
                shown = _input(page, id="unit_cost_price")
                r = await ui.post(f"/api/items/{lot}/price", data={"cost_price": shown})
            else:
                shown = _input(page, id="total_cost_price")
                r = await ui.post(f"/api/items/{lot}/price", data={"cost_price_total": shown})
            assert r.status_code == 200, r.text
        assert shown, door
        return shown


_SURFACE_DOORS = ["cell_total", "cell_unit_edit", "cell_unit_display", "pricing_unit", "pricing_total", "import_total"]
_DOOR_CASES = [(d, True) for d in _SURFACE_DOORS] + [("import_unit", False)]
_LOTS = [(3, 100.0), (7, 100.0), (0.3, 100.0), (3, 1.0), (7, 1.0), "freight"]


def _cost_fields(st: dict) -> dict:
    return {k: st.get(k) for k in ("cost_total", "cost_base", "landed_costs", MARK)}


async def _cost_events(session, auth, lot: str) -> int:
    from celerp.models.ledger import LedgerEntry
    from sqlalchemy import select
    session.expire_all()
    rows = (await session.scalars(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == lot))).all()
    return sum(1 for e in rows if "cost_" in str(e.data))


@pytest.mark.parametrize("lot_case", _LOTS, ids=lambda c: c if isinstance(c, str) else f"{c[0]:g}x{c[1]:g}")
@pytest.mark.parametrize("door,cost_list", _DOOR_CASES, ids=[d for d, _ in _DOOR_CASES])
async def test_a_cost_saved_as_shown_changes_nothing(client, session, auth, door, cost_list, lot_case):
    if cost_list:
        await _cost_column(session, auth)
    if lot_case == "freight":
        lot = await _with_freight(client, session, auth)
    else:
        await _seed(session, auth)
        qty, value = lot_case
        lot = await _item(client, auth, qty=qty, cost_total=value, sell_by="piece" if qty >= 1 else "kg")
    before, events, jes = (_cost_fields(await _state(session, auth, lot)), await _cost_events(session, auth, lot),
                           (await _counts(session, auth, lot))[1])
    shown = await _save_as_shown(door, client, auth, lot)
    after = _cost_fields(await _state(session, auth, lot))
    assert after == before, (door, shown, before, after)
    assert await _cost_events(session, auth, lot) == events and (await _counts(session, auth, lot))[1] == jes, shown


def _one_step_finer(shown: str, qty: float | None) -> str:
    """``shown`` changed in its last typed decimal, at the finest step still worth a cent of the
    lot's value: 0.01 for a total, 0.01 / ``qty`` for a unit."""
    places = max(-Decimal(shown).as_tuple().exponent, 2 + (round(math.log10(qty)) if qty else 0))
    return f"{Decimal(shown) + Decimal(1).scaleb(-places):.{places}f}"


async def _type_at(door: str, client, auth, lot: str, typed: str):
    """Save ``typed`` where the door takes the lot's cost."""
    if door.startswith("import"):
        key = "cost_price_total" if door == "import_total" else "cost_price"
        row = await _exported_row(client, auth, lot, f"sku,name,quantity,sell_by,{key}" if door == "import_total" else None)
        r = await _import(client, auth, {**row, key: typed})
        assert r.status_code == 200 and not r.json().get("errors"), r.text
        return
    async with _browser(_token(auth)) as ui:
        if door == "cell_total":
            r = await ui.patch(f"/api/items/{lot}/field/cost_price_total", data={"value": typed})
        elif door.startswith("cell_unit"):
            r = await ui.patch(f"/api/items/{lot}/field/cost_price", data={"value": typed})
        else:
            r = await ui.post(f"/api/items/{lot}/price",
                              data={"cost_price" if door == "pricing_unit" else "cost_price_total": typed})
        assert r.status_code == 200, r.text[:300]


@pytest.mark.parametrize("qty", [1000, 100000])
@pytest.mark.parametrize("door,cost_list", _DOOR_CASES, ids=[d for d, _ in _DOOR_CASES])
async def test_a_cost_changed_in_its_last_typed_decimal_is_applied(client, session, auth, door, cost_list, qty):
    """The inverse of saving as shown: "no change" is judged at the decimals the user typed,
    never coarser, so a unit typed one decimal finer than shown is the lot's new value (unit x
    quantity), and a total changed by a cent is that total."""
    if cost_list:
        await _cost_column(session, auth)
    await _seed(session, auth)
    lot = await _item(client, auth, qty=qty, cost_total=10.0 * qty)
    per_unit = door not in ("cell_total", "pricing_total", "import_total")
    async with _browser(_token(auth)) as ui:
        shown = {"cell_total": lambda: ui.get(f"/api/items/{lot}/field/cost_price_total/edit"),
                 "cell_unit_edit": lambda: ui.get(f"/api/items/{lot}/field/cost_price/edit")}.get(door)
        page = (await shown()).text if shown else None
    if page is not None:
        text = _input(page, name="value")
    elif door == "cell_unit_display":
        async with _browser(_token(auth)) as ui:
            text = _money_text((await ui.get(f"/api/items/{lot}/field/cost_price/display")).text)
    elif door.startswith("pricing"):
        async with _browser(_token(auth)) as ui:
            text = _input((await ui.get(f"/inventory/{lot}?tab=pricing")).text,
                          id="unit_cost_price" if per_unit else "total_cost_price")
    else:
        key = "cost_price_total" if door == "import_total" else "cost_price"
        text = (await _exported_row(client, auth, lot, f"sku,name,quantity,sell_by,{key}" if door == "import_total" else None))[key]
    typed = _one_step_finer(text, qty if per_unit else None)
    await _type_at(door, client, auth, lot, typed)
    expected = float(Decimal(typed) * qty) if per_unit else float(typed)
    st = await _state(session, auth, lot)
    assert st["cost_total"] == expected and st["cost_total"] != 10.0 * qty, (door, text, typed, st.get("cost_total"))


async def test_the_transform_page_cost_sent_back_as_shown_keeps_the_parent_cost(client, session, auth):
    """The transform page shows the parent's whole value; sent back unchanged the child keeps it
    and its freight, whatever the value rounds to on the page."""
    await _cost_column(session, auth)
    await _seed(session, auth)
    for lot in (await _item(client, auth, qty=3, cost_total=round(100 / 3, 10)), await _with_freight(client, session, auth)):
        parent = await _state(session, auth, lot)
        async with _browser(_token(auth)) as ui:
            page = (await ui.get("/api/items/bulk/transform-preview", params={"entity_id": lot})).text
            form = {"entity_id": lot, "child_sku": _input(page, name="child_sku"), "child_qty": _input(page, name="child_qty"),
                    "child_category": parent["category"], "child_sell_by": parent["sell_by"],
                    "child_cost_total": _input(page, name="child_cost_total")}
            r = await ui.post("/api/items/bulk/transform", data=form)
        assert r.status_code == 200, r.text
        child = next(iter(await _children(session, auth, form["child_sku"])))
        assert child["cost_total"] == parent["cost_total"], (form, child)
        assert (child.get("landed_costs") or {}) == (parent.get("landed_costs") or {}), child


async def _children(session, auth, sku: str) -> list[dict]:
    from celerp.models.projections import Projection
    from sqlalchemy import select
    session.expire_all()
    rows = (await session.scalars(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).all()
    return [r.state for r in rows if (r.state or {}).get("sku") == sku]


# ── a changed value ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [("cost_price_total", "160"), ("cost_price", "16")])
async def test_the_pricing_tab_writes_a_changed_cost_as_the_whole_value(client, session, auth, field, value):
    await _cost_column(session, auth)
    lot = await _with_freight(client, session, auth)
    async with _browser(_token(auth)) as ui:
        await ui.post(f"/api/items/{lot}/price", data={field: value})
        st = await _state(session, auth, lot)
        assert st["cost_base"] == 130.0 and st["cost_total"] == 160.0, st
        refused = (await ui.post(f"/api/items/{lot}/price", data={field: "20" if field == "cost_price_total" else "2"})).text
    assert "30.00" in refused, refused
    assert (await _state(session, auth, lot))["cost_base"] == 130.0


_CONFIRM_DOORS = ["cell_total", "cell_unit", "cell_unit_empty", "pricing_unit", "pricing_total", "transform"]


@pytest.mark.parametrize("door", _CONFIRM_DOORS)
async def test_typing_the_carried_value_on_a_marked_lot_confirms_it(client, session, auth, door):
    """A lot of unknown goods cost opens its cost edit empty, so the value the user types there
    is what they know, even when it is the value the lot carries: every door confirms it, and
    an emptied lot confirms the unit cost its next units wait at."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    m = await _marked(client, auth)
    if door == "cell_unit_empty":
        assert (await client.post(f"/items/{m}/adjust", headers=auth["headers"], json={"new_qty": 0})).status_code == 200
    parent = await _state(session, auth, m)
    assert parent[MARK] is True and parent["cost_total" if door != "cell_unit_empty" else "cost_price"] == (
        100.0 if door != "cell_unit_empty" else 20.0), parent
    async with _browser(_token(auth)) as ui:
        if door == "transform":
            page = (await ui.get("/api/items/bulk/transform-preview", params={"entity_id": m})).text
            assert _input(page, name="child_cost_total") == ""
            sku = _input(page, name="child_sku")
            r = await ui.post("/api/items/bulk/transform", data={
                "entity_id": m, "child_sku": sku, "child_qty": _input(page, name="child_qty"),
                "child_category": parent["category"], "child_sell_by": parent["sell_by"], "child_cost_total": "100"})
        elif door.startswith("cell"):
            field = "cost_price_total" if door == "cell_total" else "cost_price"
            if door == "cell_unit_empty":
                assert _input((await ui.get(f"/api/items/{m}/field/{field}/edit")).text, name="value") == ""
            r = await ui.patch(f"/api/items/{m}/field/{field}", data={"value": "100" if door == "cell_total" else "20"})
        else:
            r = await ui.post(f"/api/items/{m}/price",
                              data={"cost_price_total": "100"} if door == "pricing_total" else {"cost_price": "20"})
    assert r.status_code == 200, r.text[:300]
    st = next(iter(await _children(session, auth, sku))) if door == "transform" else await _state(session, auth, m)
    assert MARK not in st and (st["cost_price"] == 20.0 if door == "cell_unit_empty" else st["cost_total"] == 100.0), (door, st)


async def test_a_transform_without_a_cost_keeps_the_parent_cost_unknown(client, session, auth):
    """No cost sent is the parent's cost kept, as known as the parent's."""
    await _seed(session, auth)
    m = await _marked(client, auth)
    parent = await _state(session, auth, m)
    async with _browser(_token(auth)) as ui:
        page = (await ui.get("/api/items/bulk/transform-preview", params={"entity_id": m})).text
        sku = _input(page, name="child_sku")
        r = await ui.post("/api/items/bulk/transform", data={
            "entity_id": m, "child_sku": sku, "child_qty": _input(page, name="child_qty"),
            "child_category": parent["category"], "child_sell_by": parent["sell_by"], "child_cost_total": ""})
    assert r.status_code == 200, r.text[:300]
    child = next(iter(await _children(session, auth, sku)))
    assert child[MARK] is True and child["cost_total"] == 100.0, child


async def test_a_lot_without_stock_keeps_a_unit_cost_from_the_pricing_tab_as_its_waiting_cost(client, session, auth):
    await _seed(session, auth)
    await _cost_column(session, auth)
    lot = await _item(client, auth, qty=0)
    async with _browser(_token(auth)) as ui:
        await ui.post(f"/api/items/{lot}/price", data={"cost_price": "12.5"})
    assert (await _state(session, auth, lot))["cost_price"] == 12.5


# ── every surface that shows a lot's cost ────────────────────────────────────

def test_every_surface_showing_unknown_goods_cost_uses_one_renderer():
    """The label beside a cost value is rendered in one place; the merge note and the list's
    import preview name it in their own sentence."""
    hits = _functions_where(_UI, lambda n: isinstance(n, ast.Constant) and n.value == "inventory.goods_cost_unknown")
    assert hits == {("ui/routes/inventory.py", "_cost_total_view"), ("ui/routes/inventory.py", "_merge_result_sentence")}


async def test_every_surface_shows_the_value_a_marked_lot_carries(client, session, auth):
    await _seed(session, auth)
    await _cost_column(session, auth)
    m = await _marked(client, auth)
    label = t("inventory.goods_cost_unknown")
    async with _browser(_token(auth)) as ui:
        pages = {
            "list cell": (await ui.get(f"/api/items/{m}/field/cost_price_total/display")).text,
            "pricing tab": (await ui.get(f"/inventory/{m}?tab=pricing")).text,
            "transform page": (await ui.get("/api/items/bulk/transform-preview", params={"entity_id": m})).text,
        }
    for surface, page in pages.items():
        assert re.search(rf'class="cell-money">[^<]*100\.00</span>\s*<span class="cell-price-unit">{label}<', page), surface
