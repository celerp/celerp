# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The release workflows' own step scripts, run with the GitHub API stubbed.

PyPI cannot take a release back, so the GitHub release is published only after
the PyPI release of the same commit succeeded, and a version already released is
never rebuilt.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

_CURL = r"""#!/bin/sh
echo "$*" >> "$CURL_LOG"
case "$*" in
  *http_code*) printf '%s' "$HTTP_STATUS" ;;
  *publish.yml/runs*) printf '%s' "$RUNS" ;;
  *releases\?per_page*) printf '[{"id": 7, "tag_name": "v9.9.9", "body": "Notes"}]' ;;
  *) printf '{"id": 7}' ;;
esac
"""


def _steps(workflow: str, job: str) -> list[dict]:
    return yaml.safe_load((_WORKFLOWS / workflow).read_text())["jobs"][job]["steps"]


def _env(tmp_path: Path, **env: str) -> dict:
    """GitHub's step environment, with curl, sleep and pip stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("curl", _CURL), ("sleep", "#!/bin/sh\n"), ("pip", "#!/bin/sh\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CURL_LOG": str(tmp_path / "curl.log"),
            "GH_TOKEN": "t", "GITHUB_SHA": "abc123", "GITHUB_REF_NAME": "v9.9.9",
            "GITHUB_ENV": str(tmp_path / "github_env"), "HTTP_STATUS": "200", "RUNS": "", **env}


def _run(tmp_path: Path, script: str, env: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "step.sh"
    path.write_text(script)
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
                          env=env, capture_output=True, text=True)


def _run_job(tmp_path: Path, workflow: str, job: str, **env: str) -> tuple[int, str, list[str]]:
    """Runs the job's steps in order as GitHub does, stopping at the first failure."""
    base = _env(tmp_path, **env)
    out, code = "", 0
    for step in _steps(workflow, job):
        r = _run(tmp_path, step["run"], {**base, **step.get("env", {}), **env, "GH_TOKEN": "t"})
        out += r.stdout + r.stderr
        code = r.returncode
        if code:
            break
    log = tmp_path / "curl.log"
    return code, out, log.read_text().splitlines() if log.exists() else []


def _runs(status: str, conclusion: str | None) -> str:
    return json.dumps({"workflow_runs": [{"status": status, "conclusion": conclusion, "run_attempt": 2}]})


def _set_version(tmp_path: Path, version: str) -> subprocess.CompletedProcess:
    step = next(s for s in _steps("publish.yml", "build") if s.get("name") == "Verify version matches tag")
    return _run(tmp_path, step["run"], _env(tmp_path, GITHUB_EVENT_NAME="workflow_dispatch", VERSION=version))


@pytest.mark.parametrize("runs", [_runs("completed", "failure"), _runs("completed", "cancelled")])
def test_a_failed_pypi_release_leaves_the_github_release_a_draft(tmp_path, runs):
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release", RUNS=runs)
    assert code != 0, out
    assert not any("PATCH" in c for c in calls), calls


def test_a_pypi_release_that_never_finishes_leaves_the_github_release_a_draft(tmp_path):
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release",
                                RUNS=_runs("in_progress", None))
    assert code != 0, out
    assert "did not finish within 90 minutes" in out
    assert sum("publish.yml/runs?head_sha=abc123&event=push" in c for c in calls) == 90
    assert not any("PATCH" in c for c in calls), calls


def test_the_github_release_is_published_once_the_pypi_release_succeeded(tmp_path):
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release",
                                RUNS=_runs("completed", "success"))
    assert code == 0, out
    assert any("PATCH" in c and "releases/7" in c for c in calls), calls


@pytest.mark.parametrize("status", ["200", "500"])
def test_a_tag_already_released_or_unreadable_is_not_rebuilt(tmp_path, status):
    code, out, calls = _run_job(tmp_path, "build.yml", "prepare-release", HTTP_STATUS=status)
    assert code != 0, out
    assert not any("PATCH" in c or "DELETE" in c for c in calls), calls


def test_a_new_tag_proceeds_to_the_builds(tmp_path):
    code, out, _ = _run_job(tmp_path, "build.yml", "prepare-release", HTTP_STATUS="404")
    assert code == 0, out


def test_a_manual_candidate_build_takes_its_version_from_the_input(tmp_path):
    r = _set_version(tmp_path, "2.5.4")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TAG=2.5.4" in (tmp_path / "github_env").read_text()


@pytest.mark.parametrize("version", ["2.5", "2.5.4; echo x", "v2.5.4", ""])
def test_a_manual_candidate_build_refuses_a_malformed_version(tmp_path, version):
    r = _set_version(tmp_path, version)
    assert r.returncode != 0
    assert not (tmp_path / "github_env").exists()


def test_pypi_publishing_runs_only_on_a_tag_push():
    job = yaml.safe_load((_WORKFLOWS / "publish.yml").read_text())["jobs"]["publish"]
    assert job.get("if") == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
