# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The release workflows' own step scripts, run with the GitHub API stubbed.

PyPI cannot take a release back, so it is published only after every desktop
build of the same tag and commit succeeded and those Linux and Windows binaries
passed the packaged upgrade test, the GitHub release only after the PyPI release
succeeded, and a version already released is never rebuilt. The
GitHub API is a local server answering with the shapes and job names GitHub
returns for a real release.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
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


def _run_job(tmp_path: Path, workflow: str, job: str, polls: int | None = None,
             **env: str) -> tuple[int, str, list[str]]:
    """Runs the job's steps in order as GitHub does, stopping at the first failure. An
    action step is only recorded as reached. `polls` shortens the PyPI wait loop so
    running it out stays fast; its real length is checked from the workflow alone."""
    base = _env(tmp_path, **env)
    out, code = "", 0
    for step in _steps(workflow, job):
        if "uses" in step:
            out += f"reached {step['uses']}\n"
            continue
        script = step["run"]
        if polls is not None and _PYPI_LOOP in script:
            script = script.replace(_PYPI_LOOP, f"$(seq {polls})")
        r = _run(tmp_path, script, {**base, **step.get("env", {}), **env, "GH_TOKEN": "t"})
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


def test_pypi_publishes_from_its_own_tag_push_workflow():
    """PyPI's trusted publisher names publish.yml and its environment: the upload
    must stay a job of publish.yml started by the tag push, never a workflow
    another one calls or starts."""
    wf = yaml.safe_load((_WORKFLOWS / "publish.yml").read_text())
    assert wf[True] == {"push": {"tags": ["v*"]}, "workflow_dispatch": {"inputs": {"version": {
        "description": "Version to build, for example 2.5.4", "required": True}}}}
    job = wf["jobs"]["publish"]
    assert job["environment"] == "pypi" and job["permissions"]["id-token"] == "write"
    assert job["steps"][-1] == {"name": "Publish to PyPI", "uses": "pypa/gh-action-pypi-publish@release/v1"}
    build = yaml.safe_load((_WORKFLOWS / "build.yml").read_text())["jobs"]
    assert not any("publish.yml" in str(job.get("uses", "")) for job in build.values())




_PYPI = "reached pypa/gh-action-pypi-publish"
_PYPI_POLLS = 340  # a minute apart, inside GitHub's 6-hour job limit
_PYPI_LOOP = f"$(seq {_PYPI_POLLS})"
_TEST_POLLS = 3
_REPO = "/repos/celerp/celerp"
_ACTIONS = f"{_REPO}/actions"
_BUILD_RUN, _PUBLISH_RUN = 36595157056, 36595157121


def _matrix_job_names() -> dict[str, str]:
    """The desktop build job names GitHub derives from setup-matrix's tag matrix."""
    run = next(s for s in _steps("build.yml", "setup-matrix") if s.get("id") == "gen")["run"]
    matrix = [json.loads(m) for m in re.findall(r"^ *[a-z]+='(\{.*\})'$", run, re.M)]
    return {m["os"]: f"build ({', '.join(m.values())})" for m in matrix}


_BUILD = _matrix_job_names()
_LINUX, _WINDOWS, _MAC = _BUILD["ubuntu-latest"], _BUILD["windows-latest"], _BUILD["macos-latest"]
# build.yml calls packaged-upgrade-smoke.yml as its upgrade-smoke job; GitHub names
# a called workflow's jobs "<calling job> / <job>".
_UPGRADE_CHECK = "upgrade-smoke / candidate"
_UPGRADE_LINUX, _UPGRADE_WINDOWS = "upgrade-smoke / upgrade (ubuntu-latest)", "upgrade-smoke / upgrade (windows-latest)"


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
    """A run as GitHub lists it, by default the push of tag v9.9.9 at _SHA, its
    current attempt started 21 minutes ago."""
    name = {"build.yml": "Build Binaries", "publish.yml": "Publish to PyPI"}[workflow]
    return {"id": run_id, "name": name, "path": f".github/workflows/{workflow}", "head_branch": "v9.9.9",
            "head_sha": _SHA, "event": "push", "status": status, "conclusion": conclusion,
            "run_attempt": attempt, "run_number": 2043, "run_started_at": _ago(21), **identity}


