# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Correct: one action and one preview for any change to an asset after it was added.

A change in estimate (life, residual, method, convention, method settings) runs forward
from an effective date in a month not yet depreciated and posts nothing now. An error
correction (cost, available-for-use date, class, opening values, a capitalised amount
that was maintenance, a source line) recomputes the asset from its original date and
posts the difference on an open day: the difference falling in the current financial
year to the depreciation account, earlier years to retained earnings tagged
``prior_period_adjustment`` (or, with a reason, to the depreciation account as well).
Every correction is a set of coordinated journals and corrective movements posted all
or none, recorded as ``fa:corr:<uuid>``.

``Request correction`` saves a correction for a user who may post journals to apply;
it posts nothing.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from celerp_fixed_assets import notices, projections, reclassify, runs, setup, sources
from celerp_fixed_assets.calc.periods import fiscal_year_start_index, month_index, parse_period, period_label
from celerp_fixed_assets.operations import (
    MODULE, _claim, _date, _dec, _intent_hash, _journal_view, _lines, _money_in, _move, _op_key, _open_line, _post,
    _reversed_lines, _run, _same_intent, asset_movements, conflict)
from celerp_fixed_assets.store import _emit, _entities, _state
from celerp_fixed_assets.projections import BUCKETS, money, said

ESTIMATE_FIELDS = setup.POLICY_FIELDS
ERROR_FIELDS = ("cost", "available_date", "class_id", "opening_values", "maintenance")
SOURCE_ACTIONS = ("replace_source", "link_source", "release_source")
CLASSIFICATIONS = ("estimate", "error")
TREATMENTS = ("retained_earnings", "profit_and_loss")
NEED = "Needs Manage accounting"
_OPENING_KEYS = ("cost", "accumulated_depreciation", "accumulated_impairment")
_AD = "accumulated_depreciation"


def _sum(values) -> Decimal:
    return sum((_dec(v) for v in values), Decimal(0))


