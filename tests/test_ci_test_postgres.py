# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The suite never changes the settings of a Postgres server it did not start. A preset
DATABASE_URL, such as a developer's own server, is used as it is; CI turns off durability on
its own throwaway Postgres service in a step of the job, which fails the job if it cannot."""

from __future__ import annotations

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
