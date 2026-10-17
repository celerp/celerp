# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Importing an opening register: rows from a file become opening assets at the cutover.

Each row is a ``RegisterRow``. A row becomes an asset with one ``opening`` movement posted
at the cutover, the import batch as its provenance. Nothing is posted to the ledger and no
source line is claimed: the reconciliation shows what the ledger holds against what was
imported. A row disposed of before the cutover is kept as history, not as an asset.

Rows with problems, and rows left out, wait in the batch as rejected rows until they are
fixed and imported. Undo import undoes every import into the batch together.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from celerp_fixed_assets import opening, recon, setup
from celerp_fixed_assets.projections import money
from celerp_fixed_assets.operations import (
    WRITTEN_OFF_OVER_COST, _create_asset, _date, _dec, _effects, _money_in, _move, _next_numbers,
    _run, _undo, after_cutover, conflict, tag_taken)
from celerp_fixed_assets.store import _emit, _entities, _state

KEYS = ("manage_accounting",)
MAX_ROWS = 10_000
BATCH = "fa_import"
ROW = "fa_import_row"
ROW_STATUSES = ("imported", "rejected", "history")
_POLICY_COLUMNS = ("method", "life_months", "residual_percent", "total_units")
_AMOUNTS = (("cost", "Cost"), ("accumulated_depreciation", "Accumulated depreciation"),
            ("accumulated_impairment", "Accumulated impairment"), ("revaluation_surplus", "Revaluation surplus"))


@dataclass(frozen=True)
class RegisterRow:
    """One asset as another register holds it. Every value is the text the register gave."""
    number: str = ""
    name: str = ""
    class_: str = ""
    description: str = ""
    serial: str = ""
    location: str = ""
    custodian: str = ""
    acquired_date: str = ""
    available_date: str = ""
    cost: str = ""
    residual_percent: str = ""
    method: str = ""
    life_months: str = ""
    total_units: str = ""
    accumulated_depreciation: str = ""
    accumulated_impairment: str = ""
    revaluation_surplus: str = ""
    disposed_date: str = ""
    disposal_proceeds: str = ""
    parent_number: str = ""
    tag: str = ""

    @classmethod
    def of(cls, values) -> RegisterRow:
        if not isinstance(values, dict):
            raise setup.unprocessable("Every row is a set of named values.")
        unknown = set(values) - set(COLUMNS)
        if unknown:
            raise setup.unprocessable(f"{sorted(unknown)[0]} is not a register column.")
        return cls(**{_attr(k): str(v).strip() for k, v in values.items() if v is not None})

    def values(self) -> dict:
        """The columns the row has a value in, by column name."""
        return {k: getattr(self, _attr(k)) for k in COLUMNS if getattr(self, _attr(k))}


def _attr(column: str) -> str:
    return "class_" if column == "class" else column


COLUMNS = tuple("class" if f.name == "class_" else f.name for f in fields(RegisterRow))


@dataclass
class _Books:
    """What every row is checked against, read once."""
    fa: dict
    settings: dict
    cut: str
    classes: dict
    assets: list
    numbers: dict
    locations: list
    members: list
    surplus: str | None
    currency: str


def _named(candidates: list[dict], value: str, *keys: str) -> list[dict]:
    """The candidates whose ``keys`` hold ``value``: the exact match when there is one,
    else every match without case."""
    exact = [c for c in candidates if any(c.get(k) == value for k in keys)]
    return exact or [c for c in candidates if any(str(c.get(k) or "").casefold() == value.casefold() for k in keys)]


async def _books(session, company_id, fa: dict) -> _Books:
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    from celerp.models.company import Location, User
    from celerp.services.money import books_currency

    classes, surplus, _, _ = await recon._accounts(session, company_id)
    assets = await _entities(session, company_id, "fa_asset")
    locations = (await session.execute(select(Location.id, Location.name).where(
        Location.company_id == company_id))).all()
    members = (await session.execute(select(User.id, User.name, User.email).join(
        UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == company_id, UserCompany.is_active.is_(True)))).all()
    settings = await setup.company_settings(session, company_id)
    return _Books(
        fa=fa, settings=settings, cut=setup.cutover(fa),
        classes={c["name"].casefold(): c for c in classes.values() if not c.get("archived")},
        assets=assets, numbers={str(s.get("number") or "").casefold(): (eid, s) for eid, s in assets},
        locations=[{"id": str(lid), "name": name} for lid, name in locations],
        members=[{"user_id": str(uid), "name": name, "email": email} for uid, name, email in members],
        surplus=surplus, currency=books_currency(settings))


