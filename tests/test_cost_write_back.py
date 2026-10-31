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
import html
import io
import json
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
_UI_INV = "ui/routes/inventory.py"
_UI_DOORS = {(_UI_INV, "_write_cost_cell"), (_UI_INV, "item_price"), (_UI_INV, "bulk_item_transform")}
# Every function that renders a UI cost door's text (and so the text its save is read against).
_RENDERERS = {(_UI_INV, "_edit_cell"), (_UI_INV, "_row"), (_UI_INV, "_pricing_cost_opened"),
              (_UI_INV, "_pricing_cost_stale"), (_UI_INV, "bulk_transform_preview"), (_UI_INV, "_cost_written_back")}


def _calls(name: str):
    return lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == name


def _code() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".venv" not in p.parts
            and "site-packages" not in p.parts]


def test_every_cost_door_reads_the_cost_through_its_one_owner():
    """The item import and the UI wrapper call the owner; every UI door calls the wrapper,
    which reads the save against the door's own render helper."""
    assert _functions_where(_code(), _calls("cost_written_back")) == {(_INV, "_pop_cost_change"), (_UI_INV, "_cost_written_back")}
    assert _functions_where(_code(), _calls("_cost_written_back")) == _UI_DOORS


def test_no_cost_door_passes_a_precision_and_each_renders_through_one_helper():
    """No precision exists to drift from what a door shows: the owner takes none, no caller
    passes one, and every UI cost door's text comes from _cost_door_text (the CSV's from
    exported_cost_text, the cells the export writes)."""
    import inspect
    from celerp_inventory.services import cost_written_back
    assert set(inspect.signature(cost_written_back).parameters) == {"state", "typed", "opened", "now", "per_unit"}

    def _precision(n) -> bool:
        return (_calls("cost_written_back")(n) or _calls("_cost_written_back")(n)) and any(
            k.arg in ("dp", "places", "precision", "confirm") for k in n.keywords)
    assert _functions_where(_code(), _precision) == set()
    assert _functions_where(_code(), _calls("_cost_door_text")) == _RENDERERS
    assert _functions_where(_code(), _calls("exported_cost_text")) >= {
        (_INV, "_pop_cost_change"), ("default_modules/celerp-inventory/celerp_inventory/routes.py", "_rows")}


def test_every_cost_door_hands_the_owner_the_text_the_user_typed():
    """No door compares or parses the cost itself: each passes the owner the text it received."""
    def _parsed(n) -> bool:
        if not (_calls("cost_written_back")(n) or _calls("_cost_written_back")(n)):
            return False
        shown = n.args[1]
        return isinstance(shown, ast.Constant) or (
            isinstance(shown, ast.Call) and getattr(shown.func, "id", None) in ("float", "round", "int", "Decimal"))
    paths = [ROOT / _INV, ROOT / _UI_INV]
    assert _functions_where(paths, _parsed) == set()
    from celerp_inventory.services import cost_written_back
    with pytest.raises(TypeError):
        cost_written_back({"quantity": 1, "cost_total": 1.0}, 1.0, opened="1.00", now="1.00")


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


# ── the rule, by property over every door ────────────────────────────────────

# Every door, as the text it opens with for a lot: the UI doors through their one render helper
# (the AST guard above ties each door to it), the item file through the cell its export writes.
_RULE_DOORS = {
    "cell_unit": ("ui", True), "cell_total": ("ui", False), "cost_total_field": ("ui", False),
    "pricing_unit": ("ui", True), "pricing_total": ("ui", False), "transform": ("ui", False),
    "import_unit": ("csv", True), "import_total": ("csv", False),
}


def _opening(door: str, flat: dict) -> str:
    from celerp_inventory.services import exported_cost_text
    from ui.routes.inventory import _cost_door_text
    kind, per_unit = _RULE_DOORS[door]
    return _cost_door_text(flat, "USD", per_unit) if kind == "ui" else exported_cost_text(flat, per_unit)


def _flat(qty: float, total: float | None, landed: float = 0.0, **extra) -> dict:
    from celerp_inventory.routes import flatten_item
    state = {"sku": "P", "quantity": qty, **({"cost_total": total} if total is not None else {}),
             **({"landed_costs": {"freight": landed}} if landed else {}), **extra}
    return flatten_item(state, "item:p")