def _ago(minutes: float) -> str:
    """A GitHub timestamp the given number of minutes before now."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(name: str, conclusion: str | None = "success", attempt: int = 1, started: float = 20) -> dict:
    """A job as GET /actions/runs/<id>/jobs lists it, started the given minutes ago."""
    return {"id": abs(hash((name, attempt))), "name": name, "head_sha": _SHA, "run_attempt": attempt,
            "status": "in_progress" if conclusion is None else "completed", "conclusion": conclusion,
            "created_at": _ago(started + 1), "started_at": _ago(started)}


def _build_jobs(conclusions: dict[str, str | None] | None = None) -> list[dict]:
    """build.yml's jobs for a tag in GitHub's order, each succeeded unless given by
    name; "absent" leaves a job out. The release job is still waiting for PyPI."""
    names = ["setup-matrix", "prepare-release", _LINUX, _WINDOWS, _MAC, "openapi-asset",
             _UPGRADE_CHECK, _UPGRADE_LINUX, _UPGRADE_WINDOWS]
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
    return _run_job(tmp_path, "publish.yml", "publish", polls=_TEST_POLLS, GITHUB_API_URL=github.url)


def _release(tag: str, draft: bool = False, prerelease: bool = False) -> dict:
    """A release as GET /releases lists it, the fields read."""
    return {"id": abs(hash(tag)), "tag_name": tag, "draft": draft, "prerelease": prerelease}


# GitHub lists releases newest first; an authorised token sees drafts too.
_RELEASES = [_release("v9.9.9", draft=True), _release("dev-latest", prerelease=True), _release("v9.9.8"),
             _release("v9.9.10", draft=True)]


def _publish_on_github(tmp_path, github, runs: list[dict], jobs: dict[int, list[dict]],
                       releases: list[dict] = _RELEASES):
    _serve(github, "publish.yml", runs, jobs, "all")
    github.pages[f"{_REPO}/releases"] = [(200, releases)]
    return _run_job(tmp_path, "build.yml", "publish-release", GITHUB_API_URL=github.url)


def _polls(github, workflow: str) -> int:
    return sum(c.startswith(f"{_ACTIONS}/workflows/{workflow}/runs?") for c in github.calls)


def test_the_desktop_build_names_are_the_ones_github_gives_the_tag_matrix():
    assert _LINUX == "build (ubuntu-latest, pnpm run build:linux, --linux, linux-x64, x86_64-unknown-linux-gnu)"
    assert set(_BUILD) == {"ubuntu-latest", "windows-latest", "macos-latest"}


@pytest.mark.parametrize("ref", ["", "@refs/tags/v9.9.9"], ids=["bare path", "path with ref"])
def test_pypi_is_published_once_every_desktop_build_and_the_schema_succeeded(tmp_path, github, ref):
    run = _workflow_run("build.yml", _BUILD_RUN, "in_progress", None, path=f".github/workflows/build.yml{ref}")
    code, out, _ = _publish_to_pypi(tmp_path, github, [run],
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
    (_WINDOWS, "failure"), (_MAC, "cancelled"), (_UPGRADE_LINUX, "failure"), (_UPGRADE_WINDOWS, "skipped"),
])
def test_pypi_is_not_published_when_a_prerequisite_did_not_succeed(tmp_path, github, job, conclusion):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)],
                                    {_BUILD_RUN: _build_jobs({job: conclusion})})
    assert code != 0, out
    assert f"finished {conclusion}" in out
    assert _polls(github, "build.yml") == 1
    assert _PYPI not in out


@pytest.mark.parametrize("absent", [_UPGRADE_LINUX, _UPGRADE_WINDOWS])
def test_pypi_is_not_published_when_a_finished_run_has_no_upgrade_test(tmp_path, github, absent):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "completed", "failure")],
                                    {_BUILD_RUN: _build_jobs({absent: "absent"})})
    assert code != 0, out
    assert f"build.yml run {_BUILD_RUN} finished without {absent.split(' / ')[1]}" in out
    assert _PYPI not in out


def test_pypi_waits_for_the_upgrade_test_after_the_builds(tmp_path, github):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)],
                                    {_BUILD_RUN: _build_jobs({_UPGRADE_LINUX: None, _UPGRADE_WINDOWS: None})})
    assert code != 0, out
    assert "waiting for upgrade (ubuntu-latest), upgrade (windows-latest)" in out
    assert _polls(github, "build.yml") == _TEST_POLLS
    assert _PYPI not in out


@pytest.mark.parametrize("duplicate", ["upgrade (ubuntu-latest)", "other / upgrade (ubuntu-latest)"])
def test_pypi_is_not_published_when_an_upgrade_test_is_duplicated(tmp_path, github, duplicate):
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None)],
                                    {_BUILD_RUN: _build_jobs() + [_job(duplicate)]})
    assert code != 0, out
    assert f"build.yml run {_BUILD_RUN} has 2 upgrade (ubuntu-latest) jobs" in out
    assert _PYPI not in out


@pytest.mark.parametrize("identity", [
    {"head_branch": "v9.9.8"}, {"head_branch": "develop"}, {"event": "pull_request"},
    {"head_sha": "0" * 40}, {"path": ".github/workflows/ci.yml"}, {"path": ".github/workflows/ci.yml@refs/tags/v9.9.9"},
], ids=["other tag, same commit", "branch push", "pull request", "other commit", "other workflow",
        "other workflow with ref"])
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
    assert "did not finish within 340 minutes. Once they finish, re-run the failed publish job" in out
    assert _polls(github, "build.yml") == _TEST_POLLS
    assert _PYPI not in out


def test_the_pypi_wait_polls_every_minute_for_as_long_as_github_lets_the_job_run():
    job = yaml.safe_load((_WORKFLOWS / "publish.yml").read_text())["jobs"]["publish"]
    wait = next(s for s in job["steps"] if s.get("name") == "Wait for the desktop builds and upgrade test of this commit")
    assert wait["run"].count(_PYPI_LOOP) == 1
    assert "3) sleep 60 ;;" in wait["run"]
    assert f"did not finish within {_PYPI_POLLS} minutes" in wait["run"]
    assert job["timeout-minutes"] == 360
    assert _PYPI_POLLS < job["timeout-minutes"]


def test_pypi_is_not_published_when_the_build_run_was_building_for_more_than_4_hours(tmp_path, github):
    """Counted from when the tag's build run started building, not from the tag push."""
    jobs = [{**j, "started_at": _ago(250)} for j in _build_jobs({_MAC: None})]
    code, out, _ = _publish_to_pypi(tmp_path, github, [_workflow_run("build.yml", _BUILD_RUN, "in_progress", None,
                                                                      run_started_at=_ago(251))], {_BUILD_RUN: jobs})
    assert code != 0, out
    assert f"build.yml run {_BUILD_RUN} started building at {jobs[0]['started_at']} and has not finished " in out
    assert "within 4 hours. Once it finishes, re-run the failed publish job" in out
    assert _polls(github, "build.yml") == 1
    assert _PYPI not in out


