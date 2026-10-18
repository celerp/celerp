# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The reconciliation of the register with the ledger.

Every journal line on a class account (each class's asset and accumulated depreciation
account, the revaluation surplus and the asset sale clearing account) is classified once,
by the first rule that applies:

1. a line of this module's journals, or a sale invoice's line on the clearing account;
2. a registered source line;
3. a voided source line: the reversal of a voided journal's line, or a registered line
   whose journal was voided in place (the register still holds it, the ledger no longer);
4. a manual line marked as not an acquisition or kept as booked, noted with its reason;
5. a source line not yet registered;
6. anything else.

At a date on or after the cutover, the ledger balance of an account is the sum of its
lines to that date. The registered balance is the opening register (the opening values
of every opening asset, whenever they were last corrected, and every movement posted on
or before the cutover) plus the lines of rules 1 and 2 after the cutover. Their difference is the opening difference (the ledger at the
cutover less the opening register) plus every later line of rules 3 to 6, each listed.

The registered balance is checked against the assets themselves: their movements to the
date, each asset's balance on the accounts of the class it was in at that date. A
mismatch is a defect in this module and is reported as an error, never hidden.

Each difference names what clears it: the opening balances, registering the line, replacing
the voided source on its asset, or correcting the journal; every one of them, and
acknowledging, needs manage_accounting. A difference dated in a locked period is fixed
from the first open date.

A difference is acknowledged as noted, or accepted with the reason and its accounting
effect. An acknowledgement holds for the amount it was given at; when the amount changes
the difference is open again.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from celerp.services.business_time import company_today

from celerp_fixed_assets import fixaccount, runs, setup, sources
from celerp_fixed_assets.operations import MODULE, _dec, conflict
from celerp_fixed_assets.projections import fold_movements, money
from celerp_fixed_assets.store import _emit, _entities

RECON_STATES = ("open", "noted", "accepted")
REGISTERED_RULES = (1, 2)
# The claims that bring a line into the register; the others note it with a reason.
NOTED_ALLOCATIONS = ("not_acquisition", "keep")
STATUSES = ("Reconciled", "Differences acknowledged", "Differences open")
_MODULE_JOURNAL = f"je:mod:{MODULE}:"
_SIGN = {"acquisition": 1, "depreciation": -1, "reduction": -1}
_CAUSES = {1: "Fixed asset journal", 2: "Registered", 3: "Source voided", 4: "Noted", 5: "Not yet registered",
           6: "Unclassified"}


def recon_id(key: str) -> str:
    return f"fa:recon:{key}"


async def _accounts(session, company_id) -> tuple[dict, str | None, set[str], set[str]]:
    """Each class; today's surplus account; and every account that has served as the
    surplus and as the sale clearing account, where the balances posted then still stand."""
    from celerp.services.account_roles import role_map, scope_codes

    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    settings = await setup.company_settings(session, company_id)
    roles = role_map(settings)

    def served(role):
        return scope_codes(settings, role) | ({roles[role]} if roles.get(role) else set())

    return classes, roles.get("revaluation_surplus"), served("revaluation_surplus"), served("asset_disposal_clearing")


def _class_accounts(classes: dict, surpluses: set[str], clearings: set[str]) -> set[str]:
    """Every account a class posts its balances to, or did before Fix posting account."""
    out = {c[k] for c in classes.values() for k in ("asset_account", "accumulated_depreciation_account") if c.get(k)}
    out |= {a for c in classes.values() for a in fixaccount.past_accounts(c)}
    return out | surpluses | clearings


def _signed(entry: dict) -> Decimal:
    return _dec(entry.get("debit")) - _dec(entry.get("credit"))