def _carried(rng) -> tuple[float, float, float]:
    """A generated lot: (quantity, whole value, freight), dividing by its quantity or not, at 1 to
    10 decimals, quantities from a fraction of a unit to 100000."""
    qty = rng.choice([1, 2, 3, 7, 12.5, 0.37, 0.3, 100, 1000, 99999, 100000])
    places = rng.randint(1, 10)
    if rng.random() < 0.5:
        unit = Decimal(rng.randint(1, 10 ** 7)).scaleb(-places)
        total = float(round(unit * Decimal(repr(float(qty))), 10))
    else:
        total = float(Decimal(rng.randint(1, 10 ** 9)).scaleb(-places))
    landed = rng.choice([0.0, 0.0, round(total * 0.1, 2)])
    return qty, total, landed


def _variants(opened: str, rng) -> list[str]:
    """Typed texts around an opening text: some with its value, most without."""
    d = Decimal(opened)
    places = max(-d.as_tuple().exponent, 0)
    same = [opened, f"  {opened} ", f"${opened}", f"{opened}0" if "." in opened else f"{opened}.0"]
    other = [f"{d + Decimal(1).scaleb(-places):f}", f"{d + Decimal(1).scaleb(-places - 1):f}",
             f"{round(d, 2):.2f}", f"{round(d, 1):.1f}", f"{d + 1:f}", f"{round(d * Decimal('1.1'), 3):f}",
             str(rng.randint(1, 10 ** 6)), f"{Decimal(rng.randint(1, 10 ** 8)).scaleb(-rng.randint(1, 10)):f}"]
    return same + [t for t in other if Decimal(t) != d and Decimal(t) >= 0]


def _expected(typed: str, qty: float, landed: float, per_unit: bool):
    from celerp.services.money import round_basis
    total = round_basis(Decimal(typed.strip().lstrip("$")) * Decimal(repr(float(qty))) if per_unit
                        else Decimal(typed.strip().lstrip("$")))
    return None if total < round_basis(landed) else {"cost_total": {"new": round_basis(total - round_basis(landed))}}


def test_a_cost_written_back_is_read_against_the_text_its_door_opened_with():
    """Generated: every door x lots (dividing or not, 1 to 10 decimals, quantities 0.3 to 100000,
    with and without freight) x typed texts. The opening text sent back, however spaced, with a
    currency symbol or a trailing zero, is no change; any text of a different value is applied
    exactly (a unit door: typed x quantity), and the text the door then opens with is no change
    again. Nothing is compared at a precision."""
    import random
    from celerp_inventory.services import CostTotalBelowLanded, cost_written_back
    rng = random.Random(412)
    checked = 0
    for _ in range(300):
        qty, total, landed = _carried(rng)
        flat = _flat(qty, total, landed)
        for door, (_, per_unit) in _RULE_DOORS.items():
            opened = _opening(door, flat)
            assert opened, (door, qty, total)
            for typed in _variants(opened, rng):
                case = (door, qty, total, landed, opened, typed)
                if Decimal(typed.strip().lstrip("$")) == Decimal(opened):
                    assert cost_written_back(flat, typed, opened=opened, now=opened, per_unit=per_unit) is None, case
                    continue
                want = _expected(typed, qty, landed, per_unit)
                if want is None:
                    with pytest.raises(CostTotalBelowLanded):
                        cost_written_back(flat, typed, opened=opened, now=opened, per_unit=per_unit)
                    continue
                got = cost_written_back(flat, typed, opened=opened, now=opened, per_unit=per_unit)
                assert got == want, case
                after = _flat(qty, round(want["cost_total"]["new"] + landed, 10), landed,
                              cost_base=want["cost_total"]["new"])
                reopened = _opening(door, after)
                assert cost_written_back(after, reopened, opened=reopened, now=reopened, per_unit=per_unit) is None, case
                checked += 1
    assert checked > 5000, checked