# ── Checking rows ────────────────────────────────────────────────────────────


@dataclass
class _Checked:
    row: int
    values: dict
    errors: list
    history: bool = False
    excluded: bool = False
    cls: dict | None = None
    figures: dict | None = None
    fields: dict | None = None
    parent: str | None = None
    parent_row: int | None = None
    number: str | None = None

    @property
    def creates(self) -> bool:
        return not self.history and not self.excluded and not self.errors


def _class_names(books: _Books) -> str:
    return ", ".join(c["name"] for c in books.classes.values())


def _check(books: _Books, numbered: list[tuple[int, RegisterRow]], excluded: set[int]) -> list[_Checked]:
    """Every row checked, each problem named against its column. A row's number, tag and
    parent are also checked against the earlier rows being imported with it."""
    out: list[_Checked] = []
    numbers: dict[str, tuple[int, str]] = {}
    tags: dict[str, int] = {}
    for n, register_row in numbered:
        v = register_row.values()
        row = _Checked(row=n, values=v, errors=[], excluded=n in excluded)

        def bad(field, message, row=row):
            row.errors.append({"row": row.row, "field": field, "message": message})

        def read(field, call):
            try:
                return call()
            except HTTPException as exc:
                bad(field, str(exc.detail))
                return None

        out.append(row)
        if not v.get("name"):
            bad("name", "Every asset needs a name.")
        if v.get("disposed_date"):
            row.history = True
            disposed = read("disposed_date", lambda: _date(v["disposed_date"], "The disposed date"))
            if disposed and disposed > books.cut:
                bad("disposed_date", f"Disposed after the cutover {books.cut}. Import the asset as held at the "
                                     "cutover, then record its disposal.")
            if v.get("disposal_proceeds"):
                read("disposal_proceeds", lambda: _money_in(v["disposal_proceeds"], "Disposal proceeds", books.currency))
            continue
        cls = books.classes.get(v.get("class", "").casefold())
        if cls is None:
            bad("class", f"No asset class is named {v['class']}. Use one of: {_class_names(books)}." if v.get("class")
                else f"Every asset needs a class. Use one of: {_class_names(books)}.")
        _check_dates(books, v, bad, read)
        figures = {k: read(k, lambda k=k, label=label: _money_in(v.get(k), label, books.currency,
                                                                            positive=k == "cost"))
                   for k, label in _AMOUNTS}
        if None not in (figures["cost"], figures["accumulated_depreciation"], figures["accumulated_impairment"]) and (
                figures["accumulated_depreciation"] + figures["accumulated_impairment"] > figures["cost"]):
            bad("accumulated_depreciation", WRITTEN_OFF_OVER_COST)
        if figures["revaluation_surplus"] and cls and setup.measurement(cls) != "revaluation":
            bad("revaluation_surplus", f"{cls['name']} is measured at cost, so its assets carry no revaluation "
                                       "surplus.")
        row.cls, row.figures = cls, figures
        row.fields = {"policy": _policy(books, cls, v, bad) if cls else None}
        number = v.get("number")
        if number:
            first = numbers.get(number.casefold())
            if first:
                bad("number", f"{first[1]} is on row {first[0]} as well.")
            elif number.casefold() in books.numbers:
                bad("number", f"{number} is already an asset number here.")
            elif not row.excluded:
                numbers[number.casefold()] = (n, number)
            row.number = number
        tag = v.get("tag")
        if tag:
            problem = tag_taken(books.assets, "", tag)
            if problem:
                bad("tag", problem)
            elif tag.casefold() in tags:
                bad("tag", f"Tag {tag} is on row {tags[tag.casefold()]} as well.")
            elif not row.excluded:
                tags[tag.casefold()] = n
        _place(books, row, v, bad)
        parent = v.get("parent_number")
        if parent:
            in_file = numbers.get(parent.casefold())
            held = books.numbers.get(parent.casefold())
            if in_file and in_file[0] != n:
                row.parent_row = in_file[0]
            elif held and held[1].get("status") != "undone":
                row.parent = held[0]
            else:
                bad("parent_number", f"No asset is numbered {parent}. Use an existing asset's number, or the "
                                     "number of an earlier row.")
    by_row = {r.row: r for r in out}
    for row in out:
        if row.parent_row and not row.errors and not by_row[row.parent_row].creates:
            row.errors.append({"row": row.row, "field": "parent_number",
                               "message": f"The parent on row {row.parent_row} is not being imported."})
    return out