@pytest.mark.parametrize("queued", ["created long ago, jobs waiting for runners", "re-run started recently"])
def test_pypi_waits_while_the_build_run_has_not_been_building_for_4_hours(tmp_path, github, queued):
    """A tag's build run created hours ago behind other runs, or one whose failed
    jobs were re-run (the jobs it kept started hours ago), is not out of time."""
    if queued.startswith("created"):
        run = _workflow_run("build.yml", _BUILD_RUN, "queued", None, run_started_at=_ago(300))
        jobs = [{**j, "status": "queued", "conclusion": None, "created_at": _ago(300), "started_at": _ago(300)}
                for j in _build_jobs()]
    else:
        run = _workflow_run("build.yml", _BUILD_RUN, "in_progress", None, attempt=2, run_started_at=_ago(10))
        jobs = [{**j, "started_at": _ago(300)} for j in _build_jobs({_MAC: "absent"})] + [_job(_MAC, None, 2, 9)]
    code, out, _ = _publish_to_pypi(tmp_path, github, [run], {_BUILD_RUN: jobs})
    assert code != 0, out
    assert "within 4 hours" not in out
    assert _polls(github, "build.yml") == _TEST_POLLS
    assert _PYPI not in out


def test_the_github_release_waits_for_every_build_prerequisite():
    jobs = yaml.safe_load((_WORKFLOWS / "build.yml").read_text())["jobs"]
    assert {"prepare-release", "setup-matrix", "build", "openapi-asset", "upgrade-smoke"} <= set(
        jobs["publish-release"]["needs"])
    # A failed upgrade test leaves the release a draft and opens the release-failure issue.
    assert "upgrade-smoke" in jobs["notify-release-failure"]["needs"]


