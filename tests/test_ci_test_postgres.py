# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The suite never changes the settings of a Postgres server it did not start. A preset
DATABASE_URL, such as a developer's own server, is used as it is; CI turns off durability on
its own throwaway Postgres service in a step of the job, which fails the job if it cannot."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TUNE = "scripts/ci_tune_postgres.sh"


def test_the_suite_never_alters_a_postgres_server_it_did_not_start():
    """Red statement: conftest ran ALTER SYSTEM on any preset DATABASE_URL and ignored errors."""
    assert "ALTER SYSTEM" not in (ROOT / "conftest.py").read_text(encoding="utf-8")


def test_every_ci_job_on_a_postgres_service_tunes_it_before_its_tests():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    on_postgres = {name: job["steps"] for name, job in jobs.items() if "postgres" in (job.get("services") or {})}
    assert on_postgres
    for name, steps in on_postgres.items():
        runs = [step.get("run", "") for step in steps]
        tests = next(i for i, step in enumerate(steps) if str(step.get("name", "")).startswith("Run"))
        assert any(TUNE in run for run in runs[:tests]), name
    assert (ROOT / TUNE).is_file()


def _run_by_path() -> list[tuple[str, str]]:
    """Each repo file a workflow step runs as a command by its path (not through an interpreter
    such as ``bash`` or ``python``), with the workflow it is in. PowerShell scripts are left
    out: pwsh runs them whatever their mode."""
    found = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for job in (yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for command in re.split(r"&&|\|\||[;|\n]", str(step.get("run") or "")):
                    head = command.split()[0] if command.split() else ""
                    path = head.removeprefix("./")
                    if "/" in path and (ROOT / path).is_file() and not path.endswith(".ps1"):
                        found.append((workflow.name, path))
    return found


def test_every_script_a_workflow_runs_by_path_can_run():
    """A script run as a command needs the executable bit in git: the checkout restores the
    index mode, and a 100644 file gives "Permission denied" (exit 126) and fails the job.
    Red statement: ci.yml ran scripts/ci_tune_postgres.sh by path while git held it as 100644,
    so every job on a Postgres service failed before its tests."""
    for workflow, path in _run_by_path():
        mode = subprocess.run(["git", "ls-files", "-s", "--", path], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.split()[:1]
        assert mode == ["100755"], f"{workflow} runs {path} by path, but git holds it as {mode}"


def test_the_postgres_tune_step_runs_through_bash():
    """The tune step names its interpreter, so it runs whatever the file's mode."""
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if TUNE in line:
            assert line.strip().startswith(f"run: bash {TUNE} "), line