def _check_dates(books: _Books, v: dict, bad, read) -> None:
    if not v.get("available_date"):
        bad("available_date", "Every asset needs the date it was available for use.")
    for field, label in (("available_date", "The available-for-use date"), ("acquired_date", "The acquired date")):
        if v.get(field):
            day = read(field, lambda field=field, label=label: _date(v[field], label))
            if day and day > books.cut:
                bad(field, after_cutover(books.cut))


def _policy(books: _Books, cls: dict, v: dict, bad) -> dict:
    """The class's policy with the row's own method, life, residual or units over it."""
    policy = setup.policy_of(cls, books.fa)
    given = [c for c in _POLICY_COLUMNS if v.get(c)]
    if not given:
        return policy
    if v.get("method"):
        policy["method"] = v["method"]
    if v.get("life_months"):
        if not v["life_months"].isdigit():
            bad("life_months", "The useful life is a whole number of months.")
            return policy
        policy["life_months"] = int(v["life_months"])
    if v.get("residual_percent"):
        policy["residual_percent"] = v["residual_percent"]
    if v.get("total_units"):
        policy["params"] = {**policy["params"], "total_units": v["total_units"]}
    try:
        setup.check_policy(policy, books.settings)
    except HTTPException as exc:
        bad("method" if "method" in given else given[0], str(exc.detail))
    return policy


def _place(books: _Books, row: _Checked, v: dict, bad) -> None:
    """Where the asset is and who holds it: a location by name, a member by email or name,
    else a person named here who is not a user."""
    if v.get("location"):
        found = _named(books.locations, v["location"], "name")
        if not found:
            bad("location", f"No location is named {v['location']}. Add it under locations first, or leave "
                            "the column empty.")
        elif len(found) > 1:
            bad("location", f"Several locations are named {v['location']}: "
                            f"{', '.join(f['name'] for f in found)}. Use the name exactly as it is written.")
        else:
            row.fields["location_id"] = found[0]["id"]
    if v.get("custodian"):
        found = _named(books.members, v["custodian"], "email", "name")
        if len(found) > 1:
            bad("custodian", f"Several members are named {v['custodian']}. Use their email address.")
        row.fields["custodian"] = ({"user_id": found[0]["user_id"], "name": found[0]["name"]} if len(found) == 1
                                   else {"name": v["custodian"], "not_user": True})


def _duplicates(books: _Books, checked: list[_Checked]) -> list[dict]:
    """Rows that look like an asset already held, or like an earlier row: the same serial,
    or the same name, cost and available-for-use date. They are warnings, not errors."""
    live = [s for _, s in books.assets if s.get("status") != "undone"]
    out, seen = [], []
    for row in checked:
        if not row.creates:
            continue
        v = row.values
        serial = v.get("serial", "").casefold()
        same = (v["name"].casefold(), row.figures["cost"], v["available_date"])
        for s in live:
            if serial and (s.get("serial") or "").casefold() == serial:
                out.append({"row": row.row, "message": f"Serial {v['serial']} is already on {s['number']} "
                                                       f"{s.get('name')}."})
            elif ((s.get("name") or "").casefold(), _dec(s.get("cost")), s.get("available_date")) == same:
                out.append({"row": row.row, "message": f"{s['number']} {s.get('name')} has the same name, cost "
                                                       "and available-for-use date."})
        for earlier in seen:
            if serial and earlier.values.get("serial", "").casefold() == serial:
                out.append({"row": row.row, "message": f"Serial {v['serial']} is on row {earlier.row} as well."})
            elif (earlier.values["name"].casefold(), earlier.figures["cost"], earlier.values["available_date"]) == same:
                out.append({"row": row.row, "message": f"Row {earlier.row} has the same name, cost and "
                                                       "available-for-use date."})
        seen.append(row)
    return out


