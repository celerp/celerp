# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The release workflows' own step scripts, run with the GitHub API stubbed.

PyPI cannot take a release back, so it is published only after every desktop
build of the same tag and commit succeeded, the GitHub release only after the
PyPI release succeeded, and a version already released is never rebuilt. The
GitHub API is a local server answering with the shapes and job names GitHub
returns for a real release.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import yaml

_WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

_CURL = r"""#!/bin/sh
echo "$*" >> "$CURL_LOG"
case "$*" in
  *pypi.org*) printf '%s' "$PYPI_STATUS" ;;
  *http_code*) printf '%s' "$HTTP_STATUS" ;;
  *releases\?per_page*) printf '[{"id": 7, "tag_name": "v9.9.9", "body": "Notes"}]' ;;
  *) printf '{"id": 7}' ;;
esac
"""
_SHA = "e81b9a047a07f836484a87eb5353173d3e2486b4"


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
            "GH_TOKEN": "t", "GITHUB_SHA": _SHA, "GITHUB_REF_NAME": "v9.9.9",
            "GITHUB_REPOSITORY": "celerp/celerp", "GITHUB_API_URL": "http://127.0.0.1:9",
            "GITHUB_ENV": str(tmp_path / "github_env"), "HTTP_STATUS": "200", "PYPI_STATUS": "404", **env}


def _run(tmp_path: Path, script: str, env: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "step.sh"
    path.write_text(script)
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
                          env=env, capture_output=True, text=True, cwd=_WORKFLOWS.parent.parent)


def _run_job(tmp_path: Path, workflow: str, job: str, **env: str) -> tuple[int, str, list[str]]:
    """Runs the job's steps in order as GitHub does, stopping at the first failure. An
    action step is only recorded as reached."""
    base = _env(tmp_path, **env)
    out, code = "", 0
    for step in _steps(workflow, job):
        if "uses" in step:
            out += f"reached {step['uses']}\n"
            continue
        r = _run(tmp_path, step["run"], {**base, **step.get("env", {}), **env, "GH_TOKEN": "t"})
        out += r.stdout + r.stderr
        code = r.returncode
        if code:
            break
    log = tmp_path / "curl.log"
    return code, out, log.read_text().splitlines() if log.exists() else []


def _set_version(tmp_path: Path, version: str) -> subprocess.CompletedProcess:
    step = next(s for s in _steps("publish.yml", "build") if s.get("name") == "Verify version matches tag")
    return _run(tmp_path, step["run"], _env(tmp_path, GITHUB_EVENT_NAME="workflow_dispatch", VERSION=version))


@pytest.mark.parametrize("github,pypi", [("200", "404"), ("500", "404"), ("404", "200"), ("404", "500")],
                         ids=["on GitHub", "GitHub unreadable", "on PyPI", "PyPI unreadable"])
def test_a_tag_already_released_or_unreadable_is_not_rebuilt(tmp_path, github, pypi):
    code, out, calls = _run_job(tmp_path, "build.yml", "prepare-release", HTTP_STATUS=github, PYPI_STATUS=pypi)
    assert code != 0, out
    assert not any("PATCH" in c or "DELETE" in c for c in calls), calls


def test_a_new_tag_proceeds_to_the_builds(tmp_path):
    code, out, calls = _run_job(tmp_path, "build.yml", "prepare-release", HTTP_STATUS="404")
    assert code == 0, out
    assert any("https://pypi.org/pypi/celerp/9.9.9/json" in c for c in calls), calls


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




_PYPI = "reached pypa/gh-action-pypi-publish"
_ACTIONS = "/repos/celerp/celerp/actions"
_BUILD_RUN, _PUBLISH_RUN = 36595157056, 36595157121


def _matrix_job_names() -> dict[str, str]:
    """The desktop build job names GitHub derives from setup-matrix's tag matrix."""
    run = next(s for s in _steps("build.yml", "setup-matrix") if s.get("id") == "gen")["run"]
    matrix = [json.loads(m) for m in re.findall(r"^ *[a-z]+='(\{.*\})'$", run, re.M)]
    return {m["os"]: f"build ({', '.join(m.values())})" for m in matrix}