async def classify(session, company_id, at: str) -> list[dict]:
    """Every class-account line posted on or before ``at``, each with the rule that
    classifies it, plus a line for each registered source whose journal was voided in
    place. Each carries a key unique across the company's books."""
    from celerp.services.ledger_rows import REVERSAL_SUFFIX, journal_rows

    classes, _, surpluses, clearings = await _accounts(session, company_id)
    accounts = _class_accounts(classes, surpluses, clearings)
    asset_ids = {eid for eid, _ in await _entities(session, company_id, "fa_asset")}
    claims = [s for _, s in await _entities(session, company_id, "fa_src") if s.get("status") == "claimed"]
    by_journal: dict[str, dict[str, dict]] = {}
    for claim in claims:
        by_journal.setdefault(claim["journal_id"], {})[claim["source"]] = {**claim, "claim": claim}
    for line in await sources.lines(session, company_id):
        held = by_journal.setdefault(line["journal_id"], {})
        held[line["source"]] = {**line, "claim": held.get(line["source"], {}).get("claim")}
    out: list[dict] = []
    for je_id, je, day in await journal_rows(session, company_id, include_void=True):
        if not day or day > at:
            continue
        if je.get("status") == "void":
            # Voided in place: the journal posts nothing, while a registered line of it
            # still stands in the register until the source is replaced or corrected.
            for source, unit in sorted((by_journal.get(je_id) or {}).items()):
                claim = unit.get("claim")
                if claim and claim["allocation"] not in NOTED_ALLOCATIONS and claim.get("account") in accounts:
                    amount = _SIGN[claim["kind"]] * _dec(claim["amount"])
                    out.append(_unit(f"void:{source}", 3, claim["account"], amount, claim.get("date") or day, je_id,
                                     claim["description"], source=source, registered=amount, synthetic=True))
            continue
        reversal = je_id.endswith(REVERSAL_SUFFIX)
        original = je_id.removesuffix(REVERSAL_SUFFIX)
        sign = -1 if reversal else 1
        entries = [(i, e) for i, e in enumerate(je.get("entries") or []) if e.get("account") in accounts]
        if not entries:
            continue
        memo = je.get("memo") or ""
        if original.startswith(_MODULE_JOURNAL):
            out += [_unit(f"line:{je_id}:{i}", 1, e["account"], _signed(e), day, je_id, memo) for i, e in entries]
            continue
        rest: dict[str, Decimal] = {}
        for i, e in entries:
            if e["account"] in clearings and e.get("asset_id") in asset_ids:
                out.append(_unit(f"line:{je_id}:{i}", 1, e["account"], _signed(e), day, je_id, memo))
            else:
                rest[e["account"]] = rest.get(e["account"], Decimal(0)) + _signed(e)
        for source, unit in sorted((by_journal.get(original) or {}).items()):
            if unit.get("account") not in rest:
                continue
            claim = unit.get("claim")
            amount = sign * _SIGN[unit["kind"]] * _dec(unit["amount"])
            if reversal:
                rule = 3
            elif claim is None:
                rule = 5
            else:
                rule = 4 if claim["allocation"] in NOTED_ALLOCATIONS else 2
            out.append(_unit(f"line:{je_id}:{source}", rule, unit["account"], amount, day, je_id,
                             unit.get("description") or memo, source=source,
                             reason=claim.get("reason") if rule == 4 else None, claimed=claim is not None))
            rest[unit["account"]] -= amount
        out += [_unit(f"rest:{je_id}:{account}", 6, account, amount, day, je_id, memo)
                for account, amount in sorted(rest.items()) if amount]
    return out


def _unit(key: str, rule: int, account: str, amount: Decimal, day: str, journal_id: str, description: str,
          **extra) -> dict:
    return {"key": key, "rule": rule, "account": account, "amount": amount, "date": day, "journal_id": journal_id,
            "description": description, **extra}


def _class_at(asset: dict, day: str) -> str | None:
    """The class the asset was in on ``day``."""
    for held in asset.get("class_history") or []:
        if day < held["until"]:
            return held["class_id"]
    return asset.get("class_id")


def _balances(cls: dict, figures: dict, surplus: str | None) -> dict[str, Decimal]:
    """What the asset's figures stand at on each account, signed as the ledger holds them."""
    out: dict[str, Decimal] = {}

    def add(account, amount):
        if account and amount:
            out[account] = out.get(account, Decimal(0)) + amount

    add(cls.get("asset_account"), figures["cost"])
    add(cls.get("accumulated_depreciation_account"),
        -(figures["accumulated_depreciation"] + figures["accumulated_impairment"]))
    add(surplus, -figures["revaluation_surplus"])
    return out