def test_a_tag_build_upgrade_tests_its_own_linux_and_windows_binaries():
    wf = yaml.safe_load((_WORKFLOWS / "build.yml").read_text())
    smoke = wf["jobs"]["upgrade-smoke"]
    assert smoke == {
        "needs": ["build"],
        "if": "${{ success() && startsWith(github.ref, 'refs/tags/v') }}",
        "permissions": {"actions": "read", "contents": "read"},
        "uses": "./.github/workflows/packaged-upgrade-smoke.yml",
        "with": {"candidate_run": "${{ github.run_id }}"},
    }
    # Tag builds keep their binaries as workflow artifacts too, for the upgrade test.
    upload = next(s for s in wf["jobs"]["build"]["steps"] if s.get("name") == "Upload artifacts")
    assert "if" not in upload and upload["with"]["name"] == "binaries-${{ matrix.os }}"
    assert "schedule" not in wf[True]


@pytest.mark.parametrize("ref", ["", "@refs/tags/v9.9.9"], ids=["bare path", "path with ref"])
def test_the_github_release_is_published_once_the_pypi_release_succeeded(tmp_path, github, ref):
    run = _workflow_run("publish.yml", _PUBLISH_RUN, path=f".github/workflows/publish.yml{ref}")
    code, out, calls = _publish_on_github(tmp_path, github, [run],
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
    github.pages[f"{_REPO}/releases"] = [(200, _RELEASES)]
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release", GITHUB_API_URL=github.url)
    assert code == 0, out
    assert any("PATCH" in c and "releases/7" in c for c in calls), calls


def _published(calls: list[str]) -> str:
    """The release update the publish step sent."""
    patch = next(c for c in calls if "PATCH" in c and "releases/7" in c)
    return json.loads(patch.split(" -d ", 1)[1])["make_latest"]


def test_the_github_release_is_marked_latest_when_no_higher_version_is_published(tmp_path, github):
    """Drafts, prereleases and lower versions never hold the release back: v9.9.10
    is only a draft, and a higher number compares by value, not as text."""
    code, out, calls = _publish_on_github(tmp_path, github, [_workflow_run("publish.yml", _PUBLISH_RUN)],
                                          {_PUBLISH_RUN: _publish_jobs()})
    assert code == 0, out
    assert _published(calls) == "true"


@pytest.mark.parametrize("higher", ["v9.9.10", "v9.10.0", "v10.0.0"])
def test_the_github_release_is_not_marked_latest_over_a_higher_published_version(tmp_path, github, higher):
    """A tag pushed after a higher version shipped (a fix to an older line) is
    published, as PyPI already has it, but latest stays on the higher version."""
    code, out, calls = _publish_on_github(tmp_path, github, [_workflow_run("publish.yml", _PUBLISH_RUN)],
                                          {_PUBLISH_RUN: _publish_jobs()}, releases=_RELEASES + [_release(higher)])
    assert code == 0, out
    assert _published(calls) == "false"
    assert f"{higher} is already published" in out


def test_the_github_release_stays_a_draft_when_the_releases_cannot_be_read(tmp_path, github):
    _serve(github, "publish.yml", [_workflow_run("publish.yml", _PUBLISH_RUN)], {_PUBLISH_RUN: _publish_jobs()}, "all")
    github.pages[f"{_REPO}/releases"] = [(200, [_release("v9.9.8")]), (502, {"message": "Bad Gateway"})]
    code, out, calls = _run_job(tmp_path, "build.yml", "publish-release", GITHUB_API_URL=github.url)
    assert code != 0, out
    assert "GitHub API returned HTTP 502" in out
    assert not any("PATCH" in c for c in calls), calls


def test_tag_releases_mark_latest_one_at_a_time():
    """Two tags pushed together build and publish to PyPI side by side; only their
    GitHub releases queue, one at a time in the order they reached the queue, so
    the higher-version check and the update that marks latest never interleave.
    Every waiting tag stays queued, none is cancelled. publish.yml queues nothing:
    one tag's PyPI release never waits on another tag, as the GitHub release of a
    tag waits on its own PyPI release. Pull requests still cancel their earlier runs."""
    build = yaml.safe_load((_WORKFLOWS / "build.yml").read_text())
    publish = yaml.safe_load((_WORKFLOWS / "publish.yml").read_text())
    assert build["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": True}
    assert build["jobs"]["publish-release"]["concurrency"] == {
        "group": "github-release", "cancel-in-progress": False, "queue": "max"}
    assert ["publish-release"] == [name for name, job in build["jobs"].items() if "concurrency" in job]
    assert "concurrency" not in publish
    assert not any("concurrency" in job for job in publish["jobs"].values())


# ---------------------------------------------------------------------------
# The build matrix and the candidate the packaged upgrade check installs
# ---------------------------------------------------------------------------

def _matrix(tmp_path: Path, event: str, platforms: str = "", ref_name: str = "main") -> list[str]:
    step = next(s for s in _steps("build.yml", "setup-matrix") if s.get("id") == "gen")
    out = tmp_path / "github_output"
    r = _run(tmp_path, step["run"], _env(tmp_path, GITHUB_EVENT_NAME=event, GITHUB_REF_NAME=ref_name,
                                         PLATFORMS=platforms, GITHUB_OUTPUT=str(out)))
    assert r.returncode == 0, r.stdout + r.stderr
    line = out.read_text().strip()
    return [m["os"] for m in json.loads(line.removeprefix("matrix="))["include"]]


@pytest.mark.parametrize("event,platforms,ref_name,built", [
    ("workflow_dispatch", "all", "main", ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("workflow_dispatch", "linux", "main", ["ubuntu-latest"]),
    ("pull_request", "", "main", ["macos-latest"]),
    ("push", "", "bugfix", ["macos-latest"]),
    ("push", "", "v9.9.9", ["ubuntu-latest", "windows-latest", "macos-latest"]),
])
def test_each_event_builds_its_platforms(tmp_path, event, platforms, ref_name, built):
    assert _matrix(tmp_path, event, platforms, ref_name) == built


_CANDIDATE = 37700000001
_HEAD, _OLD, _TAG_OBJECT = "a" * 40, "b" * 40, "d" * 40
# GET /compare/<base>...<head>, the fields read, as GitHub answers them (v2.5.3
# against main, bugfix, develop and itself).
_AHEAD = {"status": "ahead", "ahead_by": 14, "behind_by": 0, "total_commits": 14}
_DIVERGED = {"status": "diverged", "ahead_by": 1, "behind_by": 98, "total_commits": 1}
_BEHIND = {"status": "behind", "ahead_by": 0, "behind_by": 19, "total_commits": 0}
_IDENTICAL = {"status": "identical", "ahead_by": 0, "behind_by": 0, "total_commits": 0}


def _candidate_run(**fields) -> dict:
    """A finished manual Build Binaries run of develop, as GET /actions/runs/<id> answers."""
    return {"id": _CANDIDATE, "name": "Build Binaries", "path": ".github/workflows/build.yml", "head_branch": "develop",
            "head_sha": _HEAD, "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
            "run_attempt": 1, **fields}


def _release_run(**fields) -> dict:
    """The Build Binaries run of tag v2.5.5, still running: the upgrade test is one of its jobs."""
    return _candidate_run(**{"head_branch": "v2.5.5", "event": "push", "status": "in_progress", "conclusion": None,
                             **fields})


def _artifact(name: str, **fields) -> dict:
    return {"name": name, "expired": False, "size_in_bytes": 400_000_000, **fields}


def _tag_ref(name: str, sha: str = _HEAD, kind: str = "commit") -> dict:
    return {"ref": f"refs/tags/{name}", "object": {"type": kind, "sha": sha}}


def _branch_ref(name: str, sha: str = _HEAD) -> dict:
    return {"ref": f"refs/heads/{name}", "object": {"type": "commit", "sha": sha}}


def _serve_candidate(github, run: dict, artifacts: list[dict] | None = None, tags: list[dict] | None = None,
                     jobs: list[dict] | None = None, latest: str = "v2.5.4", lineage: dict = _AHEAD) -> None:
    if artifacts is None:
        artifacts = [_artifact("binaries-ubuntu-latest"), _artifact("binaries-windows-latest")]
    if tags is None:
        # A prefix match: v2.5.5 lists v2.5.50 too, and a branch build finds no tag of its name.
        tags = [_tag_ref("v2.5.5"), _tag_ref("v2.5.50", _OLD)]
    if jobs is None:
        # The release run's Linux and Windows builds are done; macOS is still building.
        jobs = [_job("setup-matrix"), _job("prepare-release"), _job(_LINUX), _job(_WINDOWS), _job(_MAC, None)]
    github.pages[f"{_REPO}/actions/runs/{_CANDIDATE}"] = [(200, run)]
    github.pages[f"{_REPO}/actions/runs/{_CANDIDATE}/artifacts"] = [
        (200, {"total_count": len(artifacts), "artifacts": artifacts})]
    github.pages[f"{_REPO}/actions/runs/{_CANDIDATE}/jobs?filter=latest"] = [
        (200, {"total_count": len(jobs), "jobs": jobs})]
    github.pages[f"{_REPO}/git/matching-refs/tags/v2.5.5"] = [(200, tags)]
    github.pages[f"{_REPO}/git/matching-refs/tags/develop"] = [(200, [])]
    # A prefix match, as for tags: develop lists develop-old too.
    github.pages[f"{_REPO}/git/matching-refs/heads/develop"] = [
        (200, [_branch_ref("develop"), _branch_ref("develop-old", _OLD)])]
    github.pages[f"{_REPO}/git/matching-refs/heads/v2.5.5"] = [(200, [])]
    github.pages[f"{_REPO}/git/tags/{_TAG_OBJECT}"] = [(200, {"sha": _TAG_OBJECT, "object": {"type": "commit", "sha": _HEAD}})]
    github.pages[f"{_REPO}/releases/latest"] = [(200, {"tag_name": latest})]
    github.pages[f"{_REPO}/releases/tags/v2.5.3"] = [(200, {"tag_name": "v2.5.3"})]
    for previous in (latest, "v2.5.3"):
        github.pages[f"{_REPO}/compare/{previous}...{_HEAD}"] = [(200, lineage)]
    github.pages[f"{_REPO}/compare/{_HEAD}...main"] = [(200, _DIVERGED)]


def _check(tmp_path, github, *args: str, **env: str) -> tuple[int, str, str]:
    out = tmp_path / "github_output"
    r = subprocess.run(["python3", "scripts/upgrade_candidate.py", "check", *args], capture_output=True, text=True,
                       cwd=_WORKFLOWS.parent.parent,
                       env=_env(tmp_path, **{"GITHUB_API_URL": github.url, "GITHUB_OUTPUT": str(out),
                                             "GITHUB_RUN_ID": "1", "GUARDED_SINCE": "2.5.4", "PREVIOUS": "", **env}))
    return r.returncode, r.stdout + r.stderr, out.read_text() if out.exists() else ""


@pytest.mark.parametrize("tag", [_tag_ref("v2.5.5"), _tag_ref("v2.5.5", _TAG_OBJECT, "tag")],
                         ids=["lightweight tag", "annotated tag"])
def test_a_release_build_is_the_candidate_of_the_upgrade_test_it_runs(tmp_path, github, tag):
    """Called from the tag's own run: the run is still in progress, its Linux and
    Windows builds succeeded, and the tag points at the commit it built. The
    previous release is the latest published one; the draft of this tag is not."""
    _serve_candidate(github, _release_run(), tags=[tag])
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=str(_CANDIDATE))
    assert code == 0, out
    assert outputs.splitlines() == [f"run={_CANDIDATE}", f"sha={_HEAD}", "previous=v2.5.4", "previous_version=2.5.4",
                                    "guarded=true", "ref=v2.5.5"]
    assert f"{_REPO}/releases/latest" in github.calls


@pytest.mark.parametrize("conclusions,message", [
    ({_WINDOWS: "failure"}, f"{_WINDOWS} in build run {_CANDIDATE} finished failure"),
    ({_LINUX: None}, f"build run {_CANDIDATE} has not finished build (ubuntu-latest)"),
    ({_LINUX: "absent"}, f"build run {_CANDIDATE} has not finished build (ubuntu-latest)"),
    ({"duplicate": _WINDOWS}, f"build run {_CANDIDATE} has 2 build (windows-latest) jobs"),
], ids=["windows failed", "linux running", "linux absent", "windows twice"])
def test_a_release_build_without_both_binaries_built_fails_the_check(tmp_path, github, conclusions, message):
    jobs = [_job(_MAC, None)]
    for name in (_LINUX, _WINDOWS):
        if conclusions.get(name) != "absent":
            jobs.append(_job(name, conclusions.get(name, "success")))
    if "duplicate" in conclusions:
        jobs.append(_job(conclusions["duplicate"], "success", attempt=2))
    _serve_candidate(github, _release_run(), jobs=jobs)
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=str(_CANDIDATE))
    assert code == 1, out
    assert message in out
    assert outputs == ""