_BUILD = _matrix_job_names()
_LINUX, _WINDOWS, _MAC = _BUILD["ubuntu-latest"], _BUILD["windows-latest"], _BUILD["macos-latest"]


class _GitHub(BaseHTTPRequestHandler):
    """GitHub's REST API: each path (with its filter) serves its pages in turn,
    linked by a Link header as GitHub paginates. Anything else is a 404."""
    pages: dict[str, list[tuple[int, dict]]]
    calls: list[str]
    url: str

    def do_GET(self):
        url, query = urlsplit(self.path), parse_qs(urlsplit(self.path).query)
        self.calls.append(self.path)
        pages = self.pages.get(url.path + "".join(f"?filter={f}" for f in query.get("filter", [])),
                               [(404, {"message": "Not Found"})])
        page = int(query.get("page", ["1"])[0])
        status, body = pages[page - 1]
        self.send_response(status)
        if page < len(pages):
            following = urlencode({**{k: v[0] for k, v in query.items()}, "page": page + 1})
            self.send_header("Link", f'<{self.url}{url.path}?{following}>; rel="next"')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def github():
    handler = type("Handler", (_GitHub,), {"pages": {}, "calls": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield handler
    server.shutdown()
    server.server_close()


def _workflow_run(workflow: str, run_id: int, status: str = "completed", conclusion: str | None = "success",
                  attempt: int = 1, **identity: str) -> dict:
    """A run as GitHub lists it, by default the push of tag v9.9.9 at _SHA."""
    name = {"build.yml": "Build Binaries", "publish.yml": "Publish to PyPI"}[workflow]
    return {"id": run_id, "name": name, "path": f".github/workflows/{workflow}", "head_branch": "v9.9.9",
            "head_sha": _SHA, "event": "push", "status": status, "conclusion": conclusion,
            "run_attempt": attempt, "run_number": 2043, **identity}


def _job(name: str, conclusion: str | None = "success", attempt: int = 1) -> dict:
    return {"id": abs(hash((name, attempt))), "name": name, "head_sha": _SHA, "run_attempt": attempt,
            "status": "in_progress" if conclusion is None else "completed", "conclusion": conclusion}


def _build_jobs(conclusions: dict[str, str | None] | None = None) -> list[dict]:
    """build.yml's jobs for a tag in GitHub's order, each succeeded unless given by
    name; "absent" leaves a job out. The release job is still waiting for PyPI."""
    names = ["setup-matrix", "prepare-release", _LINUX, _WINDOWS, _MAC, "openapi-asset"]
    given = conclusions or {}
    jobs = [_job(n, given.get(n, "success")) for n in names if given.get(n) != "absent"]
    return jobs + [_job("publish-release", None)]


def _publish_jobs(publish: str | None = "success", attempt: int = 1) -> list[dict]:
    """publish.yml's jobs for a tag in GitHub's order; "absent" leaves out the publish job."""
    names = ["build", "pins (3.10)", "pins (3.11)", "pins (3.12)", "pins (3.13)",
             "verify (windows-latest)", "verify (macos-26)", "verify (ubuntu-latest)"]
    jobs = [_job(n, "success", attempt) for n in names]
    return jobs + ([] if publish == "absent" else [_job("publish", publish, attempt)])


def _serve(github, workflow: str, runs: list[dict], jobs: dict[int, list[dict]], filter: str) -> None:
    github.pages[f"{_ACTIONS}/workflows/{workflow}/runs"] = [
        (200, {"total_count": len(runs), "workflow_runs": runs})]
    for run_id, run_jobs in jobs.items():
        github.pages[f"{_ACTIONS}/runs/{run_id}/jobs?filter={filter}"] = [
            (200, {"total_count": len(run_jobs), "jobs": run_jobs})]


def _publish_to_pypi(tmp_path, github, runs: list[dict], jobs: dict[int, list[dict]]):
    _serve(github, "build.yml", runs, jobs, "latest")
    return _run_job(tmp_path, "publish.yml", "publish", GITHUB_API_URL=github.url)


def _publish_on_github(tmp_path, github, runs: list[dict], jobs: dict[int, list[dict]]):
    _serve(github, "publish.yml", runs, jobs, "all")
    return _run_job(tmp_path, "build.yml", "publish-release", GITHUB_API_URL=github.url)


def _polls(github, workflow: str) -> int:
    return sum(c.startswith(f"{_ACTIONS}/workflows/{workflow}/runs?") for c in github.calls)


def test_the_desktop_build_names_are_the_ones_github_gives_the_tag_matrix():
    assert _LINUX == "build (ubuntu-latest, pnpm run build:linux, --linux, linux-x64, x86_64-unknown-linux-gnu)"
    assert set(_BUILD) == {"ubuntu-latest", "windows-latest", "macos-latest"}


def test_pypi_is_published_once_every_desktop_build_and_the_schema_succeeded(tmp_path, github):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)],
                                    {_BUILD_RUN: _build_jobs()})
    assert code == 0, out
    assert f"{_ACTIONS}/runs/{_BUILD_RUN}/jobs?filter=latest&per_page=100" in github.calls
    assert _PYPI in out, out


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_pypi_is_not_published_when_a_finished_run_built_fewer_than_three_platforms(tmp_path, github, completed):
    absent = {name: "absent" for name in [_LINUX, _WINDOWS, _MAC][completed:]}
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "completed", "failure")],
                                    {_BUILD_RUN: _build_jobs(absent)})
    assert code != 0, out
    assert f"build.yml run {_BUILD_RUN} finished without build (" in out
    assert _PYPI not in out