def _balances(books: _Books, rows: list[_Checked]) -> dict[str, Decimal]:
    """What the rows add to each class account, signed as the ledger holds it."""
    out: dict[str, Decimal] = {}
    for row in rows:
        for account, amount in recon._balances(row.cls, row.figures, books.surplus).items():
            out[account] = out.get(account, Decimal(0)) + amount
    return out


async def _plan(session, company_id, books: _Books, numbered, excluded: set[int], preview: bool):
    checked = _check(books, numbered, excluded)
    creating = [r for r in checked if r.creates]
    explicit = [r.number for r in creating if r.number]
    assigned = iter(await _next_numbers(session, company_id, sum(1 for r in creating if not r.number), explicit))
    for row in creating:
        row.number = row.number or next(assigned)
    errors = [e for r in checked if not r.excluded for e in r.errors]
    if errors and not preview:
        rows = sorted({e["row"] for e in errors})
        raise setup.unprocessable("Fix or leave out the rows with problems first: rows "
                                  + ", ".join(str(n) for n in rows) + ".")
    importing = _balances(books, creating)
    accounts = [{**a, "importing": money(importing.get(a["account"]), books.currency),
                 "difference": money(_dec(a["ledger"]) - _dec(a["registered"]) - importing.get(a["account"], 0),
                                     books.currency)}
                for a in (await opening.view(session, company_id))["accounts"]]
    shown = {
        "as_at": books.cut,
        "counts": {"create": len(creating), "blocked": sum(1 for r in checked if r.errors and not r.excluded),
                   "excluded": sum(1 for r in checked if r.excluded),
                   "history": sum(1 for r in checked if r.history and not r.errors and not r.excluded)},
        "errors": errors,
        "duplicates": _duplicates(books, checked),
        "assets": [{"row": r.row, "number": r.number, "name": r.values["name"], "cost": money(r.figures["cost"], books.currency)}
                   for r in creating],
        "accounts": accounts,
    }
    return shown, {"checked": checked, "importing": importing}


async def _create(session, company_id, user_id, books: _Books, batch_id: str, checked: list[_Checked],
                  op_key: str) -> tuple[list[dict], list[str]]:
    """Each row that imports becomes an asset with its opening movement. Rows are created
    in order, so a parent on an earlier row exists before its parts."""
    made: dict[int, str] = {}
    assets, movements = [], []
    for row in (r for r in checked if r.creates):
        v, f = row.values, row.fields
        asset_id = await _create_asset(
            session, company_id, user_id, number=row.number, name=v["name"], class_id=row.cls["id"],
            status="active", available_date=_date(v["available_date"], "available_date"), op_key=op_key,
            policy=f["policy"], tag=v.get("tag"), serial=v.get("serial"), description=v.get("description"),
            location_id=f.get("location_id"), custodian=f.get("custodian"),
            acquired_date=_date(v["acquired_date"], "acquired_date") if v.get("acquired_date") else None,
            parent_id=made[row.parent_row] if row.parent_row else row.parent, import_batch=batch_id,
            import_row=row.row)
        made[row.row] = asset_id
        movements.append(await _move(
            session, company_id, user_id, asset_id, kind="opening", operation="opening",
            effective=_date(v["available_date"], "available_date"), posting=books.cut,
            amounts={k: x for k, x in row.figures.items() if x}, op_key=op_key, quantity=1, provenance=batch_id,
            surplus_account=books.surplus if row.figures.get("revaluation_surplus") else None))
        assets.append({"id": asset_id, "number": row.number, "row": row.row})
    return assets, movements