@pytest.mark.parametrize("rerun,passes,message", [
    ("success", True, ""),
    (None, False, f"build run {_CANDIDATE} has not finished build (windows-latest)"),
    ("failure", False, f"{_WINDOWS} in build run {_CANDIDATE} finished failure"),
], ids=["re-run succeeded", "re-run building", "re-run failed"])
def test_a_rerun_release_build_is_judged_on_the_rerun_of_its_failed_build(tmp_path, github, rerun, passes, message):
    """Windows failed in attempt 1 and was re-run alone: GitHub's latest filter lists
    each job of attempt 2 once, carrying the jobs that had succeeded; the full list
    holds both Windows builds."""
    first = [_job(n) for n in ("setup-matrix", "prepare-release", _LINUX, _MAC)] + [_job(_WINDOWS, "failure")]
    second = [_job(n, attempt=2) for n in ("setup-matrix", "prepare-release", _LINUX, _MAC)]
    second.append(_job(_WINDOWS, rerun, attempt=2))
    _serve_candidate(github, _release_run(run_attempt=2), jobs=second)
    github.pages[f"{_REPO}/actions/runs/{_CANDIDATE}/jobs?filter=all"] = [
        (200, {"total_count": len(first) + len(second), "jobs": first + second})]
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=str(_CANDIDATE))
    assert (code == 0) is passes, out
    assert message in out
    assert f"{_REPO}/actions/runs/{_CANDIDATE}/jobs?filter=latest&per_page=100" in github.calls
    assert not any("filter=all" in c for c in github.calls)


