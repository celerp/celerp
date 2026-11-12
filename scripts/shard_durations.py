# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Keep tests/.test_durations measuring exactly the tests the split CI jobs run.

    python scripts/shard_durations.py           # time every unmeasured test, drop stale entries
    python scripts/shard_durations.py --check   # list them and exit 1 if there are any

The ids a job collects change with the tests themselves and with everything a test takes its
parameters from or the run is selected by:
- the shipped sources (every .py under shipped_sources.SHIPPED_ROOTS, e.g. test_python310_floor),
- the alembic revisions (e.g. test_backfills_run_once),
- pyproject.toml (pytest markers and addopts),
- conftest.py.
So adding, moving or renaming any shipped module can change them as much as a new test can.
tests/test_ci_shard_split.py fails on any id the file does not measure; this script is the one
place that collects the jobs' ids, and the one way to bring the file back in line. Run --check
before every push, whatever it touches (it is cheap: it only collects).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = ROOT / "tests" / ".test_durations"

# The job's own options that split or report the run rather than choose its tests.
_RUN_ONLY = {"-n": 1, "--splits": 1, "--group": 1, "--splitting-algorithm": 1, "--durations-path": 1, "-q": 0}


@cache
def split_jobs() -> dict[str, tuple[list, list[str]]]:
    """Each CI job that splits by the durations file: its matrix groups and its pytest words."""
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    found = {}
    for name, job in jobs.items():
        for step in job.get("steps", []):
            if "--durations-path" in step.get("run", ""):
                # One group's number stands in for the matrix expression, which is no shell word.
                found[name] = (job["strategy"]["matrix"]["group"],
                               shlex.split(step["run"].replace("${{ matrix.group }}", "1")))
    return found


def flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name] + [
        a.split("=", 1)[1] for a in args if a.startswith(name + "=")
    ]


def dev_extra() -> list[Requirement]:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    return list(map(Requirement, dev))


def tooling_unlike_ci() -> list[str]:
    """The dev extra's requirements the installed packages do not meet."""
    unmet = []
    for req in dev_extra():
        if req.marker and not req.marker.evaluate():
            continue
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            installed = None
        if installed is None or not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req} (installed: {installed})")
    return unmet


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}


@cache
def ids_the_job_runs(job: str) -> frozenset[str]:
    """The test ids, with their parameters, that the job collects: its own paths and options,
    collected the way the job collects them."""
    assert not tooling_unlike_ci(), "install the dev extra first: ids collected here would not be CI's"
    _, args = split_jobs()[job]
    chosen, rest = [], iter(args[args.index("pytest") + 1:])
    for arg in rest:
        if arg in _RUN_ONLY:
            for _ in range(_RUN_ONLY[arg]):
                next(rest)
        elif "/" in arg and not arg.startswith("-") and "*" in arg:
            chosen += sorted(p.relative_to(ROOT).as_posix() + "/" for p in ROOT.glob(arg.rstrip("/")))
        else:
            chosen.append(arg)
    # One id a line whatever -q the job's addopts carry: --verbosity sets the level outright.
    out = subprocess.run([sys.executable, "-m", "pytest", *chosen, "--collect-only", "--verbosity=-1", "-p", "no:cacheprovider"],
                         cwd=ROOT, env=_clean_env(), capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]
    return frozenset(line for line in out.stdout.splitlines() if "::" in line and not line.startswith(" "))


def durations() -> dict[str, float]:
    return json.loads(DURATIONS.read_text())


def unmeasured(job: str) -> list[str]:
    """The tests the job runs that the file has no entry for (each counts as the average)."""
    return sorted(ids_the_job_runs(job) - durations().keys())


def stale() -> list[str]:
    """The entries that name a test no split job runs."""
    return sorted(durations().keys() - frozenset().union(*map(ids_the_job_runs, split_jobs())))


def _time(ids: list[str]) -> dict[str, float]:
    """Run the given tests once and return what pytest-split measured for each."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "durations.json"
        path.write_text("{}")
        out = subprocess.run([sys.executable, "-m", "pytest", *ids, "-q", "--no-cov", "-o", "addopts=",
                              "--store-durations", "--durations-path", str(path)],
                             cwd=ROOT, env=_clean_env(), capture_output=True, text=True)
        if out.returncode != 0:
            sys.exit(out.stdout[-4000:] + out.stderr[-4000:] + "\nthese tests must pass before they are timed")
        timed = json.loads(path.read_text())
    missing = sorted(set(ids) - timed.keys())
    if missing:
        sys.exit("pytest-split measured none of: " + ", ".join(missing))
    return {i: timed[i] for i in ids}


def main(argv: list[str]) -> int:
    new = sorted({t for job in split_jobs() for t in unmeasured(job)})
    gone = stale()
    for t in new:
        print(f"unmeasured {t}")
    for t in gone:
        print(f"stale {t}")
    if not new and not gone:
        return 0
    if "--check" in argv:
        print("tests/.test_durations is out of date: run python scripts/shard_durations.py and commit it")
        return 1
    kept = {k: v for k, v in durations().items() if k not in gone}
    # One run per job: a browser job's event loop must not share a process with async unit tests.
    for job in split_jobs():
        ids = [t for t in unmeasured(job) if t not in kept]
        kept.update(_time(ids) if ids else {})
    DURATIONS.write_text(json.dumps(kept, indent=4) + "\n")
    print(f"tests/.test_durations: {len(new)} timed, {len(gone)} dropped; commit it")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