@pytest.mark.parametrize("duplicate", [_LINUX, "openapi-asset"])
def test_pypi_is_not_published_when_a_required_job_is_duplicated(tmp_path, github, duplicate):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)],
                                    {_BUILD_RUN: _build_jobs() + [_job(duplicate)]})
    assert code != 0, out
    assert f"build.yml run {_BUILD_RUN} has 2 " in out
    assert _PYPI not in out


@pytest.mark.parametrize("job,conclusion", [
    ("openapi-asset", "failure"), ("setup-matrix", "cancelled"), ("prepare-release", "skipped"),
    (_WINDOWS, "failure"), (_MAC, "cancelled"),
])
def test_pypi_is_not_published_when_a_prerequisite_did_not_succeed(tmp_path, github, job, conclusion):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)],
                                    {_BUILD_RUN: _build_jobs({job: conclusion})})
    assert code != 0, out
    assert f"finished {conclusion}" in out
    assert _polls(github, "build.yml") == 1
    assert _PYPI not in out


@pytest.mark.parametrize("identity", [
    {"head_branch": "v9.9.8"}, {"head_branch": "develop"}, {"event": "pull_request"},
    {"head_sha": "0" * 40}, {"path": ".github/workflows/ci.yml"},
], ids=["other tag, same commit", "branch push", "pull request", "other commit", "other workflow"])
def test_pypi_is_never_published_on_the_strength_of_another_run(tmp_path, github, identity):
    other = _workflow_run("build.yml", _BUILD_RUN + 1, **identity)
    current = _workflow_run("build.yml", _BUILD_RUN, "completed", "failure")
    code, out, _ = _publish_to_pypi(tmp_path, github, [other, current],
                                    {_BUILD_RUN + 1: _build_jobs(), _BUILD_RUN: _build_jobs({_LINUX: "failure"})})
    assert code != 0, out
    assert f"{_LINUX} in build.yml run {_BUILD_RUN} finished failure" in out
    assert _PYPI not in out