def test_a_release_build_whose_tag_points_elsewhere_fails_the_check(tmp_path, github):
    _serve_candidate(github, _release_run(), tags=[_tag_ref("v2.5.5", _OLD)])
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=str(_CANDIDATE))
    assert code == 1, out
    assert f"build run {_CANDIDATE} built {_HEAD}, but tag v2.5.5 points at {_OLD}" in out
    assert outputs == ""


@pytest.mark.parametrize("run,current", [(_release_run(), str(_CANDIDATE)), (_candidate_run(head_branch="v2.5.5"), "1")],
                         ids=["release build", "finished build"])
def test_a_build_of_a_tag_deleted_since_fails_the_check(tmp_path, github, run, current):
    """The tag was deleted after its run started: a ref that is neither a tag nor
    a branch is refused, never taken as a branch build."""
    _serve_candidate(github, run, tags=[_tag_ref("v2.5.50", _OLD)])
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=current)
    assert code == 1, out
    assert f"::error::build run {_CANDIDATE} built v2.5.5, which is now neither a tag nor a branch" in out
    assert outputs == ""


def test_a_build_of_a_branch_deleted_since_fails_the_check(tmp_path, github):
    _serve_candidate(github, _candidate_run())
    github.pages[f"{_REPO}/git/matching-refs/heads/develop"] = [(200, [_branch_ref("develop-old", _OLD)])]
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE))
    assert code == 1, out
    assert f"build run {_CANDIDATE} built develop, which is now neither a tag nor a branch" in out