async def _record_rows(session, company_id, user_id, batch_id: str, checked: list[_Checked], assets: list[dict]):
    by_row = {a["row"]: a for a in assets}
    for row in checked:
        asset = by_row.get(row.row)
        status = ("imported" if asset else "history" if row.history and not row.errors and not row.excluded
                  else "rejected")
        await _emit(session, company_id, user_id, _row_id(batch_id, row.row), ROW, "fa.import.row", {
            "batch_id": batch_id, "row": row.row, "status": status, "values": row.values, "errors": row.errors,
            "asset_id": asset["id"] if asset else None, "number": asset["number"] if asset else None})


def _row_id(batch_id: str, row: int) -> str:
    return f"fa:improw:{batch_id.removeprefix('fa:import:')}:{row}"


def _totals(before: dict, added: dict[str, Decimal]) -> dict:
    out = {k: _dec(v) for k, v in before.items()}
    for account, amount in added.items():
        out[account] = out.get(account, Decimal(0)) + amount
    return {k: str(v) for k, v in sorted(out.items())}


async def _record_batch(session, company_id, user_id, batch_id: str, batch: dict) -> None:
    await _emit(session, company_id, user_id, batch_id, BATCH, "fa.import.recorded", batch)


async def _batch_rows(session, company_id, batch_id: str) -> list[dict]:
    rows = [s for _, s in await _entities(session, company_id, ROW) if s.get("batch_id") == batch_id]
    return sorted(rows, key=lambda r: r["row"])


def _counts(rows: list[dict]) -> dict:
    return {status: sum(1 for r in rows if r["status"] == status) for status in ROW_STATUSES}


# ── The operations ───────────────────────────────────────────────────────────


def _excluded(value, count: int) -> set[int]:
    if value in (None, ""):
        return set()
    if not isinstance(value, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in value):
        raise setup.unprocessable("Leave out rows by their row numbers.")
    outside = [n for n in value if not 1 <= n <= count]
    if outside:
        raise setup.unprocessable(f"There is no row {outside[0]} in this file.")
    return set(value)


async def import_register(session, *, company_id, user_id, rows, as_at, source, body: dict) -> dict:
    """Preview or confirm importing ``rows`` (RegisterRow values) as the opening register at
    ``as_at``, which is the cutover. ``source`` names where the rows came from; ``body``
    carries the operation contract (preview, fingerprint, op_key) and the rows left out."""
    if not isinstance(rows, list) or not rows:
        raise setup.unprocessable("The file has no rows to import.")
    if len(rows) > MAX_ROWS:
        raise setup.unprocessable(f"Import at most {MAX_ROWS:,} rows at a time. Split the file and import each part.")
    parsed = [RegisterRow.of(v) for v in rows]
    excluded = _excluded(body.get("exclude"), len(parsed))
    source = str(source or "").strip()[:200] or "api"

    async def plan(fa):
        cut = setup.cutover(fa)
        if as_at != cut:
            raise setup.unprocessable(f"The register is imported as at the cutover {cut}.")
        books = await _books(session, company_id, fa)
        shown, context = await _plan(session, company_id, books, list(enumerate(parsed, 1)), excluded,
                                     bool(body.get("preview")))
        return shown, {**context, "books": books}

    async def apply(context, op_key):
        books, checked = context["books"], context["checked"]
        batch_id = f"fa:import:{uuid.uuid4()}"
        assets, movements = await _create(session, company_id, user_id, books, batch_id, checked, op_key)
        await _record_rows(session, company_id, user_id, batch_id, checked, assets)
        counts = _counts(await _batch_rows(session, company_id, batch_id))
        await _record_batch(session, company_id, user_id, batch_id, {
            "as_at": books.cut, "source": source, "state": "imported", "rows": len(checked), "counts": counts,
            "totals": _totals({}, context["importing"]), "op_keys": [op_key],
            "recorded_at": datetime.now(timezone.utc).isoformat(), "recorded_by": str(user_id)})
        return ({"batch": {"id": batch_id}, "assets": assets, "journals": [], "counts": counts},
                _effects(assets=[a["id"] for a in assets], movements=movements))

    return await _run(session, company_id, user_id, {**body, "rows": rows, "as_at": as_at, "source": source},
                      op_type="import", keys=KEYS, plan=plan, apply=apply)