def test_pypi_is_not_published_when_two_runs_share_the_tag_and_commit(tmp_path, github):
    old = _workflow_run("build.yml", _BUILD_RUN - 1)
    current = _workflow_run("build.yml", _BUILD_RUN, "completed", "failure")
    code, out, _ = _publish_to_pypi(tmp_path, github, [current, old],
                                    {_BUILD_RUN - 1: _build_jobs(), _BUILD_RUN: _build_jobs({_MAC: "failure"})})
    assert code != 0, out
    assert f"2 build.yml runs for v9.9.9 at {_SHA} ({_BUILD_RUN}, {_BUILD_RUN - 1})" in out
    assert _PYPI not in out


@pytest.mark.parametrize("earlier,current,published", [("success", "failure", False), ("failure", "success", True)])
def test_pypi_follows_the_current_attempt_of_a_rerun_build(tmp_path, github, earlier, current, published):
    run = _workflow_run("build.yml", _BUILD_RUN, "in_progress", None, attempt=2)
    latest = [j for j in _build_jobs() if j["name"] != _LINUX] + [_job(_LINUX, current, attempt=2)]
    _serve(github, "build.yml", [run], {_BUILD_RUN: _build_jobs({_LINUX: earlier}) + latest[-1:]}, "all")
    code, out, _ = _publish_to_pypi(tmp_path, github, [run], {_BUILD_RUN: latest})
    assert (code == 0) is published, out
    assert (_PYPI in out) is published


@pytest.mark.parametrize("path,status", [("workflows/build.yml/runs", 500), (f"runs/{_BUILD_RUN}/jobs?filter=latest", 403)])
def test_pypi_is_not_published_when_the_github_api_fails(tmp_path, github, path, status):
    _serve(github, "build.yml", [_workflow_run("build.yml", _BUILD_RUN)], {_BUILD_RUN: _build_jobs()}, "latest")
    github.pages[f"{_ACTIONS}/{path}"] = [(status, {"message": "Server Error"})]
    code, out, _ = _run_job(tmp_path, "publish.yml", "publish", GITHUB_API_URL=github.url)
    assert code != 0, out
    assert f"GitHub API returned HTTP {status}" in out
    assert _polls(github, "build.yml") == 1
    assert _PYPI not in out


def test_pypi_reads_every_page_of_runs_and_jobs(tmp_path, github):
    jobs = _build_jobs()
    _serve(github, "build.yml", [], {}, "latest")
    github.pages[f"{_ACTIONS}/workflows/build.yml/runs"] = [
        (200, {"total_count": 2, "workflow_runs": [_workflow_run("build.yml", 1, head_branch="develop")]}),
        (200, {"total_count": 2, "workflow_runs": [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)]})]
    github.pages[f"{_ACTIONS}/runs/{_BUILD_RUN}/jobs?filter=latest"] = [
        (200, {"total_count": len(jobs), "jobs": jobs[:4]}), (200, {"total_count": len(jobs), "jobs": jobs[4:]})]
    code, out, _ = _run_job(tmp_path, "publish.yml", "publish", GITHUB_API_URL=github.url)
    assert code == 0, out
    assert sum("page=2" in c for c in github.calls) == 2
    assert _PYPI in out


@pytest.mark.parametrize("runs", [[_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)], []],
                         ids=["still building", "not started"])
def test_pypi_is_not_published_while_the_desktop_builds_never_finish(tmp_path, github, runs):
    code, out, _ = _publish_to_pypi(tmp_path, github, runs, {_BUILD_RUN: _build_jobs({_MAC: None})})
    assert code != 0, out
    assert "did not finish within 4 hours" in out
    assert _polls(github, "build.yml") == 240
    assert _PYPI not in out


def test_the_github_release_waits_for_every_build_prerequisite():
    job = yaml.safe_load((_WORKFLOWS / "build.yml").read_text())["jobs"]["publish-release"]
    assert {"prepare-release", "setup-matrix", "build", "openapi-asset"} <= set(job["needs"])


