# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Report what the write observer (mutation_observer.py) saw across a test run.

    python scripts/mutation_census.py <census folder>... [--out census.md] [--coverage coverage.json] [--enforce]

Reads every census-*.json and census-*.routes.json under the folders (one pair per test
process, so the folders of all CI shards together give the whole suite) and writes:

- the route inventory: every mutating route the test apps serve, checked against the
  mutating route decorators in the bundled source, with each route's class: it made a
  durable write of company data, it was requested and wrote nothing, or no test requested it;
- the findings: each distinct write that was not protected by the company lock, SQL the
  observer could not classify, and writes to tables that hold no company's data;
- mutation-site coverage: the places in the bundled source that write (emit_event, a
  session add, delete or merge, an INSERT, UPDATE or DELETE construct executed) and
  whether any observed durable write passed through each.

With --enforce the exit status is 1 when there is any finding."""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
SOURCES = ("celerp", "default_modules", "ui")
_SKIP_PARTS = ("/tests/", "/migrations/", "/__pycache__/")


def _python_files(top: str):
    for folder, _, files in os.walk(os.path.join(ROOT, top)):
        for name in files:
            path = os.path.join(folder, name)
            if name.endswith(".py") and not any(p in path + "/" for p in _SKIP_PARTS):
                yield path


def _called(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _receiver(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Attribute):
            return value.attr
        if isinstance(value, ast.Name):
            return value.id
    return ""


def static_inventory():
    """Mutating route decorators and write sites in the bundled source."""
    decorators, sites = [], []
    for top in SOURCES:
        for path in _python_files(top):
            rel = os.path.relpath(path, ROOT)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for fn in functions:
                for deco in fn.decorator_list:
                    if isinstance(deco, ast.Call) and _called(deco).upper() in MUTATING:
                        # the line the function's source starts on, the first decorator's
                        decorators.append((rel, fn.decorator_list[0].lineno, fn.name, _called(deco).upper(),
                                           _receiver(deco)))
            owner = {}
            for fn in functions:
                for child in ast.walk(fn):
                    if hasattr(child, "lineno"):
                        owner.setdefault(id(child), fn)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name, receiver = _called(node), _receiver(node).lower()
                write = name == "emit_event" or (
                    name in ("add", "add_all", "delete", "merge") and "session" in receiver) or (
                    name == "execute" and "session" in receiver and node.args
                    and isinstance(node.args[0], ast.Call) and _called(node.args[0]) in ("insert", "update", "delete"))
                if write:
                    fn = owner.get(id(node))
                    sites.append((rel, node.lineno, node.end_lineno or node.lineno,
                                  fn.name if fn else "<module>", name,
                                  (fn.lineno, fn.end_lineno) if fn else (node.lineno, node.end_lineno)))
    return decorators, sites


def load(folders):
    routes, sites, findings, inventory = defaultdict(lambda: defaultdict(int)), defaultdict(int), {}, {}
    files = 0
    for folder in folders:
        for path in glob.glob(os.path.join(folder, "**", "census-*.json"), recursive=True):
            if path.endswith(".routes.json"):
                for row in json.load(open(path, encoding="utf-8")):
                    for method in row["methods"]:
                        inventory[(row["app"], method, row["path"])] = row
                continue
            files += 1
            data = json.load(open(path, encoding="utf-8"))
            for route, counts in data["routes"].items():
                for what, n in counts.items():
                    routes[route][what] += n
            for site, n in data["sites"].items():
                sites[site] += n
            for f in data["findings"]:
                key = (f["kind"], f["route"], f["table"], f["detail"], f["sql"][:200], tuple(f["where"]))
                if key in findings:
                    findings[key]["count"] += f["count"]
                    findings[key]["tests"] = (findings[key]["tests"] + f["tests"])[:3]
                else:
                    findings[key] = f
    return files, routes, sites, findings, inventory


_APP_LABEL = {"FastAPI": "api", "FastHTML": "ui"}


def _count(items) -> dict:
    counts = defaultdict(int)
    for item in items:
        counts[item] += 1
    return counts


def _coverage(path):
    """Lines and branches a coverage.py JSON report (--cov-branch) records per file."""
    if not path:
        return None
    data = json.load(open(path, encoding="utf-8"))["files"]
    return {rel: (set(f["executed_lines"]), f.get("executed_branches", []), f.get("missing_branches", []))
            for rel, f in data.items()}


def report(folders, out, coverage_path=None):
    files, routes, sites, findings, inventory = load(folders)
    coverage = _coverage(coverage_path)
    decorators, static_sites = static_inventory()
    lines = [f"# Mutation census", "", f"Census files: {files} test processes across {len(folders)} folder(s).", ""]

    # -- routes ----------------------------------------------------------------
    bundled = [d for d in decorators if not d[0].startswith("ui/")]
    mutating = {k: v for k, v in inventory.items() if k[1] in MUTATING}
    mounted = {(row["source"], method) for (_, method, _), row in mutating.items()}
    unmounted = [d for d in decorators if (f"{d[0]}:{d[1]}", d[3]) not in mounted]
    lines += ["## Route inventory", "",
              f"- Mutating route decorators in celerp/ and default_modules/: {len(bundled)} "
              f"in {len({d[0] for d in bundled})} files; in ui/: {len(decorators) - len(bundled)}.",
              f"- Mutating routes the test apps serve: {len(mutating)} "
              f"(api {sum(1 for k in mutating if k[0] == 'FastAPI')}, ui {sum(1 for k in mutating if k[0] == 'FastHTML')}).",
              f"- Receivers of the bundled decorators: "
              f"{', '.join(f'{r or "?"} {n}' for r, n in sorted(_count(d[4] for d in bundled).items()))}.",
              f"- Decorated in the source but not served by the test apps: {len(unmounted)}.", ""]
    classes = defaultdict(list)
    for (app, method, path), row in sorted(mutating.items()):
        counts = routes.get(f"{_APP_LABEL.get(app, app)} {method} {path}", {})
        wrote = sum(n for what, n in counts.items() if what in ("protected", "unprotected", "lock-order", "cross-company"))
        cls = "writes" if wrote else "requested, no durable write" if counts.get("requests") else "never requested"
        classes[cls].append((app, method, path, row["source"], counts))
    lines += ["| Class | Routes |", "|---|---|"] + [f"| {c} | {len(v)} |" for c, v in sorted(classes.items())] + [""]
    for cls in ("never requested", "requested, no durable write"):
        lines += [f"### {cls.capitalize()}", "", "| App | Route | Source |", "|---|---|---|"]
        lines += [f"| {_APP_LABEL.get(a, a)} | {m} {p} | {src} |" for a, m, p, src, _ in classes.get(cls, [])]
        lines.append("")
    if unmounted:
        lines += ["### Decorated but not served by the test apps", ""]
        lines += [f"- {m} {f}:{n} {fn}" for f, n, fn, m, _ in unmounted] + [""]

    # -- findings --------------------------------------------------------------
    by_kind = defaultdict(list)
    for f in findings.values():
        by_kind[f["kind"]].append(f)
    lines += ["## Findings", "", "| Kind | Distinct | Seen |", "|---|---|---|"]
    lines += [f"| {k} | {len(v)} | {sum(f['count'] for f in v)} |" for k, v in sorted(by_kind.items())] + [""]
    for kind, items in sorted(by_kind.items()):
        lines += [f"### {kind}", ""]
        grouped = defaultdict(list)
        for f in items:
            grouped[(f["route"], f["table"], f["where"][-1] if f["where"] else "")].append(f)
        for (route, table, site), group in sorted(grouped.items(), key=lambda g: (g[0][0], str(g[0][1]), g[0][2])):
            first = group[0]
            lines.append(f"- `{route}` table `{table}` at `{site or '?'}` x{sum(f['count'] for f in group)}: "
                         f"{first['detail']}; earlier locks {first['earlier_locks'] or 'none'}; "
                         f"company {first['company_from']}; e.g. {first['tests'][0]}")
            lines.append(f"  - where: {' <- '.join(reversed(first['where']))}")
            lines.append(f"  - sql: `{first['sql'][:160]}`")
        lines.append("")

    # -- mutation sites --------------------------------------------------------
    hit = defaultdict(set)
    for site in sites:
        location = site.split(" ", 1)[0]
        path, _, line = location.rpartition(":")
        hit[path].add(int(line))
    covered, missed = [], []
    for rel, start, end, fn, name, span in static_sites:
        (covered if any(start <= n <= end for n in hit.get(rel, ())) else missed).append((rel, start, end, fn, name, span))
    lines += ["## Mutation sites", "",
              f"- Write sites in the bundled source: {len(static_sites)}; passed through by an observed durable "
              f"write in a request: {len(covered)}; not: {len(missed)}.",
              "- A site not passed through is either outside request work (startup, background, migration, "
              "CLI) or a request write no test reaches.", ""]
    if coverage is None:
        per_file = defaultdict(list)
        for rel, start, _, fn, name, _ in missed:
            per_file[rel].append(f"{start} {fn} ({name})")
        lines += ["| File | Sites not passed through |", "|---|---|"]
        lines += [f"| {rel} | {'; '.join(v)} |" for rel, v in sorted(per_file.items())] + [""]
    else:
        # With a branch coverage report, a site no observed request write passed through
        # is either run by some test (outside a request, or a write the observer counts
        # elsewhere) or run by none; its function's branches show how much of it is reached.
        ran, never = defaultdict(list), defaultdict(list)
        for rel, start, end, fn, name, (first, last) in missed:
            executed, taken, untaken = coverage.get(rel, (set(), [], []))
            inside = lambda arcs: sum(1 for a, _ in arcs if first <= a <= last)
            branches = inside(taken) + inside(untaken)
            entry = f"{start} {fn} ({name}; branches {inside(taken)}/{branches})"
            (ran if any(start <= n <= end for n in executed) else never)[rel].append(entry)
        lines += [f"- Of the sites not passed through: run by a test {sum(len(v) for v in ran.values())}, "
                  f"run by no test {sum(len(v) for v in never.values())} (branches: taken/all in the "
                  f"site's function).", ""]
        for title, group in (("Run by no test", never), ("Run by a test, no observed request write", ran)):
            lines += [f"### {title}", "", "| File | Sites |", "|---|---|"]
            lines += [f"| {rel} | {'; '.join(v)} |" for rel, v in sorted(group.items())] + [""]

    text = "\n".join(lines) + "\n"
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return sum(len(v) for v in by_kind.values())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("folders", nargs="+")
    parser.add_argument("--out")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--coverage", help="a coverage.py JSON report of the same run, with branches")
    args = parser.parse_args(argv)
    count = report(args.folders, args.out, args.coverage)
    return 1 if args.enforce and count else 0


if __name__ == "__main__":
    sys.exit(main())
