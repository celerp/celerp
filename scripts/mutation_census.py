# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Report what the write observer (mutation_observer.py) saw across a test run.

    python scripts/mutation_census.py <census folder>... [--out census.md] [--coverage coverage.json] [--baseline file]
        [--gaps file] [--base ref] [--enforce]

Reads every census-*.json and census-*.routes.json under the folders (one pair per test
process, so the folders of all CI shards together give the whole suite) and writes:

- the route inventory: every mutating route the test apps serve, checked against the
  mutating route decorators in the bundled source, with each route's class: it made a
  durable write of company data, it was requested and wrote nothing, or no test requested it;
- the read routes: every GET and HEAD route, whether a test requested it, and for each one
  no test requested, whether its source reaches a write (scripts/static_calls.py);
- the findings: each distinct write that was not protected by the company lock, SQL the
  observer could not classify, and writes to tables that hold no company's data;
- mutation-site coverage: the places in the bundled source that write (emit_event, a
  session add, delete or merge, an executed statement that writes, built or as SQL text,
  and any session call scripts/static_calls.py cannot classify as a read) and whether any observed durable write passed through each: a statement counts for the line
  it ran from, an added, deleted or merged row for the line that called the session, both
  recorded when they happen.

- the baseline: each accepted finding (mutation_baseline.json) with its tracking issue, and
  every entry no finding matched or its own reproducing test did not see;
- the coverage gaps: each mutating route whose source reaches a write that no test made
  write durably, and each write site no request write passed through, against the accepted
  ones (mutation_coverage_gaps.json), with every entry that no longer matches its gap, that
  the base branch's list (git ref origin/main by default) does not hold, or that counts more
  write sites than the base branch's copy of it.

With --enforce the exit status is 1 when a finding matches no baseline entry, an entry
is stale or not reproduced by its test, a read route no test requested reaches a write, a
coverage gap has no entry, or a coverage-gap entry no longer matches its gap, is not in the
base branch's list or counts more write sites than the base branch's copy: the list and each
site entry's count only shrink, so a newly untested writer cannot take the place of an accepted
one or join one."""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import subprocess
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import mutation_baseline  # noqa: E402
import mutation_read_notes  # noqa: E402
import static_calls  # noqa: E402
from static_calls import _called, _receiver  # noqa: E402

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
READING = {"GET", "HEAD"}
# Functions a read route reaches whose writes no read makes, each with the reason: the
# static scan of a read route no test requested neither enters nor reports them.
READ_SAFE = {
    "ui.api_client.get_items_metadata": "POST /items/metadata carries the id list; the route only reads",
    "ui.api_client.manufacturing_requirements": "POST /manufacturing/to-make/requirements carries the item ids; "
                                                "the route only reads",
    "ui.api_client.doctor_report": "POST /admin/doctor without fix is the dry-run report; it writes nothing",
    "ui.api_client.get_billing_portal_url": "POST /settings/cloud/billing-portal asks the relay for a portal link; "
                                            "nothing is saved",
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def static_inventory(graph):
    """Mutating route decorators and write sites in the bundled source."""
    decorators, sites = [], []
    for rel, tree in graph.trees.items():
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in fn.decorator_list:
                    if isinstance(deco, ast.Call) and _called(deco).upper() in MUTATING:
                        # the line the function's source starts on, the first decorator's
                        decorators.append((rel, fn.decorator_list[0].lineno, fn.name, _called(deco).upper(),
                                           _receiver(deco)))
    for rel, node, fn, module in graph.calls():
        if (graph.effect(node, fn, module) or "").startswith("write "):
            # The innermost function's span from its first decorator, the line its code object starts on.
            sites.append((rel, node.lineno, node.end_lineno or node.lineno,
                          fn.node.name if fn else "<module>", _called(node),
                          (fn.first, fn.node.end_lineno) if fn else (node.lineno, node.end_lineno)))
    return decorators, sorted(sites)


def _read_noted(graph, route: str) -> set[str]:
    """The functions a READ_ROUTE_WRITES note lets *route* write through: the observer
    holds each write they make to the note's columns while the route runs."""
    sites = {site for note in mutation_read_notes.READ_ROUTE_WRITES.values() if note.route in (None, route)
             for site in note.sites}
    return {key for key, fn in graph.functions.items() if f"{fn.path} {fn.qualname.rpartition('.')[2]}" in sites}


def routes_that_write(rows, graph) -> dict[tuple, list]:
    """Each route among *rows* whose handler the static call graph shows reaching a write
    (a session write, a commit, a company lock or a mutating API request), with one chain to
    each, through no function of READ_SAFE or, for a read route, of a note READ_ROUTE_WRITES
    has for it."""
    found = {}
    for (app, method, path), row in rows.items():
        rel, _, line = row["source"].rpartition(":")
        handler = graph.by_source.get((rel, int(line))) if line.isdigit() else None
        if handler is not None:
            stop = set(READ_SAFE) | (_read_noted(graph, f"{method} {path}") if method in READING else set())
            chains = graph.doing([handler.key], lambda what: True, stop=stop)
            if chains:
                found[(app, method, path)] = chains
    return found