@pytest.mark.parametrize("door", sorted(_RULE_DOORS))
def test_placeholders_unknown_costs_empty_lots_and_stale_editors_at_every_door(door):
    """An empty editor or the "--" an empty cell shows, sent back, is no change. A lot whose goods
    cost is not known opens every editor empty, so any number sent there sets (confirms) it. A
    unit typed on a lot without units is refused. An editor opened on a cost that changed since
    writes nothing when it sends back what it opened with or what is there now; any other value is
    applied here and refused by the writer's item-version check (test_cost_write_stale_pg)."""
    from celerp_inventory.services import CostNeedsQuantity, cost_written_back
    kind, per_unit = _RULE_DOORS[door]
    for empty in ("", "  ", "--", " -- "):
        assert cost_written_back(_flat(3, None), empty, opened="", now="", per_unit=per_unit) is None
    if kind == "ui":
        marked = _flat(3, 100.0, **{MARK: True})
        assert _opening(door, marked) == ""
        for empty in ("", "--"):
            assert cost_written_back(marked, empty, opened="", now="", per_unit=per_unit) is None
        typed, total = ("33.333333", 99.999999) if per_unit else ("100", 100.0)
        assert cost_written_back(marked, typed, opened="", now="", per_unit=per_unit) == {"cost_total": {"new": total}}
    empty_lot = _flat(0, None, cost_price=12.0)
    if per_unit:
        with pytest.raises(CostNeedsQuantity):
            cost_written_back(empty_lot, "13", opened=_opening(door, empty_lot), now=_opening(door, empty_lot), per_unit=True)
    else:
        assert cost_written_back(_flat(0, None), "50", opened="", now="", per_unit=False) == {"cost_total": {"new": 50.0}}
    opened, now = ("12.3456", "15.00") if per_unit else ("1234.56", "1500.00")
    lot = _flat(100, float(Decimal(now) * (100 if per_unit else 1)))
    assert cost_written_back(lot, opened, opened=opened, now=now, per_unit=per_unit) is None
    assert cost_written_back(lot, now, opened=opened, now=now, per_unit=per_unit) is None
    assert cost_written_back(lot, "13", opened=opened, now=now, per_unit=per_unit) == {
        "cost_total": {"new": 1300.0 if per_unit else 13.0}}


_PARSED = [("12.30", Decimal("12.30")), (" 12.30 ", Decimal("12.30")), ("$1,234.56", Decimal("1234.56")),
           ("1,234,567.5", Decimal("1234567.5")), ("\u0e3f 99", Decimal("99")), ("\u20ac12.5", Decimal("12.5")),
           (".5", Decimal(".5")), ("\u0661\u0662", Decimal("12")), ("7.", Decimal("7")), ("0", Decimal("0")),
           ("", None), ("   ", None), ("--", None)]
_REFUSED = ["12,35", "1,23", "1,2345.6", "1e3", "1E-2", "nan", "NaN", "inf", "-inf", "-5", "- 5", "12.3.4",
            "abc", "$", "1 000", "0x10", "12..3"]


@pytest.mark.parametrize("text,value", _PARSED)
def test_the_one_cost_parser_reads(text, value):
    from celerp_inventory.services import parse_cost
    assert parse_cost(text) == value


@pytest.mark.parametrize("text", _REFUSED)
def test_the_one_cost_parser_refuses_and_says_how_to_write_a_cost(text):
    """A decimal comma, an exponent, nan or inf and a negative are refused, never read as some
    other number; the refusal says to use a dot for decimals. Every door reads through it."""
    from celerp_inventory.services import CostTextInvalid, cost_written_back, parse_cost
    with pytest.raises(CostTextInvalid) as refused:
        parse_cost(text)
    assert "dot" in refused.value.detail["message"]
    for per_unit in (False, True):
        with pytest.raises(CostTextInvalid):
            cost_written_back(_flat(3, 100.0), text, opened="100.00", now="100.00", per_unit=per_unit)


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