async def _register(session, company_id, classes: dict, surplus: str | None, cut: str,
                    at: str) -> tuple[dict, dict]:
    """The opening register (opening values, and every movement posted on or before the
    cutover, such as an asset contributed before it) mapped to the accounts at the cutover,
    and every asset's balances at ``at`` mapped to the accounts its class posted to then."""
    assets = dict(await _entities(session, company_id, "fa_asset"))
    recorded = dict(await _entities(session, company_id, "fa_movement"))

    def in_opening(mv: dict) -> bool:
        # An opening movement, a correction of opening values, or the reversal of either.
        reversed_mv = recorded.get(mv.get("reverses") or "")
        return mv["operation"] in runs.OPENING_OPERATIONS or bool(reversed_mv and in_opening(reversed_mv))

    movements: dict[str, list[dict]] = {}
    for mv in recorded.values():
        movements.setdefault(mv["asset_id"], []).append(mv)
    opening: dict[str, Decimal] = {}
    standing: dict[str, Decimal] = {}
    for asset_id, moved in movements.items():
        asset = assets[asset_id]
        for target, day, chosen in (
                (opening, cut, [m for m in moved if in_opening(m) or m["posting_date"] <= cut]),
                (standing, at, [m for m in moved if in_opening(m) or m["posting_date"] <= at])):
            cls = classes.get(_class_at(asset, day))
            if cls is None or not chosen:
                continue
            figures = {k: Decimal(v) for k, v in fold_movements(chosen).items() if k != "quantity"}
            kept = asset.get("surplus_account") or surplus
            for account, amount in _balances(fixaccount.accounts_at(cls, day), figures, kept).items():
                target[account] = target.get(account, Decimal(0)) + amount
    return opening, standing


async def _acknowledged(session, company_id) -> dict[str, dict]:
    return {s["key"]: s for _, s in await _entities(session, company_id, "fa_recon")}


async def report(session, company_id, at: str | None = None) -> dict:
    """The reconciliation at ``at`` (today when not given), per class account."""
    from celerp.services.money import books_currency
    from celerp.services.posting_readiness import account_names

    fa = await setup.require_managed(session, company_id)
    cut = setup.cutover(fa)
    at = at or (await company_today(session, company_id)).isoformat()
    at = setup.day(at, "The reconciliation date").isoformat()
    if at < cut:
        raise setup.unprocessable(f"The reconciliation starts at the cutover {cut}.")
    classes, surplus, surpluses, clearings = await _accounts(session, company_id)
    accounts = sorted(_class_accounts(classes, surpluses, clearings))
    units = await classify(session, company_id, at)
    opening, standing = await _register(session, company_id, classes, surplus, cut, at)
    marks = await _acknowledged(session, company_id)
    settings = await setup.company_settings(session, company_id)
    lock, currency = settings.get("lock_date"), books_currency(settings)
    owners = {s["source"]: asset_id for asset_id, a in await _entities(session, company_id, "fa_asset")
              for s in a.get("sources") or [] if isinstance(s, dict) and s.get("source")}
    fixing = {"marks": marks, "owners": owners, "lock": date.fromisoformat(lock) if lock else None,
              "currency": currency}
    names = await account_names(session, company_id, accounts)
    labels = await setup.account_labels(session, company_id, accounts)
    out, errors = [], []
    for account in accounts:
        mine = [u for u in units if u["account"] == account]
        ledger = sum((u["amount"] for u in mine if not u.get("synthetic")), Decimal(0))
        before = sum((u["amount"] for u in mine if u["date"] <= cut and not u.get("synthetic")), Decimal(0))
        later = [u for u in mine if u["date"] > cut]
        registered = opening.get(account, Decimal(0)) + sum(
            (u.get("registered", u["amount"]) for u in later if u["rule"] in REGISTERED_RULES or u.get("synthetic")),
            Decimal(0))
        differences = []
        if before != opening.get(account, Decimal(0)):
            differences.append(_difference(f"opening:{account}", "opening", "Opening balance not in the register",
                                           before - opening.get(account, Decimal(0)), cut, [], fixing))
        for u in _unpaired([u for u in later if u["rule"] not in REGISTERED_RULES]):
            amount = -u["amount"] if u.get("synthetic") else u["amount"]
            differences.append(_difference(u["key"], u["rule"], _CAUSES[u["rule"]], amount, u["date"], [u], fixing))
        if account not in clearings and registered != standing.get(account, Decimal(0)):
            # The assets' own balances against the Register column, read from their journals.
            errors.append({"account": account, "name": names.get(account) or account, "label": labels[account],
                           "held": money(standing.get(account, 0), currency), "registered": money(registered, currency)})
        out.append({"account": account, "name": names.get(account) or account, "label": labels[account], "ledger": money(ledger, currency),
                    "registered": money(registered, currency), "status": _status(ledger, registered, differences),
                    "differences": differences})
    statuses = {a["status"] for a in out}
    status = next(s for s in reversed(STATUSES) if s in statuses) if statuses else STATUSES[0]
    return {"at": at, "cutover": cut, "status": status, "accounts": out, "errors": errors,
            "opening": (fa.get("opening") or {}).get("state")}


