# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every Python file a workflow runs as a script loads the way the workflow starts it: from the
repository root, with only the script's own folder on the import path. Pytest puts the root on the
path, so an import that works under the suite can still fail in the CI step that runs the file."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
SCRIPT = re.compile(r"\bpython3?\s+((?:[\w.-]+/)*[\w.-]+\.py)\b")

# Start the file as a script would (its folder first on the path, the working directory not on it) and run
# it only up to its last module-level import, never its work: some scripts act at import.
LOADER = """
import ast, os, sys
p = sys.argv[1]
sys.path[0] = os.path.dirname(p)
tree = ast.parse(open(p).read(), p)
last = max(i for i, n in enumerate(tree.body) if isinstance(n, (ast.Import, ast.ImportFrom)))
exec(compile(ast.Module(tree.body[:last + 1], []), p, "exec"), {"__name__": "ci_script_load_check", "__file__": p})
"""


def step_env(site) -> dict:
    """The import view of a workflow step: the packages the install step puts on the path (pyproject's
    package list, linked into ``site``) and nothing else from the checkout. The suite's conftest sets
    a PYTHONPATH of the whole checkout for its own subprocesses; a workflow step has none."""
    find = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]["find"]
    for name in {pkg.split(".")[0] for pkg in find["include"]}:
        (site / name).symlink_to(REPO / name)
    return {**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "PYTHONPATH": str(site)}


def workflow_scripts() -> list[str]:
    """Repository Python files that a workflow step runs with python <path>.py."""
    found = set()
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for rel in SCRIPT.findall(wf.read_text()):
            if (REPO / rel).is_file():
                found.add(rel)
    return sorted(found)


def test_workflows_run_python_scripts():
    assert "tests/lock_rank.py" in workflow_scripts()


@pytest.mark.timeout(120)
def test_every_workflow_script_loads_as_a_script(tmp_path):
    env = step_env(tmp_path)
    failures = []
    for rel in workflow_scripts():
        r = subprocess.run([sys.executable, "-c", LOADER, str(REPO / rel)], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            failures.append(f"{rel}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
    assert not failures, failures