def test_a_manual_candidate_is_the_commit_its_run_built(tmp_path, github):
    """A run's head_sha is the commit it built, kept by every re-run, so a branch
    build is taken at that commit wherever the branch has moved since; only the
    previous release must come before it."""
    _serve_candidate(github, _candidate_run())
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), PREVIOUS="v2.5.3")
    assert code == 0, out
    assert outputs.splitlines() == [f"run={_CANDIDATE}", f"sha={_HEAD}", "previous=v2.5.3", "previous_version=2.5.3",
                                    "guarded=false", "ref=develop"]
    assert f"commit {_HEAD} is 14 commits after v2.5.3 and 1 commits behind main" in out
    assert not any(c.startswith(f"{_REPO}/activity") for c in github.calls)


def test_a_manual_candidate_may_be_a_finished_release_build(tmp_path, github):
    _serve_candidate(github, _release_run(status="completed", conclusion="success"))
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE))
    assert code == 0, out
    assert "ref=v2.5.5" in outputs


@pytest.mark.parametrize("lineage", [_DIVERGED, _BEHIND, _IDENTICAL], ids=["diverged", "behind", "identical"])
@pytest.mark.parametrize("run,current", [(_candidate_run(), "1"), (_release_run(), str(_CANDIDATE))],
                         ids=["branch build", "release build"])
def test_a_build_not_made_after_the_previous_release_fails_the_check(tmp_path, github, lineage, run, current):
    """A higher version number is not enough: the commit must contain the previous
    release and add to it, or the upgrade would test a side branch or the release itself."""
    _serve_candidate(github, run, lineage=lineage)
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=current)
    assert code == 1, out
    assert (f"::error::build run {_CANDIDATE} built {_HEAD}, which is {lineage['status']} v2.5.4, "
            "not a commit after it") in out
    assert outputs == ""