def _unpaired(units: list[dict]) -> list[dict]:
    """The differences less each voided line and its reversal where both stand and net to
    nothing: a journal entered and voided again leaves no difference."""
    from celerp.services.ledger_rows import REVERSAL_SUFFIX

    def base(u):
        return u["key"].replace(REVERSAL_SUFFIX, "")

    totals: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for u in units:
        totals[base(u)] = totals.get(base(u), Decimal(0)) + u["amount"]
        counts[base(u)] = counts.get(base(u), 0) + 1
    return [u for u in units if not (counts[base(u)] > 1 and not totals[base(u)])]


def _difference(key: str, rule, cause: str, amount: Decimal, day: str, units: list[dict], fixing: dict) -> dict:
    mark = fixing["marks"].get(key) or {}
    if units and units[0]["rule"] == 4:
        state, reason, effect = "noted", units[0].get("reason"), None
    elif mark.get("state") in ("noted", "accepted") and _dec(mark.get("amount")) == amount:
        state, reason, effect = mark["state"], mark.get("reason"), mark.get("effect")
    else:
        state, reason, effect = "open", None, None
    currency = fixing["currency"]
    return {"key": key, "rule": rule, "cause": cause, "amount": money(amount, currency), "date": day, "state": state,
            "reason": reason, "effect": effect, "action": _action(rule),
            "asset_id": fixing["owners"].get(units[0].get("source")) if rule == 3 else None,
            "fix_from": _fix_from(fixing["lock"], day),
            "lines": [{"journal_id": u["journal_id"], "source": u.get("source"), "description": u["description"],
                       "date": u["date"], "amount": money(u["amount"], currency)} for u in units]}


def _action(rule) -> str | None:
    """What clears the difference: the opening balances, registering the line, replacing the
    voided source on its asset, or correcting the journal from its ledger. A noted line needs nothing."""
    return {"opening": "opening", 3: "replace_source", 5: "intake", 6: "ledger"}.get(rule)


def _fix_from(lock: date | None, day: str) -> str | None:
    """The first open date when the difference falls in the locked period, else None."""
    if lock is None or date.fromisoformat(day) > lock:
        return None
    return (lock + timedelta(days=1)).isoformat()


def _status(ledger: Decimal, registered: Decimal, differences: list[dict]) -> str:
    if ledger == registered and not differences:
        return STATUSES[0]
    if differences and all(d["state"] != "open" for d in differences):
        return STATUSES[1]
    return STATUSES[2]


async def find(session, company_id, key: str) -> dict:
    """The difference ``key`` as it stands today."""
    found = next((d for a in (await report(session, company_id))["accounts"] for d in a["differences"]
                  if d["key"] == key), None)
    if found is None:
        raise setup.unprocessable("There is no such difference. Reload the reconciliation.")
    return found


async def acknowledge(session, company_id, user_id, body: dict) -> dict:
    """Note a difference, accept it with its reason and accounting effect, or open it
    again. Changes no balance and no status other than this difference's."""
    from celerp.services.company_lock import lock_company
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await lock_company(session, company_id)
    key, state = str(body.get("key") or ""), body.get("state")
    if state not in RECON_STATES:
        raise setup.unprocessable("Choose open, noted or accepted.")
    reason = str(body.get("reason") or "").strip()
    effect = str(body.get("effect") or "").strip()
    if state != "open" and not reason:
        raise setup.unprocessable("A reason is required to note or accept a difference.")
    if state == "accepted" and not effect:
        raise setup.unprocessable("Describe the accounting effect of accepting this difference.")
    found = await find(session, company_id, key)
    if found["rule"] == 4:
        raise conflict("This line was noted when it was kept or marked as not an acquisition. Undo that to open it.")
    if _dec(body.get("amount")) != _dec(found["amount"]):
        raise conflict(f"The difference is now {found['amount']}. Review it again.")
    data = {"key": key, "state": state, "amount": found["amount"], "reason": reason or None,
            "effect": effect or None, "by": str(user_id), "at": (await company_today(session, company_id)).isoformat()}
    await _emit(session, company_id, user_id, recon_id(key), "fa_recon", "fa.recon.recorded", data)
    return {**found, "state": state, "reason": data["reason"], "effect": data["effect"]}
