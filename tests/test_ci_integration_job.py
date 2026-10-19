# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""CI runs the integration validation suite (tests/test_integration_validation.py) in a
job of its own, on a real database, and the Test check waits for it."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def check_integration_validation_job(workflow: dict) -> list[str]:
    """What keeps CI from running the integration suite in a job of its own on a real database;
    empty when nothing does."""
    problems = []
    jobs = workflow.get("jobs", {})
    job = jobs.get("integration")
    if job is None:
        return ["no integration job"]
    if "postgres" not in (job.get("services") or {}):
        problems.append("the integration job has no Postgres service")
    commands = " ".join(step.get("run", "") for step in job.get("steps", []))
    if "tests/test_integration_validation.py" not in commands or "-m integration" not in commands:
        problems.append("the integration job does not run the integration suite")
    for name in ("shard", "process"):
        runs = " ".join(step.get("run", "") for step in jobs.get(name, {}).get("steps", []))
        if "not integration" not in runs:
            problems.append(f"the {name} job also collects the integration suite")
    if "integration" not in (jobs.get("test", {}).get("needs") or []):
        problems.append("the Test check does not wait for the integration job")
    return problems


def test_ci_runs_the_integration_suite_in_its_own_job():
    assert check_integration_validation_job(yaml.safe_load(WORKFLOW.read_text())) == []