@pytest.mark.parametrize("run,message", [
    (_candidate_run(conclusion="failure"), "conclusion failure; only a successful build is a candidate"),
    (_candidate_run(conclusion="cancelled"), "conclusion cancelled"),
    (_candidate_run(status="in_progress", conclusion=None),
     "is in_progress; only a finished build, or the release build this check runs in, is a candidate"),
    (_release_run(), "is in_progress; only a finished build, or the release build this check runs in"),
    (_candidate_run(path=".github/workflows/ci.yml"), "not Build Binaries"),
], ids=["build failed", "build cancelled", "build running", "another release build running", "other workflow"])
def test_a_build_that_did_not_succeed_fails_the_check(tmp_path, github, run, message):
    _serve_candidate(github, run)
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE))
    assert code == 1, out
    assert "::error::" in out and message in out
    assert outputs == ""


@pytest.mark.parametrize("artifacts,missing", [
    ([_artifact("binaries-ubuntu-latest")], "binaries-windows-latest"),
    ([_artifact("binaries-windows-latest")], "binaries-ubuntu-latest"),
    ([_artifact("binaries-ubuntu-latest", expired=True), _artifact("binaries-windows-latest")], "binaries-ubuntu-latest"),
    ([_artifact("binaries-ubuntu-latest"), _artifact("binaries-windows-latest", size_in_bytes=0)],
     "binaries-windows-latest"),
    ([], "binaries-ubuntu-latest, binaries-windows-latest"),
], ids=["no windows", "no linux", "linux expired", "windows empty", "none"])
@pytest.mark.parametrize("run,current", [(_candidate_run(), "1"), (_release_run(), str(_CANDIDATE))],
                         ids=["branch build", "release build"])
def test_a_build_missing_either_platform_fails_the_check(tmp_path, github, artifacts, missing, run, current):
    _serve_candidate(github, run, artifacts=artifacts)
    code, out, _ = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=current)
    assert code == 1, out
    assert f"build run {_CANDIDATE} carries no {missing}" in out


@pytest.mark.parametrize("path,run,current", [
    (f"actions/runs/{_CANDIDATE}", _candidate_run(), "1"),
    (f"actions/runs/{_CANDIDATE}/artifacts", _candidate_run(), "1"),
    ("releases/latest", _candidate_run(), "1"),
    (f"compare/v2.5.4...{_HEAD}", _candidate_run(), "1"),
    (f"compare/{_HEAD}...main", _candidate_run(), "1"),
    ("git/matching-refs/tags/develop", _candidate_run(), "1"),
    ("git/matching-refs/heads/develop", _candidate_run(), "1"),
    ("git/matching-refs/tags/v2.5.5", _release_run(), str(_CANDIDATE)),
    (f"actions/runs/{_CANDIDATE}/jobs?filter=latest", _release_run(), str(_CANDIDATE)),
])
def test_the_check_fails_when_the_github_api_fails(tmp_path, github, path, run, current):
    _serve_candidate(github, run)
    github.pages[f"{_REPO}/{path}"] = [(502, {"message": "Bad Gateway"})]
    code, out, outputs = _check(tmp_path, github, str(_CANDIDATE), GITHUB_RUN_ID=current)
    assert code == 1, out
    assert "GitHub API returned HTTP 502" in out
    assert outputs == ""


@pytest.mark.parametrize("run_id", ["", "latest", "1; echo x"])
def test_the_check_refuses_a_malformed_run_id(tmp_path, github, run_id):
    code, out, _ = _check(tmp_path, github, run_id)
    assert code == 1, out
    assert "is not a run id" in out
    assert github.calls == []


@pytest.mark.parametrize("installed,previous,is_newer", [
    ("2.5.5-dev.3+g1a2b3c4", "2.5.4", True),
    ("2.6.0", "v2.5.9", True),
    ("2.5.10-dev.1+g1a2b3c4", "2.5.9", True),
    ("2.5.4", "2.5.4", False),
    ("2.5.4-dev.3+g1a2b3c4", "2.5.4", False),  # built before the release it carries the number of
    ("2.4.1-dev.12+g1a2b3c4", "2.5.3", False),
    ("not-a-version", "2.5.3", False),
    ("", "2.5.3", False),
])
def test_the_installed_candidate_must_be_newer_than_the_previous_release(installed, previous, is_newer):
    r = subprocess.run(["python3", "scripts/upgrade_candidate.py", "newer", installed, previous],
                       capture_output=True, text=True, cwd=_WORKFLOWS.parent.parent)
    assert (r.returncode == 0) is is_newer, r.stdout + r.stderr
    if not is_newer:
        assert "::error::" in r.stdout