def _first_day(index: int) -> str:
    return date(index // 12, index % 12 + 1, 1).isoformat()


def _fiscal_year(settings: dict, index: int) -> int:
    """The financial year holding month ``index``, named by the calendar year it ends in."""
    start = int(str(settings.get("fiscal_year_start") or "01-01")[:2])
    return (fiscal_year_start_index(index, start) + 11) // 12


def _contributions(movements: list[dict], key: tuple | None = None) -> dict[tuple, Decimal]:
    """What the movements hold of each journal line, netted; only ``key`` when given."""
    out: dict[tuple, Decimal] = {}
    for m in movements:
        for c in m.get("contributions") or []:
            k = (c.get("journal_id"), c.get("line_index"))
            if key is None or k == key:
                out[k] = out.get(k, Decimal(0)) + _dec(c.get("amount"))
    return {k: v for k, v in out.items() if v}


def _as_list(netted: dict[tuple, Decimal], sign: int = 1) -> list[dict]:
    return [{"journal_id": j, "line_index": i, "amount": str(sign * v)} for (j, i), v in netted.items()]


# ── Classification ───────────────────────────────────────────────────────────


def default_classification(changes: dict) -> str:
    return "estimate" if set(changes) <= set(ESTIMATE_FIELDS) else "error"


def _classify(body: dict) -> tuple[dict, str, str, str | None]:
    """The changes, their classification, its default, and the reason for overriding it."""
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise setup.unprocessable("Choose what to correct.")
    unknown = set(changes) - {*ESTIMATE_FIELDS, *ERROR_FIELDS, *SOURCE_ACTIONS}
    if unknown:
        raise setup.unprocessable(f"{sorted(unknown)[0]} cannot be corrected.")
    default = default_classification(changes)
    if default == "error" and len(changes) != 1:
        raise setup.unprocessable("Correct one of cost, dates, class, opening values, maintenance or a source at a "
                                  "time, apart from depreciation settings.")
    chosen = body.get("classification") or default
    if chosen not in CLASSIFICATIONS:
        raise setup.unprocessable("A correction is a change in estimate or an error correction.")
    reason = str(body.get("override_reason") or "").strip() or None
    if chosen != default:
        if default == "error":
            raise setup.unprocessable(f"A change to {next(iter(changes))} corrects an error, so it cannot be "
                                      "treated as a change in estimate.")
        if not reason:
            raise setup.unprocessable("A reason is required to treat a change in estimate as an error correction.")
    return changes, chosen, default, reason if chosen != default else None


def overrides(body: dict) -> bool:
    changes = body.get("changes")
    chosen = body.get("classification")
    return isinstance(changes, dict) and bool(changes) and bool(chosen) and chosen != default_classification(changes)


# ── The draft a correction is planned into ───────────────────────────────────


class Draft:
    """Journals, corrective movements, claims and asset changes one correction makes."""

    def __init__(self, session, company_id, asset_id: str, asset: dict, cls: dict, movements: list[dict],
                 books: runs.Books, posting: str | None):
        self.session, self.company_id = session, company_id
        self.asset_id, self.asset, self.cls, self.new_cls = asset_id, asset, cls, cls
        self.movements, self.live, self.books, self.posting = movements, projections.live(movements), books, posting
        self.journals: list[dict] = []
        self.moves: list[dict] = []
        self.claims: list[tuple[dict, dict]] = []
        self.releases: list[str] = []
        self.changes: dict = {}
        self.prior_years: set[int] = set()
        self.prior_detail: list[dict] = []
        self.shown: dict = {}
        self.generation = 0

    def opening(self) -> bool:
        return any(m.get("operation") == "opening" for m in self.live)

    def journal(self, lines, memo: str, tags: dict | None = None) -> int:
        self.journals.append({"lines": lines, "memo": memo, "tags": tags})
        return len(self.journals) - 1

    def move(self, operation: str, effective: str, amounts: dict, contributions=(), posting: str | None = None,
             **extra) -> None:
        self.moves.append({"operation": operation, "effective_date": effective,
                           "posting_date": posting or self.posting,
                           "amounts": {k: str(v) for k, v in amounts.items()}, "contributions": list(contributions),
                           **extra})

    def hypothetical(self) -> tuple[dict, list[dict]]:
        """The asset and its movements as they stand once the correction is made."""
        planned = [{"id": f"new:{i}", **m} for i, m in enumerate(self.moves)]
        return {**self.asset, **self.changes}, [*self.live, *planned]

    def current_fy(self) -> int:
        return _fiscal_year(self.books.settings, month_index(date.fromisoformat(self.posting)))

    def retained_earnings(self) -> str:
        from celerp.services.account_roles import role_map

        account = role_map(self.books.settings).get("retained_earnings")
        if not account:
            raise setup.unprocessable("Choose the retained earnings account in Posting accounts first: earlier "
                                      "years are corrected through it.")
        return account

    def without_estimates(self) -> None:
        if runs.estimates(self.live):
            raise setup.unprocessable("This asset's depreciation settings were changed as an estimate. Undo that "
                                      "change first, or correct them as another change in estimate.")


# ── Error corrections, one per field ─────────────────────────────────────────


async def _cost(d: Draft, value, fa: dict) -> None:
    if d.asset.get("sources"):
        raise setup.unprocessable("This cost comes from its source lines. Use Replace source to change it.")
    if d.opening():
        raise setup.unprocessable("This asset came in at the cutover. Correct its opening values instead.")
    new = _money_in(value, "Cost", d.books.currency, positive=True)
    diff = new - _dec(d.asset.get("cost"))
    if not diff:
        raise setup.unprocessable(f"The cost is already {said(new, d.books.currency)}.")
    credit = await _acquisition_credit(d)
    n = d.journal(_lines(d.cls["asset_account"], credit, diff), f"{d.asset['number']} cost corrected")
    d.move("cost", d.asset["available_date"], {"cost": diff},
           [{"journal": n, "line_index": 0 if diff > 0 else 1, "amount": str(diff)}])


async def _acquisition_credit(d: Draft) -> str:
    """The account a hand-added asset's acquisition was credited to."""
    for m in d.live:
        for c in m.get("contributions") or []:
            je = await _state(d.session, d.company_id, str(c.get("journal_id")))
            for e in (je or {}).get("entries") or []:
                if _dec(e.get("credit")) > 0 and e.get("account") != d.cls["asset_account"]:
                    return e["account"]
    raise setup.unprocessable("This asset has no acquisition entry to correct its cost against.")


async def _available_date(d: Draft, value, fa: dict) -> None:
    if d.opening():
        raise setup.unprocessable("This asset came in at the cutover. Correct its opening values instead.")
    new, old = _date(value, "The available-for-use date"), d.asset.get("available_date")
    if new == old:
        raise setup.unprocessable(f"The available-for-use date is already {new}.")
    cut = setup.cutover(fa)
    if cut and new <= cut:
        raise setup.unprocessable(f"Dated on or before the cutover {cut}. An asset in use by then belongs to the "
                                  "opening register.")
    at_old = [m for m in d.live if m.get("effective_date") == old and _dec((m.get("amounts") or {}).get("cost"))]
    cost = _sum((m.get("amounts") or {}).get("cost") for m in at_old)
    if not cost:
        raise setup.unprocessable(f"Nothing was acquired on {old} to move.")
    held = _contributions(at_old)
    d.move("available_date", old, {"cost": -cost}, _as_list(held, -1))
    d.move("available_date", new, {"cost": cost}, _as_list(held))
    d.changes["available_date"] = new


async def _class_id(d: Draft, value, fa: dict) -> None:
    new = await setup.get_class(d.session, d.company_id, value)
    if new["id"] == d.cls["id"]:
        raise setup.unprocessable("The asset is already in this class.")
    if new.get("archived"):
        raise setup.unprocessable("That class is archived. Choose a class in use.")
    d.without_estimates()
    d.new_cls = new
    # The classes the asset was in, each until the date it left, read when an earlier
    # balance is mapped to the accounts it stood on then.
    d.changes.update(class_id=new["id"], policy=setup.policy_of(new, fa),
                     class_history=[*(d.asset.get("class_history") or []), {"until": d.posting, "class_id": d.cls["id"]}])
    for lines, memo in reclassify.balance_moves(
            d.asset["number"], d.cls, new, _dec(d.asset.get("cost")),
            _dec(d.asset.get(_AD)) + _dec(d.asset.get("accumulated_impairment"))):
        d.journal(lines, memo)


async def _opening_values(d: Draft, value, fa: dict) -> None:
    if not d.opening():
        raise setup.unprocessable("Only an asset brought in at the cutover has opening values.")
    if not isinstance(value, dict) or not value or set(value) - set(_OPENING_KEYS):
        raise setup.unprocessable("Opening values are a cost, accumulated depreciation and accumulated impairment.")
    held = [m for m in d.live if m.get("operation") in runs.OPENING_OPERATIONS]
    current = {k: _sum((m.get("amounts") or {}).get(k) for m in held) for k in _OPENING_KEYS}
    labels = {"cost": "Cost", "accumulated_depreciation": "Accumulated depreciation",
              "accumulated_impairment": "Accumulated impairment"}
    new = {k: _money_in(value[k], labels[k], d.books.currency, positive=k == "cost") if k in value else current[k] for k in _OPENING_KEYS}
    if new["accumulated_depreciation"] + new["accumulated_impairment"] > new["cost"]:
        raise setup.unprocessable("Accumulated depreciation and impairment cannot be more than the cost.")
    diff = {k: new[k] - current[k] for k in _OPENING_KEYS if new[k] != current[k]}
    if not diff:
        raise setup.unprocessable("The opening values are already these.")
    first = next(m for m in held if m.get("operation") == "opening")
    d.move("opening_values", first["effective_date"], diff)


async def _maintenance(d: Draft, value, fa: dict) -> None:
    from celerp.services.journal_accounts import require_destinations

    if d.opening():
        raise setup.unprocessable("This asset came in at the cutover. Correct its opening values instead.")
    account = str((value or {}).get("expense_account") or "") if isinstance(value, dict) else ""
    if not account:
        raise setup.unprocessable("Choose the expense account the maintenance belongs to.")
    await require_destinations(d.session, d.company_id, {account})
    current = d.current_fy()
    buckets: dict[bool, list[dict]] = {}
    for m in d.live:
        if _dec((m.get("amounts") or {}).get("cost")):
            year = _fiscal_year(d.books.settings, month_index(date.fromisoformat(m["effective_date"])))
            buckets.setdefault(year < current, []).append({**m, "year": year})
    number = d.asset["number"]
    for prior, carried in sorted(buckets.items(), reverse=True):
        years = sorted({m["year"] for m in carried})
        total = _sum((m.get("amounts") or {}).get("cost") for m in carried)
        if prior:
            d.prior_years.update(years)
        target = d.retained_earnings() if prior else account
        n = d.journal(_lines(target, d.cls["asset_account"], total), f"{number} expensed as maintenance",
                      {"prior_period_adjustment": years} if prior else None)
        for m in carried:
            cost = _dec(m["amounts"]["cost"])
            d.move("maintenance", m["effective_date"], {"cost": -cost},
                   [{"journal": n, "line_index": 1 if total > 0 else 0, "amount": str(-cost)}])
    d.changes["status"] = "expensed"


# ── Source lines ─────────────────────────────────────────────────────────────


async def _claimed_here(d: Draft, source: str) -> tuple[dict, dict]:
    entry = next((s for s in d.asset.get("sources") or [] if s.get("source") == source), None)
    if entry is None:
        raise setup.unprocessable("That line is not a source of this asset.")
    claim = await _state(d.session, d.company_id, sources.claim_id(source))
    if claim is None or claim.get("status") != "claimed":
        raise setup.unprocessable("That line is not registered.")
    if (claim.get("asset_ids") or []) != [d.asset_id]:
        raise setup.unprocessable("That line is shared with other assets. Undo its registration and register the "
                                  "replacement instead.")
    return entry, claim


def _carrying(d: Draft, claim: dict) -> tuple[list[dict], Decimal, tuple]:
    key = (claim.get("journal_id"), claim.get("line_index"))
    carrying = [m for m in d.live if any((c.get("journal_id"), c.get("line_index")) == key
                                         for c in m.get("contributions") or [])]
    return carrying, _contributions(carrying, key).get(key, Decimal(0)), key


def _source_ref(line: dict, amount, **extra) -> dict:
    return {"source": line["source"], "ref": line.get("document_ref"), "amount": str(amount), **extra}


async def _replace_source(d: Draft, value, fa: dict) -> None:
    from celerp.services.module_journals import JournalLine

    value = value if isinstance(value, dict) else {}
    entry, claim = await _claimed_here(d, str(value.get("old") or ""))
    line = await _open_line(d.session, d.company_id, value.get("new"))
    if line["kind"] != "acquisition":
        raise setup.unprocessable("Replace a source with a bill line or manual journal line acquiring the asset.")
    carrying, held, key = _carrying(d, claim)
    effective = min(m["effective_date"] for m in carrying)
    new_cost = _dec(line["amount"])
    number, account = d.asset["number"], d.cls["asset_account"]
    # A source on another account was moved onto the class account when it was registered.
    moved = await _state(d.session, d.company_id, f"je:mod:{MODULE}:asset:{d.asset_id.removeprefix('fa:asset:')}:reg:1")
    if moved and moved.get("status") == "posted" and held:
        other = next(e["account"] for e in moved.get("entries") or [] if _dec(e.get("credit")) > 0)
        d.journal(_lines(other, account, held), f"{number} source replaced")
    if line["account"] != account:
        d.journal([JournalLine(account=account, debit=new_cost),
                   JournalLine(account=line["account"], credit=new_cost, moves_recognized=True,
                               recognized_in=line["journal_id"])], f"{number} source replaced")
    d.move("replace_source", effective, {"cost": -held}, _as_list({key: held}, -1))
    d.move("replace_source", effective, {"cost": new_cost},
           [{"journal_id": line["journal_id"], "line_index": line["line_index"], "amount": str(new_cost)}])
    d.releases.append(entry["source"])
    d.claims.append((line, {"allocation": claim.get("allocation") or "separate", "asset_ids": [d.asset_id],
                            "numbers": claim.get("numbers") or [number]}))
    d.changes["sources"] = [_source_ref(line, new_cost) if s is entry else s for s in d.asset.get("sources") or []]
    d.generation = line["generation"]
    replaced = {id(m) for m in carrying}
    d.shown["kept"] = [{"operation": m.get("operation"), "effective_date": m["effective_date"],
                        "posting_date": m["posting_date"]} for m in d.live if id(m) not in replaced]
    d.shown["corrected"] = [{"effective_date": m["effective_date"], "posting_date": m["posting_date"],
                             "cost": m["amounts"]["cost"]} for m in d.moves]


async def _link_source(d: Draft, value, fa: dict) -> None:
    line = await _open_line(d.session, d.company_id, (value or {}).get("source") if isinstance(value, dict) else None)
    account = d.cls["asset_account"]
    if line["kind"] not in ("acquisition", "reduction") or line["account"] != account:
        raise setup.unprocessable(f"Link a line on {account}, the asset account of this asset's class.")
    amount = _dec(line["amount"]) * (1 if line["kind"] == "acquisition" else -1)
    if _dec(d.asset.get("cost")) + amount < 0:
        raise setup.unprocessable("This line reduces the cost below zero.")
    effective = max(line["date"], d.asset.get("available_date") or line["date"])
    d.move("link_source", effective, {"cost": amount},
           [{"journal_id": line["journal_id"], "line_index": line["line_index"], "amount": str(amount)}])
    d.claims.append((line, {"allocation": "link", "asset_ids": [d.asset_id], "numbers": [d.asset["number"]]}))
    d.changes["sources"] = [*(d.asset.get("sources") or []), _source_ref(line, amount, linked=True)]
    d.generation = line["generation"]


async def _release_source(d: Draft, value, fa: dict) -> None:
    source = str((value or {}).get("source") or "") if isinstance(value, dict) else ""
    entry = next((s for s in d.asset.get("sources") or [] if s.get("source") == source), None)
    if entry is not None and not entry.get("linked"):
        raise setup.unprocessable("This asset was registered from this line. Use Undo on the asset to release it.")
    entry, claim = await _claimed_here(d, source)
    carrying, held, key = _carrying(d, claim)
    if _dec(d.asset.get("cost")) - held < 0:
        raise setup.unprocessable("Releasing this line takes the cost below zero.")
    d.move("release_source", min(m["effective_date"] for m in carrying), {"cost": -held}, _as_list({key: held}, -1))
    d.releases.append(source)
    d.changes["sources"] = [s for s in d.asset.get("sources") or [] if s is not entry]


_ERRORS = {"cost": _cost, "available_date": _available_date, "class_id": _class_id,
           "opening_values": _opening_values, "maintenance": _maintenance, "replace_source": _replace_source,
           "link_source": _link_source, "release_source": _release_source}


# ── Depreciation the correction changes ──────────────────────────────────────


def _depreciation(d: Draft, treatment: str) -> None:
    """Post what the corrected asset should have been charged through the last posted run
    less what it was: by financial year against the posting date's year."""
    books = d.books
    if books.last is None:
        return
    hyp, moves = d.hypothetical()
    held = set((d.asset.get("exception") or {}).get("months") or [])
    restated: dict[int, Decimal] = {}
    if hyp.get("status") == "active" and hyp["policy"]["method"] != "none":
        restated = {parse_period(p.period): p.amount
                    for p in runs.asset_schedule(hyp, moves, books.settings, books.last)}
    reported = runs.charged_by_month(d.movements)
    months = sorted(k for k in {*restated, *reported} if k <= books.last and period_label(k) not in held)
    diff = {k: restated.get(k, Decimal(0)) - reported.get(k, Decimal(0)) for k in months}
    diff = {k: v for k, v in diff.items() if v}
    if not diff:
        return
    current = d.current_fy()
    years = {k: _fiscal_year(books.settings, k) for k in diff}
    prior = sorted({y for y in years.values() if y < current})
    for year in prior:
        in_year = [k for k in months if _fiscal_year(books.settings, k) == year]
        d.prior_detail.append({"year": year, "reported": money(_sum(reported.get(k) for k in in_year), books.currency),
                               "restated": money(_sum(restated.get(k) for k in in_year), books.currency)})
    d.prior_years.update(prior)
    groups: dict[bool, dict[int, Decimal]] = {}
    for k, amount in diff.items():
        groups.setdefault(treatment == "retained_earnings" and years[k] < current, {})[k] = amount
    expense = hyp.get("depreciation_account") or d.new_cls["depreciation_account"]
    accumulated = d.new_cls["accumulated_depreciation_account"]
    for to_equity, by_month in sorted(groups.items(), reverse=True):
        total = _sum(by_month.values())
        contributions = []
        if total:
            target = d.retained_earnings() if to_equity else expense
            tags = {"prior_period_adjustment": sorted({years[k] for k in by_month})} if to_equity else None
            n = d.journal(_lines(target, accumulated, total), f"{d.asset['number']} depreciation corrected", tags)
            contributions = [{"journal": n, "line_index": 1 if total > 0 else 0, "amount": str(total)}]
        d.move("depreciation", runs._end(max(by_month)).isoformat(), {_AD: total}, contributions,
               periods={period_label(k): str(v) for k, v in sorted(by_month.items())})


def _future(d: Draft, start: int, months: int = 12) -> list[dict]:
    """The corrected asset's charge for the months after the last posted run."""
    hyp, moves = d.hypothetical()
    if hyp.get("status") != "active" or hyp["policy"]["method"] == "none":
        return []
    periods = runs.asset_schedule(hyp, moves, d.books.settings, start + months - 1)
    return [{"month": p.period, "amount": money(p.amount, d.books.currency)} for p in periods if parse_period(p.period) >= start]


# ── Planning ─────────────────────────────────────────────────────────────────


async def _open(session, company_id, asset_id: str) -> tuple[dict, list[dict]]:
    from celerp.services.company_lock import lock_projections

    row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    if row.state.get("status") != "active":
        raise conflict(f"{row.state['number']} is not in use, so it cannot be corrected.")
    from celerp_fixed_assets import sales

    await sales.refuse_waiting(session, company_id, asset_id, row.state)
    return row.state, await asset_movements(session, company_id, asset_id)


def _first_open(books: runs.Books, start: int | None) -> int:
    """The first month not yet depreciated nor locked, and not before the asset started."""
    k = books.next
    while books.locked(k):
        k += 1
    return max(k, start if start is not None else k)


async def _posting_day(session, company_id, books: runs.Books, original: str, chosen) -> str:
    from celerp.services.posting_dates import correction_day

    today = setup.today_in(books.settings).isoformat()
    if chosen:
        chosen = _date(chosen, "The posting date")
        setup.not_after_today("The posting date", chosen, books.settings)
    return await correction_day(session, company_id, original, chosen=chosen or None) or today


async def plan(session, company_id, asset_id: str, body: dict, fa: dict) -> tuple[dict, dict]:
    """What the correction posts and changes, as the preview shows it, and what applying it needs."""
    changes, classification, default, reason = _classify(body)
    asset, movements = await _open(session, company_id, asset_id)
    cls = await setup.get_class(session, company_id, asset.get("class_id"))
    books = await runs._books(session, company_id)
    live = projections.live(movements)
    start = _first_open(books, runs._first(live))
    # A remeasurement measured the asset as it then stood, so nothing before it is restated.
    remeasured = max((m["effective_date"] for m in live if m.get("operation") in projections.REMEASUREMENTS), default=None)
    shown = {"asset": {"id": asset_id, "number": asset["number"], "name": asset.get("name")},
             "classification": classification, "default_classification": default, "override_reason": reason}
    treatment = body.get("prior_years") or "retained_earnings"
    if treatment not in TREATMENTS:
        raise setup.unprocessable("Earlier years go to retained earnings or to current profit or loss.")
    immaterial = str(body.get("immaterial_reason") or "").strip() or None
    if treatment == "profit_and_loss" and not immaterial:
        raise setup.unprocessable("A reason is required to post an earlier year's effect to current profit or loss.")
    policy_changes = {k: v for k, v in changes.items() if k in ESTIMATE_FIELDS}
    if policy_changes:
        policy = {**asset["policy"], **policy_changes}
        setup.check_policy(policy, books.settings, asset["policy"])
        if policy == asset["policy"]:
            raise setup.unprocessable("The depreciation settings are already these.")
    if classification == "estimate":
        d = Draft(session, company_id, asset_id, asset, cls, movements, books, None)
        effective = start
        if body.get("effective_date"):
            effective = month_index(date.fromisoformat(_date(body["effective_date"], "The effective date")))
            if effective < start:
                raise setup.unprocessable(f"A change in estimate takes effect from {period_label(start)} or later, "
                                          "the first month not yet depreciated.")
        day = _first_day(effective)
        if remeasured and day <= remeasured:
            raise setup.unprocessable(f"{asset['number']} was remeasured on {remeasured}. A change in estimate "
                                      "takes effect after that.")
        d.move("estimate", day, {}, posting=day, policy=policy, previous=asset["policy"])
        d.changes["policy"] = policy
        shown.update(effective_date=day, posting_date=None)
        treatment, immaterial = None, None
    else:
        if remeasured:
            raise setup.unprocessable(f"{asset['number']} was remeasured on {remeasured}, so what came before it "
                                      "is not restated. Undo the remeasurement first while it is the latest change.")
        original = min(m["posting_date"] for m in live)
        d = Draft(session, company_id, asset_id, asset, cls, movements, books,
                  await _posting_day(session, company_id, books, original, body.get("posting_date")))
        if policy_changes:
            d.without_estimates()
            d.changes["policy"] = policy
        else:
            field, value = next(iter(changes.items()))
            await _ERRORS[field](d, value, fa)
        _depreciation(d, treatment)
        if not d.prior_years:
            treatment, immaterial = None, None
        shown.update(effective_date=None, posting_date=d.posting)
    shown.update(
        journals=[{**_journal_view(j["lines"], books.currency), "posting_date": d.posting, "memo": j["memo"], "tags": j["tags"]}
                  for j in d.journals],
        movements=[{k: m[k] for k in ("operation", "effective_date", "posting_date", "amounts")} for m in d.moves],
        prior_years=d.prior_detail, treatment=treatment, immaterial_reason=immaterial,
        changes={k: v for k, v in d.changes.items() if k not in ("sources", "class_history")},
        future=_future(d, start), **d.shown)
    context = {"draft": d, "changes": changes, "classification": classification, "default": default,
               "reason": reason, "treatment": treatment, "immaterial": immaterial, "shown": shown,
               "generation": d.generation,
               "basis": [asset.get("version"), asset.get("movement_count"),
                         await runs._ledger_basis(session, company_id, books)]}
    return shown, context


# ── Applying ─────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _apply(session, company_id, user_id, context: dict, op_key: str, record_id: str | None = None) -> dict:
    """Post the planned journals and movements all or none, and record the correction."""
    d: Draft = context["draft"]
    record_id = record_id or f"fa:corr:{uuid.uuid4()}"
    tag, asset_id = record_id.removeprefix("fa:corr:"), d.asset_id
    href = f"/fixed-assets/assets/{asset_id}"
    async with session.begin_nested():
        journals = []
        for n, j in enumerate(d.journals, 1):
            journals.append(await _post(
                session, company_id, user_id, key=f"asset:{asset_id.removeprefix('fa:asset:')}:corr:{tag}:{n}",
                operation="correction", day=d.posting, memo=j["memo"], lines=j["lines"], source_id=asset_id,
                href=href, label=d.asset["number"], op_key=op_key, tags=j["tags"]))
        movements = []
        for m in d.moves:
            contributions = [{"journal_id": journals[c["journal"]], "line_index": c["line_index"],
                              "amount": c["amount"]} if "journal" in c else c for c in m["contributions"]]
            extra = {k: v for k, v in m.items() if k not in ("operation", "effective_date", "posting_date",
                                                              "amounts", "contributions")}
            movements.append(await _move(session, company_id, user_id, asset_id, kind="corrective",
                                         operation=m["operation"], effective=m["effective_date"],
                                         posting=m["posting_date"], amounts=m["amounts"], op_key=op_key,
                                         contributions=contributions, corrects=record_id, **extra))
        released = []
        for source in d.releases:
            claim = sources.claim_id(source)
            state = await _state(session, company_id, claim)
            generation = int(state.get("generation") or 0)
            await _emit(session, company_id, user_id, claim, "fa_src", "fa.src.released",
                        {"generation": generation, "op_key": op_key}, key=f"{claim}:release:{generation}")
            released.append({"source": source, "state": state})
        for line, extra in d.claims:
            await _claim(session, company_id, user_id, line, op_key=op_key, **extra)
        before = {k: d.asset.get(k) for k in d.changes}
        if d.changes:
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated", {"changes": d.changes})
        after = await _state(session, company_id, asset_id)
        await _emit(session, company_id, user_id, record_id, "fa_correction", "fa.correction.applied", {
            "asset_id": asset_id, "number": d.asset["number"], "changes": context["changes"],
            "classification": context["classification"], "default_classification": context["default"],
            "override_reason": context["reason"], "effective_date": context["shown"]["effective_date"],
            "posting_date": d.posting, "prior_years": sorted(d.prior_years), "prior_detail": d.prior_detail,
            "treatment": context["treatment"], "immaterial_reason": context["immaterial"], "journals": journals,
            "movements": movements, "claimed": [line["source"] for line, _ in d.claims], "released": released,
            "before": before, "after": {k: after.get(k) for k in d.changes},
            "movement_count": int(after.get("movement_count") or 0), "applied_by": str(user_id),
            "applied_at": _now(), "op_key": op_key})
    return {"correction": {"id": record_id, "classification": context["classification"]}, "journals": journals}


async def correct(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Preview or apply a correction to one asset."""

    async def planned(fa):
        return await plan(session, company_id, asset_id, body, fa)

    async def apply(context, op_key):
        from celerp_fixed_assets.operations import _effects

        return await _apply(session, company_id, user_id, context, op_key), _effects()

    return await _run(session, company_id, user_id, body, op_type="correction", keys=("manage_accounting",),
                      plan=planned, apply=apply)


async def apply_to_assets(session, company_id, user_id, class_id: str, body: dict) -> dict:
    """Apply a class's depreciation settings to the assets that still follow older ones,
    as a change in estimate for each."""
    from celerp_fixed_assets.operations import _effects

    async def planned(fa):
        cls = await setup.get_class(session, company_id, class_id)
        policy = setup.policy_of(cls, fa)
        targets = sorted(((eid, s) for eid, s in await _entities(session, company_id, "fa_asset")
                          if s.get("class_id") == cls["id"] and s.get("status") == "active"
                          and s.get("policy") != policy), key=lambda r: r[1]["number"])
        if not targets:
            raise setup.unprocessable("Every asset in this class already follows its depreciation settings.")
        assets, contexts = [], []
        for asset_id, asset in targets:
            changes = {k: policy[k] for k in ESTIMATE_FIELDS if asset["policy"].get(k) != policy[k]}
            shown, context = await plan(session, company_id, asset_id, {
                "changes": changes, "effective_date": body.get("effective_date")}, fa)
            assets.append({"id": asset_id, "number": asset["number"], "name": asset.get("name"),
                           "effective_date": shown["effective_date"], "changes": changes})
            contexts.append(context)
        return {"assets": assets, "journals": []}, {"contexts": contexts,
                                                     "basis": [c["basis"] for c in contexts]}

    async def apply(context, op_key):
        done = [await _apply(session, company_id, user_id, c, op_key) for c in context["contexts"]]
        return {"corrections": [r["correction"] for r in done], "journals": []}, _effects()

    return await _run(session, company_id, user_id, body, op_type="apply_class", keys=("manage_accounting",),
                      plan=planned, apply=apply)


# ── Requests ─────────────────────────────────────────────────────────────────


_REQUEST_FIELDS = ("changes", "classification", "override_reason", "effective_date", "prior_years",
                   "immaterial_reason")


async def record(session, company_id, correction_id: str) -> dict:
    state = await _state(session, company_id, correction_id)
    if state is None or not correction_id.startswith("fa:corr:"):
        raise HTTPException(status_code=404, detail="No such correction.")
    return state


async def _tell_waiting(session, company_id) -> None:
    waiting = [s for _, s in await _entities(session, company_id, "fa_correction") if s.get("state") == "requested"]
    if not waiting:
        await notices.clear(session, company_id, notices.CORRECTIONS)
        return
    await notices.tell(session, company_id, notices.CORRECTIONS,
                       f"{len(waiting)} asset corrections wait for a user with Manage accounting to apply them.",
                       action_url=notices.CORRECTIONS_URL, count=len(waiting))


async def request(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Save a correction for a user who may post journals to apply. Posts nothing. The
    request is named by its form's operation key, so sending it again (a lost response, a
    double click) returns the same request as it stands now instead of asking twice."""
    from celerp.services.permissions import locked_authority, missing_permission, role_has_permission

    op_key = _op_key(body)
    role, settings = await locked_authority(session, company_id, user_id, ("manage_fixed_assets",))
    intent_hash = _intent_hash("correction_request", {**body, "asset_id": asset_id})
    correction_id = f"fa:corr:{uuid.uuid5(uuid.NAMESPACE_URL, f'{company_id}:{op_key}')}"
    saved = await _state(session, company_id, correction_id)
    if saved is not None:
        _same_intent(saved, intent_hash)
        return {"id": correction_id, **saved}
    fa = await setup.require_started(session, company_id)
    if overrides(body) and not role_has_permission(settings, role, "manage_accounting"):
        raise missing_permission("manage_accounting")
    stored = {k: body.get(k) for k in _REQUEST_FIELDS}
    shown, _ = await plan(session, company_id, asset_id, stored, fa)
    data = {**stored, "asset_id": asset_id, "intent_hash": intent_hash, "number": shown["asset"]["number"],
            "classification": shown["classification"], "default_classification": shown["default_classification"],
            "reason": str(body.get("reason") or "").strip() or None, "requested_by": str(user_id),
            "requested_at": _now(), "need": NEED}
    await _emit(session, company_id, user_id, correction_id, "fa_correction", "fa.correction.requested", data)
    await _tell_waiting(session, company_id)
    return {"id": correction_id, **data, "state": "requested"}


async def apply_request(session, company_id, user_id, correction_id: str, body: dict) -> dict:
    """Preview or apply a requested correction, on the posting date chosen now."""
    from celerp_fixed_assets.operations import _effects

    async def planned(fa):
        saved = await record(session, company_id, correction_id)
        if saved.get("state") != "requested":
            raise conflict(f"This correction was {saved.get('state')}, so there is nothing to apply.")
        stored = {k: saved.get(k) for k in _REQUEST_FIELDS}
        return await plan(session, company_id, saved["asset_id"], {**stored, "posting_date": body.get("posting_date")},
                          fa)

    async def apply(context, op_key):
        result = await _apply(session, company_id, user_id, context, op_key, record_id=correction_id)
        await _tell_waiting(session, company_id)
        return result, _effects()

    return await _run(session, company_id, user_id, body, op_type="correction", keys=("manage_accounting",),
                      plan=planned, apply=apply)


async def withdraw(session, company_id, user_id, correction_id: str) -> dict:
    """The requester takes back a correction nobody has applied."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_fixed_assets",))
    await lock_projections(session, company_id, [correction_id])
    saved = await record(session, company_id, correction_id)
    if saved.get("requested_by") != str(user_id):
        raise HTTPException(status_code=403, detail="Only the user who requested this correction can withdraw it.")
    if saved.get("state") != "requested":
        raise conflict(f"This correction was {saved.get('state')}, so it cannot be withdrawn.")
    await _emit(session, company_id, user_id, correction_id, "fa_correction", "fa.correction.withdrawn", {})
    await _tell_waiting(session, company_id)
    return {"id": correction_id, **saved, "state": "withdrawn"}


async def records(session, company_id, asset_id: str | None = None) -> list[dict]:
    rows = [{"id": eid, **s} for eid, s in await _entities(session, company_id, "fa_correction")
            if asset_id is None or s.get("asset_id") == asset_id]
    return sorted(rows, key=lambda r: r.get("applied_at") or r.get("requested_at") or "", reverse=True)


# ── Undo ─────────────────────────────────────────────────────────────────────


async def undo(session, company_id, user_id, correction_id: str, body: dict) -> dict:
    """Undo a correction while nothing has happened to the asset since and its day is open."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.module_journals import reverse_journal
    from celerp_fixed_assets.operations import _effects

    async def planned(fa):
        saved = await record(session, company_id, correction_id)
        if saved.get("state") != "applied":
            raise conflict("Only an applied correction can be undone.")
        asset_id = saved["asset_id"]
        asset = (await lock_projections(session, company_id, [asset_id]))[asset_id].state
        if (int(asset.get("movement_count") or 0) != saved["movement_count"]
                or any(asset.get(k) != v for k, v in (saved.get("after") or {}).items())):
            raise conflict(f"{asset['number']} has changed since this correction. Correct it again instead.")
        books = await runs._books(session, company_id)
        day = saved.get("posting_date") or saved.get("effective_date")
        if books.lock is not None and date.fromisoformat(day) <= books.lock:
            raise conflict(f"{day} is locked, so this correction can no longer be undone. Correct the asset again.")
        journals = [await _state(session, company_id, j) for j in saved.get("journals") or []]
        shown = {"correction": correction_id, "number": asset["number"],
                 "journals": [_reversed_lines(je, books.currency) for je in journals if je and je.get("status") == "posted"]}
        return shown, {"record": saved, "basis": [asset.get("version"), asset.get("movement_count"),
                                                   await runs._ledger_basis(session, company_id, books)]}

    async def apply(context, op_key):
        record = context["record"]
        asset_id = record["asset_id"]
        async with session.begin_nested():
            for je_id in record.get("journals") or []:
                await reverse_journal(session, company_id=company_id, user_id=user_id, module=MODULE,
                                      key=je_id.removeprefix(f"je:mod:{MODULE}:"), operation="undo",
                                      reason="Correction undone")
            for movement_id in record.get("movements") or []:
                mv = await _state(session, company_id, movement_id)
                await _move(session, company_id, user_id, asset_id, kind="reversal", operation="undo",
                            effective=mv["effective_date"], posting=mv["posting_date"],
                            amounts={k: -_dec(v) for k, v in (mv.get("amounts") or {}).items() if k in BUCKETS},
                            op_key=op_key, contributions=[{**c, "amount": str(-_dec(c.get("amount")))}
                                                          for c in mv.get("contributions") or []],
                            reverses=movement_id)
            for source in record.get("claimed") or []:
                claim = sources.claim_id(source)
                generation = int((await _state(session, company_id, claim) or {}).get("generation") or 0)
                await _emit(session, company_id, user_id, claim, "fa_src", "fa.src.released",
                            {"generation": generation, "op_key": op_key}, key=f"{claim}:release:{generation}")
            for item in record.get("released") or []:
                claim = sources.claim_id(item["source"])
                generation = int((await _state(session, company_id, claim) or {}).get("generation") or 0)
                kept = {k: v for k, v in item["state"].items() if k not in ("status", "released_by_op")}
                await _emit(session, company_id, user_id, claim, "fa_src", "fa.src.claimed",
                            {**kept, "generation": generation}, key=f"{claim}:claim:{generation}")
            if record.get("before"):
                await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated",
                            {"changes": record["before"]})
            await _emit(session, company_id, user_id, correction_id, "fa_correction", "fa.correction.undone",
                        {"op_key": op_key})
        return {"undone": correction_id, "journals": []}, _effects()

    return await _run(session, company_id, user_id, body, op_type="correction_undo", keys=("manage_accounting",),
                      plan=planned, apply=apply)


# ── What the form offers ─────────────────────────────────────────────────────


async def fields(session, company_id, asset_id: str) -> dict:
    """The fields Correct offers for an asset, each with its default classification, and
    the lines that may be linked to it."""
    asset = await _state(session, company_id, asset_id)
    if asset is None or not asset_id.startswith("fa:asset:"):
        raise HTTPException(status_code=404, detail="No such asset.")
    movements = projections.live(await asset_movements(session, company_id, asset_id))
    opening = any(m.get("operation") == "opening" for m in movements)
    skip = {"opening_values"} if not opening else {"cost", "available_date", "maintenance"}
    if asset.get("sources"):
        skip.add("cost")
    out = [{"field": f, "classification": "estimate"} for f in ESTIMATE_FIELDS]
    out += [{"field": f, "classification": "error"} for f in ERROR_FIELDS if f not in skip]
    account = None
    if asset.get("class_id"):
        account = (await setup.get_class(session, company_id, asset["class_id"]))["asset_account"]
    linkable = [{k: line[k] for k in ("source", "kind", "description", "amount", "date", "document_ref", "href")}
                for line in await sources.lines(session, company_id)
                if not line["claim"] and not line["before_cutover"] and line["kind"] in ("acquisition", "reduction")
                and line["account"] == account]
    held = asset.get("sources") or []
    actions = [{"action": "link_source"}]
    if any(not s.get("linked") for s in held):
        actions.append({"action": "replace_source"})
    if any(s.get("linked") for s in held):
        actions.append({"action": "release_source"})
    return {"fields": out, "sources": linkable, "source_actions": actions}


# ── Prior-period adjustments report ──────────────────────────────────────────


async def prior_period_report(session, company_id, year: int) -> dict:
    """Corrections restating ``year``: the year's depreciation as reported and as restated,
    and the immaterial ones posted to current profit or loss instead, with their reason."""
    applied = sorted((r for r in await records(session, company_id) if r.get("state") == "applied"
                      and year in (r.get("prior_years") or [])), key=lambda r: r.get("applied_at") or "")
    items = [r for r in applied if r.get("treatment") == "retained_earnings"]
    per_asset: dict[str, list[dict]] = {}
    for r in items:
        per_asset.setdefault(r["asset_id"], []).extend(y for y in r.get("prior_detail") or [] if y["year"] == year)
    reported = _sum(rows[0]["reported"] for rows in per_asset.values() if rows)
    restated = _sum(rows[-1]["restated"] for rows in per_asset.values() if rows)
    currency = await setup.currency(session, company_id)
    keep = ("id", "asset_id", "number", "prior_years", "posting_date", "journals", "applied_at")
    return {"year": year,
            "items": [{**{k: r.get(k) for k in keep}, "years": r.get("prior_years")} for r in items],
            "years": [{"year": year, "reported": money(reported, currency), "restated": money(restated, currency)}] if items else [],
            "immaterial": [{**{k: r.get(k) for k in keep}, "years": r.get("prior_years"),
                            "reason": r.get("immaterial_reason")}
                           for r in applied if r.get("treatment") == "profit_and_loss"]}
