# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The mutation census is part of the single required status: the Test check waits for every
job that writes census files or enforces the census, and fails unless each one succeeded."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def census_jobs(jobs: dict) -> set[str]:
    """The jobs that upload the write observer's census files or run the census report."""
    found = set()
    for name, job in jobs.items():
        for step in job.get("steps", []):
            uploads = "upload-artifact" in step.get("uses", "") and \
                str((step.get("with") or {}).get("name", "")).startswith("census-shard")
            if uploads or "mutation_census.py" in step.get("run", ""):
                found.add(name)
    return found


def check_census_in_test(workflow: dict) -> list[str]:
    """What lets the Test check pass while a census job did not succeed; empty when nothing does."""
    jobs = workflow.get("jobs", {})
    test = jobs.get("test", {})
    needs = test.get("needs") or []
    checks = " ".join(step.get("run", "") for step in test.get("steps", []))
    problems = []
    names = census_jobs(jobs)
    if not any("mutation_census.py" in step.get("run", "") and "--enforce" in step["run"]
               for name in names for step in jobs[name].get("steps", [])):
        problems.append("no job enforces the census")
    for name in sorted(names):
        if name not in needs:
            problems.append(f"the Test check does not wait for {name}")
        if f'"${{{{ needs.{name}.result }}}}" != "success"' not in checks:
            problems.append(f"the Test check does not require {name} to succeed")
        elif f'"${{{{ needs.{name}.result }}}}" != "skipped"' in checks:
            problems.append(f"the Test check lets {name} be skipped")
    return problems


def test_the_test_check_requires_every_census_job_to_succeed():
    assert check_census_in_test(yaml.safe_load(WORKFLOW.read_text())) == []


def test_a_census_job_the_test_check_does_not_wait_for_is_found():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    workflow["jobs"]["test"]["needs"] = [n for n in workflow["jobs"]["test"]["needs"] if n != "shard"]
    assert "the Test check does not wait for shard" in check_census_in_test(workflow)
