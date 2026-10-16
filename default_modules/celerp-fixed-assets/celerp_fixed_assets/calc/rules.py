# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The depreciation methods, timing conventions, tax regimes and statutory reports the
fixed assets module calculates with, and the shapes each takes.

The methods and conventions below are the module's own. Any module may add rules of all
four kinds through the ``fixed_asset_rules`` slot: each entry is ``{"kind", "key",
"handler": "module.path:Name"}``, where calling ``Name()`` returns an object with the
attributes its kind lists in ``KINDS``. An entry that lacks one, names an unknown kind,
reuses a core key or repeats a key already registered is skipped and kept in
``problems()``, so the rest of its module still loads. A rule applies to a company only
while the company uses the module that registered it (``available``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from celerp_fixed_assets.calc import conventions, methods
from celerp_fixed_assets.calc.methods import MethodInputs

_log = logging.getLogger(__name__)


class DepreciationMethod(Protocol):
    key: str
    label_key: str
    params_schema: dict
    uses_readings: bool

    def amounts(self, inputs: MethodInputs) -> list[Decimal]: ...


class TimingConvention(Protocol):
    key: str
    label_key: str

    def start_weights(self, day: date, fiscal_year_start: int) -> list[Decimal]: ...

    def disposal(self, normal: Decimal, day: date, fy_consumed: Decimal, remaining: Decimal) -> Decimal: ...


class TaxRegime(Protocol):
    """A country's tax depreciation. ``basis`` is "asset" (each asset its own balance,
    ``asset_year``) or "pool" (assets pooled by ``pool_key``, ``pool_year``).
    ``deferred_tax`` is None when the regime reports none. ``elections_schema`` maps each
    election's name to "bool", "decimal" or "text"; its label is the key
    ``<label_key>.<name>`` in the module's locales."""
    key: str
    country: str
    label_key: str
    basis: str
    categories: list
    elections_schema: dict

    def tax_year(self, tax_year_end: str, year: int) -> tuple[date, date]: ...

    def related_party_transfer(self, inputs): ...


class StatutoryReport(Protocol):
    """A country form drawn from one regime's book: ``render(books, year)`` returns
    ``{"columns": [label keys], "rows": [[values]]}``."""
    key: str
    country: str
    label_key: str
    regime: str

    def render(self, books: list[dict], year: int) -> dict: ...


CORE = {
    "method": (methods.StraightLine(), methods.DecliningBalance(), methods.SumOfYearsDigits(),
               methods.UnitsOfUse(), methods.NoDepreciation()),
    "convention": (conventions.FullMonth(), conventions.MidMonth(), conventions.Daily(), conventions.HalfYear()),
    "tax_regime": (),
    "statutory_report": (),
}

# Per kind: the text attributes, the other attributes, and the callables an entry needs.
KINDS = {
    "method": (("key", "label_key"), ("params_schema", "uses_readings"), ("amounts",)),
    "convention": (("key", "label_key"), (), ("start_weights", "disposal")),
    "tax_regime": (("key", "country", "label_key", "basis"), ("categories", "elections_schema", "deferred_tax"),
                   ("tax_year", "related_party_transfer")),
    "statutory_report": (("key", "country", "label_key", "regime"), (), ("render",)),
}
_BASIS_CALLABLES = {"asset": ("asset_year",), "pool": ("pool_key", "pool_year")}


class RuleUnavailable(LookupError):
    """A rule no loaded module provides."""


@dataclass(frozen=True)
class Registry:
    rules: dict      # kind -> {key: rule}
    modules: dict    # (kind, key) -> (module name, display name), absent for core rules
    problems: tuple  # {"module", "kind", "key", "reason"} per skipped entry


def _problem(rule, kind: str) -> str | None:
    """Why ``rule`` cannot be a ``kind``, or None."""
    texts, values, calls = KINDS[kind]
    for name in texts:
        if not isinstance(getattr(rule, name, None), str) or not getattr(rule, name):
            return f"It has no {name}."
    for name in values:
        if not hasattr(rule, name):
            return f"It has no {name}."
    if kind == "tax_regime":
        if rule.basis not in _BASIS_CALLABLES:
            return "Its basis is neither asset nor pool."
        calls = (*calls, *_BASIS_CALLABLES[rule.basis])
        if rule.deferred_tax is not None and not callable(rule.deferred_tax):
            return "Its deferred_tax is neither None nor callable."
        if not isinstance(rule.categories, (list, tuple)) or not all(
                isinstance(getattr(c, "key", None), str) and isinstance(getattr(c, "label_key", None), str)
                for c in rule.categories):
            return "Its categories are not a list of tax categories."
    for name in calls:
        if not callable(getattr(rule, name, None)):
            return f"It has no {name}."
    if kind == "method" and not isinstance(rule.params_schema, dict):
        return "Its params_schema is not a dict."
    if kind == "tax_regime" and not isinstance(rule.elections_schema, dict):
        return "Its elections_schema is not a dict."
    return None


def _build(entries: tuple) -> Registry:
    from celerp.modules.slots import resolve_handler

    rules = {kind: {r.key: r for r in core} for kind, core in CORE.items()}
    modules, problems = {}, []
    for entry in entries:
        module, kind, key = entry.get("_module"), entry.get("kind"), entry.get("key")

        def skip(reason: str) -> None:
            problems.append({"module": module, "kind": kind, "key": key, "reason": reason})
            _log.warning("Fixed asset rule %s %r from %s skipped: %s", kind, key, module, reason)

        if kind not in KINDS:
            skip(f"{kind!r} is not a kind of fixed asset rule.")
            continue
        if key in {r.key for r in CORE[kind]}:
            skip(f"{key} is a core {kind.replace('_', ' ')} and cannot be replaced.")
            continue
        if key in rules[kind]:
            skip(f"{key} is already registered by {modules[(kind, key)][0]}.")
            continue
        try:
            rule = resolve_handler(entry["handler"])()
        except Exception as exc:  # a module's own code: skipped and named, never fatal
            skip(f"It could not be created ({type(exc).__name__}).")
            continue
        reason = _problem(rule, kind)
        if reason is None and rule.key != key:
            reason = f"It calls itself {rule.key}, not {key}."
        if reason:
            skip(reason)
            continue
        rules[kind][key] = rule
        modules[(kind, key)] = (module, _display_name(module))
    return Registry(rules, modules, tuple(problems))


def _display_name(module: str) -> str:
    from celerp.modules.loader import loaded_modules

    manifest = next((m for m in loaded_modules() if m.get("name") == module), {})
    return manifest.get("display_name") or module


_memo: tuple = ((), None)


def registry() -> Registry:
    """Every rule, built once per set of slot entries."""
    global _memo
    from celerp.modules.slots import get

    entries = tuple(get("fixed_asset_rules"))
    if _memo[1] is None or _memo[0] != entries:
        _memo = (entries, _build(entries))
    return _memo[1]


def _rule(kind: str, key: str):
    rule = registry().rules[kind].get(key)
    if rule is None:
        raise RuleUnavailable(f"No module provides the {kind.replace('_', ' ')} {key}.")
    return rule


def method(key: str) -> DepreciationMethod:
    return _rule("method", key)


def convention(key: str) -> TimingConvention:
    return _rule("convention", key)


def regime(key: str) -> TaxRegime:
    return _rule("tax_regime", key)


def statutory_report(key: str) -> StatutoryReport:
    return _rule("statutory_report", key)


def module_of(kind: str, key: str) -> tuple[str, str] | None:
    """The (module, display name) that registered a rule; None for the module's own."""
    return registry().modules.get((kind, key))


def is_available(kind: str, key: str, settings: dict | None) -> bool:
    """Whether the rule is registered and the company uses the module that registered it."""
    from celerp.modules.registry import uses_module

    if key not in registry().rules[kind]:
        return False
    owner = module_of(kind, key)
    return owner is None or uses_module(settings or {}, owner[0])


def available(kind: str, settings: dict | None) -> dict:
    """The rules of ``kind`` a company may use, keyed by rule key, core rules first."""
    return {key: rule for key, rule in registry().rules[kind].items() if is_available(kind, key, settings)}


def unavailable_reason(kind: str, key: str) -> str:
    """Why a company cannot use a rule: its module is turned off, or nothing provides it."""
    from ui.i18n import t

    owner = module_of(kind, key)
    if owner is None:
        return f"{key} is not provided by any installed module."
    label = t(registry().rules[kind][key].label_key)
    return f"{label} needs the {owner[1]} module, which is turned off."


def problems() -> tuple:
    return registry().problems