def load(folders):
    routes, sites, findings, inventory = defaultdict(lambda: defaultdict(int)), defaultdict(int), {}, {}
    files = 0
    for folder in folders:
        for path in glob.glob(os.path.join(folder, "**", "census-*.json"), recursive=True):
            if path.endswith(".routes.json"):
                for row in json.loads(_read(path)):
                    for method in row["methods"]:
                        inventory[(row["app"], method, row["path"])] = row
                continue
            files += 1
            data = json.loads(_read(path))
            for route, counts in data["routes"].items():
                for what, n in counts.items():
                    routes[route][what] += n
            for site, n in data["sites"].items():
                sites[site] += n
            for f in data["findings"]:
                key = f["key"]
                if key in findings:
                    findings[key]["count"] += f["count"]
                    findings[key]["tests"] += [t for t in f["tests"] if t not in findings[key]["tests"]]
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
    data = json.loads(_read(path))["files"]
    return {rel: (set(f["executed_lines"]), f.get("executed_branches", []), f.get("missing_branches", []))
            for rel, f in data.items()}


def _passed_through(site, sent) -> bool:
    """A durable write was made on the site's own lines: the observer records the line a
    statement ran from, and the line that called session.add, delete or merge."""
    _, start, end, fn, _, (first, _) = site
    return any(name == fn and start <= line <= end and (begins is None or begins == first)
               for line, name, begins in sent)