async def _batch(session, company_id, batch_id: str) -> dict:
    batch = await _state(session, company_id, batch_id)
    if batch is None or not batch_id.startswith("fa:import:"):
        raise HTTPException(status_code=404, detail="No such import.")
    return batch


async def import_rows(session, company_id, user_id, batch_id: str, body: dict) -> dict:
    """Fix and import rows: rejected rows of a batch, with their values corrected, imported
    into the same batch."""
    fixes = body.get("rows")
    if not isinstance(fixes, list) or not fixes or not all(isinstance(f, dict) for f in fixes):
        raise setup.unprocessable("Send the rows to import with their values.")

    async def plan(fa):
        batch = await _batch(session, company_id, batch_id)
        if batch["state"] == "undone":
            raise conflict("This import was undone. Import the rows again as a new file.")
        waiting = {r["row"]: r for r in await _batch_rows(session, company_id, batch_id) if r["status"] == "rejected"}
        numbered = []
        for fix in fixes:
            n = fix.get("row")
            if n not in waiting:
                raise setup.unprocessable(f"Row {n} is not waiting in this import.")
            numbered.append((n, RegisterRow.of(fix.get("values"))))
        books = await _books(session, company_id, fa)
        shown, context = await _plan(session, company_id, books, numbered, set(), bool(body.get("preview")))
        return shown, {**context, "books": books, "batch": batch}

    async def apply(context, op_key):
        books, checked, batch = context["books"], context["checked"], context["batch"]
        assets, movements = await _create(session, company_id, user_id, books, batch_id, checked, op_key)
        await _record_rows(session, company_id, user_id, batch_id, checked, assets)
        counts = _counts(await _batch_rows(session, company_id, batch_id))
        await _record_batch(session, company_id, user_id, batch_id, {
            **batch, "counts": counts, "totals": _totals(batch.get("totals") or {}, context["importing"]),
            "op_keys": [*batch["op_keys"], op_key]})
        return ({"batch": {"id": batch_id}, "assets": assets, "journals": [], "counts": counts},
                _effects(assets=[a["id"] for a in assets], movements=movements))

    return await _run(session, company_id, user_id, {**body, "batch": batch_id}, op_type="import_rows", keys=KEYS,
                      plan=plan, apply=apply)


async def undo_import(session, company_id, user_id, batch_id: str, body: dict) -> dict:
    """Undo every import into the batch together: its assets go, the batch stays on record
    as undone. Refused, naming them, when an imported asset has changed since."""
    batch = await _batch(session, company_id, batch_id)
    if batch["state"] == "undone":
        raise conflict("This import was already undone.")

    async def done(op_key):
        await _record_batch(session, company_id, user_id, batch_id, {**batch, "state": "undone", "undone_by": op_key})

    return await _undo(session, company_id, user_id, batch["op_keys"], {**body, "batch": batch_id},
                       subject=batch_id, done=done)


# ── Reading ──────────────────────────────────────────────────────────────────


def batch_out(batch_id: str, batch: dict) -> dict:
    return {"id": batch_id, **{k: batch.get(k) for k in (
        "as_at", "source", "state", "rows", "counts", "totals", "recorded_at", "recorded_by")}}


async def batches(session, company_id) -> list[dict]:
    """Every import, newest first."""
    found = await _entities(session, company_id, BATCH)
    return [batch_out(eid, s) for eid, s in sorted(found, key=lambda e: e[1].get("recorded_at") or "", reverse=True)]


async def batch_view(session, company_id, batch_id: str, *, status: str | None, limit: int, offset: int) -> dict:
    """An import with its rows (all, or those of one status) and the opening reconciliation."""
    if status is not None and status not in ROW_STATUSES:
        raise setup.unprocessable(f"Choose rows that are {', '.join(ROW_STATUSES)}.")
    if limit < 1 or limit > 500 or offset < 0:
        raise setup.unprocessable("Choose a page of 1 to 500 rows.")
    batch = await _batch(session, company_id, batch_id)
    rows = [r for r in await _batch_rows(session, company_id, batch_id) if status is None or r["status"] == status]
    return {**batch_out(batch_id, batch), "accounts": (await opening.view(session, company_id))["accounts"],
            "rows": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}