def test_the_github_release_is_published_once_the_pypi_release_succeeded(tmp_path, github):
    code, out, calls = _publish_on_github(tmp_path, github, [_workflow_run("publish.yml", _PUBLISH_RUN)],
                                          {_PUBLISH_RUN: _publish_jobs()})
    assert code == 0, out
    assert any("PATCH" in c and "releases/7" in c for c in calls), calls


@pytest.mark.parametrize("publish,conclusion", [("failure", "failure"), ("absent", "success"), ("skipped", "failure")],
                         ids=["publish failed", "no publish job", "publish skipped"])
def test_a_pypi_release_that_did_not_succeed_leaves_the_github_release_a_draft(tmp_path, github, publish, conclusion):
    code, out, calls = _publish_on_github(tmp_path, github, [_workflow_run("publish.yml", _PUBLISH_RUN, "completed", conclusion)],
                                          {_PUBLISH_RUN: _publish_jobs(publish)})
    assert code != 0, out
    assert f"publish.yml run {_PUBLISH_RUN} finished without publishing to PyPI" in out
    assert not any("PATCH" in c for c in calls), calls


def test_a_pypi_release_that_never_finishes_leaves_the_github_release_a_draft(tmp_path, github):
    code, out, calls = _publish_on_github(tmp_path, github, [_workflow_run("publish.yml", _PUBLISH_RUN, "in_progress", None)],
                                          {_PUBLISH_RUN: _publish_jobs(None)})
    assert code != 0, out
    assert "did not finish within 90 minutes" in out
    assert _polls(github, "publish.yml") == 90
    assert not any("PATCH" in c for c in calls), calls


def test_only_the_github_release_is_retried_after_pypi_published(tmp_path, github):
    """A re-run of publish.yml cannot upload the same version again and fails; the
    earlier attempt's publication still lets the GitHub release be retried alone."""
    run = _workflow_run("publish.yml", _PUBLISH_RUN, "completed", "failure", attempt=2)
    jobs = _publish_jobs("success") + _publish_jobs("failure", attempt=2)
    code, out, calls = _publish_on_github(tmp_path, github, [run], {_PUBLISH_RUN: jobs})
    assert code == 0, out
    assert any("PATCH" in c and "releases/7" in c for c in calls), calls


@pytest.mark.parametrize("other", [
    _workflow_run("publish.yml", _PUBLISH_RUN + 1, head_branch="v9.9.8"),
    _workflow_run("publish.yml", _PUBLISH_RUN - 1),
], ids=["other tag, same commit", "older run of the same tag"])
def test_the_github_release_is_never_published_on_the_strength_of_another_pypi_run(tmp_path, github, other):
    current = _workflow_run("publish.yml", _PUBLISH_RUN, "completed", "failure")
    code, out, calls = _publish_on_github(tmp_path, github, [current, other],
                                          {other["id"]: _publish_jobs(), _PUBLISH_RUN: _publish_jobs("failure")})
    assert code != 0, out
    assert "ERROR:" in out
    assert not any("PATCH" in c for c in calls), calls


def test_the_github_release_stays_a_draft_when_the_github_api_fails(tmp_path, github):
    github.pages[f"{_ACTIONS}/workflows/publish.yml/runs"] = [(502, {"message": "Bad Gateway"})]
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release", GITHUB_API_URL=github.url)
    assert code != 0, out
    assert "GitHub API returned HTTP 502" in out
    assert not any("PATCH" in c for c in calls), calls


def test_the_github_release_reads_every_page_of_jobs(tmp_path, github):
    jobs = _publish_jobs()
    _serve(github, "publish.yml", [_workflow_run("publish.yml", _PUBLISH_RUN)], {}, "all")
    github.pages[f"{_ACTIONS}/runs/{_PUBLISH_RUN}/jobs?filter=all"] = [
        (200, {"total_count": len(jobs), "jobs": jobs[:-1]}), (200, {"total_count": len(jobs), "jobs": jobs[-1:]})]
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release", GITHUB_API_URL=github.url)
    assert code == 0, out
    assert any("PATCH" in c and "releases/7" in c for c in calls), calls
