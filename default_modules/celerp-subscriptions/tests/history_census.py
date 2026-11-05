# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Census of what an agreement's History can show, read from the code.

``registrations()`` finds every ``register_fields`` call (the History describers a module
names). ``fields()`` takes every field the subscriptions module's writers record on an
agreement (``owned_fields_census.writes()``: each ``write_agreement`` call and the
migration's own move) and names how History renders it:

- described: the registered describer renders the change;
- generic: no describer, History's generic field line renders it (named in GENERIC);
- companion: a set (COMPANION_SETS) or a drop (COMPANION_DROPS) its describer shows nothing
  of; each write site that records one also records a field History renders;
- derived: write_agreement computes it itself from the change (named in DERIVED).

A field no rule names is reported as ``unnamed``, so a new field reaches History only
once it is named here. Run as a script for the census table: ``python history_census.py``.
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owned_fields_census  # noqa: E402

ROOT = owned_fields_census.ROOT
PACKAGE = owned_fields_census.PACKAGE
SOURCE_TOPS = ("ui", "celerp", "default_modules", "premium_modules")

# Fields History renders through its generic line, and why no describer is needed.
GENERIC = {"status": "the generic line names the status by its label on both sides"}
# Sets and drops History shows nothing of, each recorded only beside a change it shows.
COMPANION_SETS = {
    "resume_restarts": "the migration's move marks a paused template with its placed periods",
    "released_bills": "Reopen keeps the released bill as the period opens again",
}
COMPANION_DROPS = {
    "resume_restarts": "resume drops it as the agreement becomes active",
    "billing_failure": "the bill that clears a failure records its period",
    "generated_doc_ids": "the migration's move drops it as it places the bills",
    "next_run_date": "the same move drops it",
}
COMPANIONS = {**COMPANION_DROPS, **COMPANION_SETS}
# Fields write_agreement computes from the change itself, never taken from a caller.
DERIVED = {"consents": "write_agreement keeps exactly the consents of the modes in force"}


@dataclass
class Row:
    field: str
    kind: str
    describer: str
    sites: list[str] = field(default_factory=list)


def registrations() -> list[str]:
    """Every register_fields call outside tests, as path:line."""
    found = []
    for top in SOURCE_TOPS:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "/tests/" in str(path) or "/test_" in str(path):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bregister_fields\(\{", line):
                    found.append(f"{path.relative_to(ROOT)}:{n}")
    return found


def _module(site: str) -> str:
    path = Path(site.rsplit(":", 1)[0])
    parts = path.with_suffix("").parts
    return ".".join(parts[2:] if parts[0] in ("default_modules", "premium_modules") else parts)


def describers() -> dict:
    """Every registered History describer, each registering module imported."""
    for site in registrations():
        importlib.import_module(_module(site))
    from ui.components import activity
    return dict(activity._FIELD_DESCRIBERS)


def writes() -> list[owned_fields_census.Write]:
    return owned_fields_census.writes()


def fields() -> dict[str, Row]:
    found = describers()
    rows: dict[str, Row] = {}
    for w in writes():
        for f in sorted({*w.sets, *w.drops}):
            if f not in rows:
                kind = ("derived" if f in DERIVED else "described" if f in found and f not in COMPANIONS
                        else "companion" if f in COMPANIONS else "generic" if f in GENERIC else "unnamed")
                rows[f] = Row(f, kind, getattr(found.get(f), "__qualname__", "generic line") if f in found
                              else "generic line")
            rows[f].sites.append(w.where)
    for f in DERIVED:
        rows.setdefault(f, Row(f, "derived", getattr(found.get(f), "__qualname__", "generic line"),
                               ["billing.write_agreement"]))
    return rows


def companions_alone() -> list[str]:
    """Each write site that records a companion set or drop and no field History renders."""
    rows = fields()
    alone = []
    for w in writes():
        quiet = {*(w.sets & set(COMPANION_SETS)), *(w.drops & set(COMPANION_DROPS))}
        shown = {f for f in {*w.sets, *w.drops} - quiet if rows[f].kind in ("described", "generic", "companion")}
        if quiet and not shown:
            alone.append(f"{w.where}: {', '.join(sorted(quiet))}")
    return alone


def moves_not_through_changed_fields() -> list[str]:
    """Each agreement writer outside write_agreement whose function does not build its
    fields through billing.changed_fields."""
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(PACKAGE.glob("*.py"))}
    missing = []
    for w in writes():
        if w.through_sink:
            continue
        path, rest = w.where.split(":", 1)
        fn_name = rest.split(" ", 1)[1]
        fn = next(n for n in ast.walk(trees[ROOT / path])
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name)
        calls = {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if "changed_fields" not in calls:
            missing.append(w.where)
    return missing


def main() -> None:
    from celerp_subscriptions import periods

    unset_values = getattr(periods, "UNSET", {})

    print("# History census: register_fields calls")
    for site in registrations():
        print(f"register {site}")
    print("# fields an agreement event records: field | kind | renderer | UNSET | write sites")
    rows = fields()
    for f in sorted(rows):
        r = rows[f]
        unset = repr(unset_values[f]) if f in unset_values else "-"
        print(f"field {f} | {r.kind} | {r.describer} | {unset} | {'; '.join(sorted(set(r.sites)))}")
    print("# registered describers no writer records")
    for f in sorted(set(describers()) - set(rows)):
        print(f"unwritten {f}")
    for line in companions_alone():
        print(f"companion-alone {line}")
    for line in moves_not_through_changed_fields():
        print(f"move-not-through-changed_fields {line}")
    print(f"# {len(rows)} fields, {len(registrations())} registrations")


if __name__ == "__main__":
    main()