async def _children(session, auth, sku: str | None) -> list[dict]:
    from celerp.models.projections import Projection
    from sqlalchemy import select
    session.expire_all()
    rows = (await session.scalars(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).all()
    return [r.state for r in rows if sku is None or (r.state or {}).get("sku") == sku]


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


_CONFIRM_DOORS = ["cell_total", "cell_unit", "pricing_unit", "pricing_total", "transform"]


@pytest.mark.parametrize("door", _CONFIRM_DOORS)
async def test_typing_the_carried_value_on_a_marked_lot_confirms_it(client, session, auth, door):
    """A lot of unknown goods cost opens its cost edit empty, so the value the user types there
    is what they know, even when it is the value the lot carries: every door confirms it."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    m = await _marked(client, auth)
    parent = await _state(session, auth, m)
    assert parent[MARK] is True and parent["cost_total"] == 100.0, parent
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
            r = await ui.patch(f"/api/items/{m}/field/{field}", data={"value": "100" if door == "cell_total" else "20"})
        else:
            r = await ui.post(f"/api/items/{m}/price",
                              data={"cost_price_total": "100"} if door == "pricing_total" else {"cost_price": "20"})
    assert r.status_code == 200, r.text[:300]
    st = next(iter(await _children(session, auth, sku))) if door == "transform" else await _state(session, auth, m)
    assert MARK not in st and st["cost_total"] == 100.0, (door, st)


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


@pytest.mark.parametrize("door", ["cell_unit", "pricing_unit"])
async def test_a_unit_cost_typed_on_a_lot_without_units_is_refused(client, session, auth, door):
    """A unit cost needs units to apply to: on an empty lot it is refused with "set the quantity
    first" and nothing is written; a total is taken."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    lot = await _item(client, auth, qty=0)
    before = _cost_fields(await _state(session, auth, lot))
    async with _browser(_token(auth)) as ui:
        if door == "cell_unit":
            r = await ui.patch(f"/api/items/{lot}/field/cost_price", data={"value": "12.5"})
            assert "cell--error" in r.text
        else:
            r = await ui.post(f"/api/items/{lot}/price", data={"cost_price": "12.5"})
        assert r.status_code == 200 and t("inventory.cost_unit_needs_quantity") in r.text, r.text[:300]
        assert _cost_fields(await _state(session, auth, lot)) == before
        await ui.post(f"/api/items/{lot}/price", data={"cost_price_total": "50"})
    assert (await _state(session, auth, lot))["cost_total"] == 50.0


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


# ── the editor's text, its staleness, and text that is no cost, at the HTTP doors ──

_UI_EDITORS = ["cell_unit", "cell_total", "cost_total_field", "pricing_unit", "pricing_total", "transform"]
_CELL_FIELD = {"cell_unit": "cost_price", "cell_total": "cost_price_total", "cost_total_field": "cost_total"}


async def _opened_at(ui, door: str, lot: str) -> tuple[str, str]:
    """The text the door's editor opens with and the item version it opened on, as the page holds them."""
    if door in _CELL_FIELD:
        page = (await ui.get(f"/api/items/{lot}/field/{_CELL_FIELD[door]}/edit")).text
        value = _input(page, name="value")
        sent = re.search(r'hx-vals=\'(\{[^\']*\})\'', page) or re.search(r'hx-vals="([^"]*)"', page)
        vals = json.loads(html.unescape(sent.group(1))) if sent else {}
        assert vals.get("opened") == value and isinstance(vals.get("opened_version"), int), page
        return value, str(vals["opened_version"])
    if door == "transform":
        page = (await ui.get("/api/items/bulk/transform-preview", params={"entity_id": lot})).text
        assert _input(page, name="child_cost_total_opened") == _input(page, name="child_cost_total")
        return _input(page, name="child_cost_total"), _input(page, name="child_cost_total_opened_version")
    page = (await ui.get(f"/inventory/{lot}?tab=pricing")).text
    which = "unit" if door == "pricing_unit" else "total"
    assert _input(page, id=f"opened_{which}_cost_price") == _input(page, id=f"{which}_cost_price")
    return _input(page, id=f"{which}_cost_price"), _input(page, id="opened_cost_version")


async def _send(ui, door: str, lot: str, typed: str, opened: tuple[str, str] | None, state: dict | None = None):
    """Send ``typed`` at the door, with the text and item version its editor opened with."""
    extra = {} if opened is None else {"opened": opened[0], "opened_version": opened[1]}
    if door in _CELL_FIELD:
        return await ui.patch(f"/api/items/{lot}/field/{_CELL_FIELD[door]}", data={"value": typed, **extra})
    if door == "transform":
        return await ui.post("/api/items/bulk/transform", data={
            "entity_id": lot, "child_sku": f"T-{uuid.uuid4().hex[:6]}", "child_qty": str(state["quantity"]),
            "child_category": state.get("category") or "", "child_sell_by": state.get("sell_by") or "",
            "child_cost_total": typed, **({} if opened is None else {
                "child_cost_total_opened": opened[0], "child_cost_total_opened_version": opened[1]})})
    return await ui.post(f"/api/items/{lot}/price", data={
        "cost_price" if door == "pricing_unit" else "cost_price_total": typed, **extra})


@pytest.mark.parametrize("door", _UI_EDITORS)
async def test_every_cost_editor_of_a_lot_of_unknown_goods_cost_opens_empty(client, session, auth, door):
    """Its editor offers no number to confirm by accident; empty or "--" sent back changes nothing."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    m = await _marked(client, auth)
    before = _cost_fields(await _state(session, auth, m))
    async with _browser(_token(auth)) as ui:
        opened = await _opened_at(ui, door, m)
        assert opened[0] == ""
        if door != "transform":
            for empty in ("", "--"):
                r = await _send(ui, door, m, empty, opened)
                assert r.status_code == 200, r.text[:300]
    assert _cost_fields(await _state(session, auth, m)) == before


@pytest.mark.parametrize("door", _UI_EDITORS)
async def test_a_cost_that_changed_since_its_editor_opened_is_not_overwritten(client, session, auth, door):
    """Two staff on one lot: one opens the cost, the other sets 15.00 a unit. The first one's
    untouched text sent back writes nothing (no revert); a new value typed over the stale one is
    refused by the writer's item-version check (409, nothing written) with the cost as it is now
    in the editor; sent again from there it is applied."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    lot = await _item(client, auth, qty=100, cost_total=1234.56)
    async with _browser(_token(auth)) as clerk, _browser(_token(auth)) as buyer:
        opened = await _opened_at(clerk, door, lot)
        assert (await buyer.post(f"/api/items/{lot}/price", data={"cost_price": "15"})).status_code == 200
        assert (await _state(session, auth, lot))["cost_total"] == 1500.0
        events = await _cost_events(session, auth, lot)
        if door != "transform":
            r = await _send(clerk, door, lot, opened[0], opened)
            assert r.status_code == 200, r.text[:300]
            assert (await _state(session, auth, lot))["cost_total"] == 1500.0
            assert await _cost_events(session, auth, lot) == events
        typed = "13" if door.endswith("unit") else "1300"
        r = await _send(clerk, door, lot, typed, opened, await _state(session, auth, lot))
        now = "15.00" if door.endswith("unit") else "1500.00"
        assert t("inventory.cost_version_stale") in r.text, r.text[:400]
        if door != "transform":
            assert r.status_code == 409 and r.headers.get("X-Swap-Refusal") == "1"
        assert (await _state(session, auth, lot))["cost_total"] == 1500.0
        assert await _cost_events(session, auth, lot) == events
        if door in _CELL_FIELD:
            assert _input(r.text, name="value") == now
        reopened = await _opened_at(clerk, door, lot)
        assert reopened[0] == now and reopened[1] != opened[1]
        r = await _send(clerk, door, lot, typed, reopened, await _state(session, auth, lot))
        assert r.status_code == 200, r.text[:300]
    if door == "transform":
        return
    assert (await _state(session, auth, lot))["cost_total"] == 1300.0


@pytest.mark.parametrize("text", ["nan", "inf", "12,35", "1e3", "-5", "abc"])
@pytest.mark.parametrize("door", _UI_EDITORS)
async def test_text_that_is_no_cost_is_refused_at_every_door_and_nothing_is_written(client, session, auth, door, text):
    """Not a 500 and never some other number: the cell keeps its editor open in its invalid-input
    state with the reason, the Pricing tab and transform page say why, and nothing is written."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    lot = await _item(client, auth, qty=10, cost_total=100.0)
    state, lots = await _state(session, auth, lot), len(await _children(session, auth, None))
    async with _browser(_token(auth)) as ui:
        r = await _send(ui, door, lot, text, None, state)
    assert r.status_code == 200 and t("inventory.cost_text_invalid") in r.text, (door, r.status_code, r.text[:300])
    if door in _CELL_FIELD:
        assert "cell--error" in r.text and _input(r.text, name="value") == text
    assert _cost_fields(await _state(session, auth, lot)) == _cost_fields(state)
    assert len(await _children(session, auth, None)) == lots


# ── the round-3 cases: a unit typed coarser than the editor opened ───────────

@pytest.mark.parametrize("door", ["cell_unit", "pricing_unit"])
@pytest.mark.parametrize("qty,value,typed,want", [(100, 1234.56, "12.35", 1235.0), (100000, 1234560.0, "12.35", 1235000.0),
                                                  (3, 100.0, "33.33", 99.99)])
async def test_a_unit_typed_coarser_than_its_editor_opened_is_applied(client, session, auth, door, qty, value, typed, want):
    """The unit editor opens on the lot's unit as the cell shows it (12.3456); 12.35 typed over
    it is the lot's new value, 12.35 x the quantity, at the list cell as on the Pricing tab."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    lot = await _item(client, auth, qty=qty, cost_total=value)
    async with _browser(_token(auth)) as ui:
        opened = await _opened_at(ui, door, lot)
        assert Decimal(opened[0]) != Decimal(typed)
        r = await _send(ui, door, lot, typed, opened)
        assert r.status_code == 200, r.text[:300]
        if door == "cell_unit":
            assert _money_text((await ui.get(f"/api/items/{lot}/field/cost_price/display")).text) == typed
    assert (await _state(session, auth, lot))["cost_total"] == want, opened


async def test_the_cost_total_field_opens_on_the_total_it_shows_and_a_different_one_is_applied(client, session, auth):
    """The cost_total field door opens at the currency's precision like the total cell (100.00
    for 100.004); saved back that is no change, and 100.01 typed is the lot's value."""
    await _seed(session, auth)
    await _cost_column(session, auth)
    lot = await _item(client, auth, qty=3, cost_total=100.004)
    async with _browser(_token(auth)) as ui:
        opened = await _opened_at(ui, "cost_total_field", lot)
        assert opened[0] == "100.00"
        await _send(ui, "cost_total_field", lot, opened[0], opened)
        assert (await _state(session, auth, lot))["cost_total"] == 100.004
        await _send(ui, "cost_total_field", lot, "100.01", opened)
    assert (await _state(session, auth, lot))["cost_total"] == 100.01


# ── the item file: a total read first, and every cost change listed before import ──

async def test_the_import_reads_a_rows_total_first_and_refuses_a_row_giving_both(client, session, auth):
    """A spreadsheet can round a unit it saves back, so a row's total decides: the importer never
    reads a unit cost beside it (the owner reads the total first, the builder refuses both)."""
    from celerp_inventory.services import _pop_cost_change
    state = _flat(3, 100.0)
    assert _pop_cost_change({"cost_total": 100.0, "cost_price": 33.33}, state) is None
    assert _pop_cost_change({"cost_total": 120.0, "cost_price": 33.33}, state) == {"cost_total": {"new": 120.0}}
    await _cost_column(session, auth)
    await _seed(session, auth)
    lot = await _item(client, auth, qty=3, cost_total=100.0)
    row = await _exported_row(client, auth, lot, "sku,name,quantity,sell_by,cost_price,cost_price_total")
    r = await _import(client, auth, {**row, "cost_price": "33.33", "cost_price_total": "100"})
    assert any(e.get("code") == "price_unit_total_conflict" for e in r.json()["detail"]["errors"]), r.text[:300]
    assert (await _state(session, auth, lot))["cost_total"] == 100.0


async def test_the_import_preview_lists_every_cost_change_before_it_is_confirmed(client, session, auth):
    """Old and new whole values per lot, the unchanged rows not listed, on the API preview and on
    the importer's review page."""
    await _cost_column(session, auth)
    await _seed(session, auth)
    a = await _item(client, auth, qty=3, cost_total=100.0)
    b = await _item(client, auth, qty=100, cost_total=1234.56)
    cols = "sku,name,quantity,sell_by,cost_price_total"
    ra, rb = await _exported_row(client, auth, a, cols), await _exported_row(client, auth, b, "sku,name,quantity,sell_by,cost_price")
    r = await client.post("/items/import/rows/preview", headers=auth["headers"], json={
        "rows": [{**ra, "cost_price_total": "120"}, {**rb, "cost_price": "12.35"}], "upsert": True,
        "idempotency_key": f"wb-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, r.text[:300]
    changes = r.json()["cost_changes"]
    assert changes == [{"row": 1, "sku": ra["sku"], "old": "100.0", "new": "120.0"},
                       {"row": 2, "sku": rb["sku"], "old": "1234.56", "new": "1235.0"}], changes
    r = await client.post("/items/import/rows/preview", headers=auth["headers"], json={
        "rows": [ra, rb], "upsert": True, "idempotency_key": f"wb-{uuid.uuid4().hex[:6]}"})
    assert r.json()["cost_changes"] == []
    from ui.routes.inventory import _import_cost_changes
    from fasthtml.common import to_xml
    shown = to_xml(_import_cost_changes(changes))
    assert t("inventory.import_cost_changes", count=2) in shown
    assert t("inventory.import_cost_change", row=2, sku=rb["sku"], old="1234.56", new="1235.0") in shown