def report(folders, out, coverage_path=None, baseline=mutation_baseline.PATH, gaps=mutation_baseline.GAPS_PATH,
           base=mutation_baseline.GAPS_BASE):
    files, routes, sites, findings, inventory = load(folders)
    coverage = _coverage(coverage_path)
    graph = static_calls.Graph()
    decorators, static_sites = static_inventory(graph)
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
        wrote = sum(n for what, n in counts.items()
                    if what in ("protected", "unprotected", "lock-order", "cross-company", "writes through"))
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

    # -- read routes -----------------------------------------------------------
    # A read route must write nothing durable. A requested one is judged by the observer;
    # one no test requested is judged from its source, and fails when its handler reaches a write.
    reading = {}
    for (app, method, path), row in sorted(inventory.items()):
        if method in READING:
            reading.setdefault((app, path), row)
    def _requested(app, path):
        return any(routes.get(f"{_APP_LABEL.get(app, app)} {m} {path}", {}).get("requests") for m in READING)
    unrequested = {(app, "GET", path): row for (app, path), row in reading.items() if not _requested(app, path)}
    untested = {(app, method, path): row for cls in mutation_baseline.ROUTE_CLASSES
                for app, method, path, _, _ in classes.get(cls, []) for row in [mutating[(app, method, path)]]}
    reaching = routes_that_write({**unrequested, **untested}, graph)
    writing = {k: v for k, v in reaching.items() if k in unrequested}
    lines += ["## Read routes", "",
              f"- GET and HEAD routes the test apps serve: {len(reading)} "
              f"(api {sum(1 for k in reading if k[0] == 'FastAPI')}, ui {sum(1 for k in reading if k[0] == 'FastHTML')}); "
              f"requested: {len(reading) - len(unrequested)}; never requested: {len(unrequested)}.",
              f"- Never requested and reaching a write in their source: {len(writing)}.", "",
              "### Never requested", "", "| App | Route | Source | Reaches a write |", "|---|---|---|---|"]
    for (app, method, path), row in unrequested.items():
        chains = writing.get((app, method, path), [])
        reach = "; ".join(f"`{static_calls.describe(c)}`" for c in chains[:3]) or "no"
        lines.append(f"| {_APP_LABEL.get(app, app)} | {path} | {row['source']} | {reach} |")
    lines.append("")

    # -- findings --------------------------------------------------------------
    entries = mutation_baseline.load(baseline)
    by_kind, by_entry = defaultdict(list), defaultdict(list)
    for f in findings.values():
        key = mutation_baseline.key(f)
        if key in entries:
            by_entry[key].append(f)
        else:
            by_kind[f["kind"]].append(f)
    lines += ["## Findings", "", "Writes no baseline entry accepts.", "",
              "| Kind | Distinct | Seen |", "|---|---|---|"]
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

    # -- baseline --------------------------------------------------------------
    failing = 0
    lines += ["## Baseline", "", f"Accepted findings (mutation_baseline.json): {len(entries)} entries.", "",
              "| Route | Table | Seen | Status | Issue |", "|---|---|---|---|---|"]
    for key, entry in entries.items():
        seen = by_entry.get(key, [])
        tests = {mutation_baseline.test_function(t) for f in seen for t in f["tests"]}
        status = "stale: no finding matched" if not seen else \
            "not reproduced by its test" if entry["test"] not in tests else "matched"
        failing += status != "matched"
        lines.append(f"| `{entry['route']}` | {entry['table']} | {sum(f['count'] for f in seen)} | {status} | "
                     f"{entry['issue']} |")
    lines.append("")

    # -- mutation sites --------------------------------------------------------
    sent = defaultdict(set)  # file -> (line, function, the line its function starts on or None)
    for site in sites:
        location, _, function = site.partition(" ")
        path, _, line = location.rpartition(":")
        name, _, first = function.partition("@")
        sent[path].add((int(line), name, int(first) if first else None))
    covered, missed = [], []
    for site in static_sites:
        (covered if _passed_through(site, sent.get(site[0], ())) else missed).append(site)
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

    # -- coverage gaps ---------------------------------------------------------
    # A writer no test covers: a mutating route reaching a write that no test made write
    # durably, and the write sites of one function and call no request write passed through.
    route_gaps = {("route", f"{_APP_LABEL.get(a, a)} {m} {p}"): (cls, f"| {_APP_LABEL.get(a, a)} | {m} {p} | {src} | {cls} |")
                  for cls in mutation_baseline.ROUTE_CLASSES for a, m, p, src, _ in classes.get(cls, [])
                  if (a, m, p) in reaching}
    site_gaps = defaultdict(list)
    for rel, start, _, fn, name, _ in missed:
        site_gaps[("site", f"{rel} {fn} {name}")].append(start)
    known_sites = {("site", f"{rel} {fn} {name}") for rel, _, _, fn, name, _ in static_sites}
    served = {("route", f"{_APP_LABEL.get(a, a)} {m} {p}") for a, m, p in mutating}
    gap_entries = mutation_baseline.load_gaps(gaps)
    try:
        base_entries = mutation_baseline.gaps_at(base, gaps)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"cannot read the coverage gaps at {base} ({exc.stderr.strip()}); "
                         f"fetch it first: git fetch origin main") from exc
    unnamed = [k for k in (*route_gaps, *site_gaps) if k not in gap_entries]
    lines += ["## Coverage gaps", "",
              f"- Mutating routes reaching a write that no test made write: {len(route_gaps)}; write sites "
              f"(by file, function and call) no request write passed through: {len(site_gaps)}.",
              f"- Accepted (mutation_coverage_gaps.json): {len(gap_entries)}; named by no entry: {len(unnamed)}; "
              f"compared with the list at {base}" + ("." if base_entries is not None else ", which has none yet."), "",
              "### Named by no entry", "", "| App | Route | Source | Class |", "|---|---|---|---|"]
    lines += [route_gaps[k][1] for k in unnamed if k[0] == "route"]
    lines += ["", "| Site | Count | Lines |", "|---|---|---|"]
    lines += [f"| {k[1]} | {len(site_gaps[k])} | {', '.join(map(str, sorted(site_gaps[k])))} |"
              for k in unnamed if k[0] == "site"]
    lines += ["", "### Accepted", "", "| Kind | Gap | Class | Status |", "|---|---|---|---|"]
    gap_failing = len(unnamed)
    for k, entry in gap_entries.items():
        if k in route_gaps:
            status = "matched" if route_gaps[k][0] == entry["class"] else f"class is {route_gaps[k][0]}"
        elif k in site_gaps:
            n = len(site_gaps[k])
            status = "matched" if n == entry["count"] else f"{n} site(s), entry says {entry['count']}"
        elif k in served or k in known_sites:
            status = "stale: covered"
        else:
            status = "stale: no such gap"
        if base_entries is not None and k not in base_entries:
            status = f"not in the list at {base}"
        elif base_entries is not None and k[0] == "site" and entry["count"] > base_entries[k]["count"]:
            status = f"{entry['count']} site(s), the list at {base} holds {base_entries[k]['count']}"
        gap_failing += status != "matched"
        lines.append(f"| {k[0]} | {k[1]} | {entry['class']} | {status} |")
    lines.append("")

    text = "\n".join(lines) + "\n"
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return sum(len(v) for v in by_kind.values()) + failing + len(writing) + gap_failing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("folders", nargs="+")
    parser.add_argument("--out")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--coverage", help="a coverage.py JSON report of the same run, with branches")
    parser.add_argument("--baseline", default=mutation_baseline.PATH, help="the accepted findings")
    parser.add_argument("--gaps", default=mutation_baseline.GAPS_PATH, help="the accepted coverage gaps")
    parser.add_argument("--base", default=mutation_baseline.GAPS_BASE,
                        help="the git ref whose coverage gaps the accepted ones may only shrink")
    args = parser.parse_args(argv)
    count = report(args.folders, args.out, args.coverage, args.baseline, args.gaps, args.base)
    return 1 if args.enforce and count else 0


if __name__ == "__main__":
    sys.exit(main())
