# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Whether a tagged release may be published, read from GitHub Actions.

PyPI cannot take a release back, so it is published only once build.yml's run for
the same tag and commit built every desktop platform, passed the packaged upgrade
test on its Linux and Windows binaries and attached the API schema; the GitHub
release only once publish.yml's run for that tag and commit published to PyPI. A run counts only if its workflow, tag, commit and event all match, and
only when exactly one does.

    python scripts/release_gate.py desktop-builds   # in publish.yml, before PyPI
    python scripts/release_gate.py pypi-release     # in build.yml, before GitHub
    python scripts/release_gate.py make-latest      # in build.yml, publishing on GitHub

make-latest prints whether the tag's GitHub release becomes the latest one:
false when a published release (not a draft or prerelease) has a higher X.Y.Z,
so a fix to an older line never takes latest from a newer version.

Reads GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_REF_NAME, GITHUB_SHA and GH_TOKEN.
Exit status: 0 ready, 3 not finished yet, 1 never ready (any API error included).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

PLATFORMS = ("ubuntu-latest", "windows-latest", "macos-latest")
BUILD_PREREQUISITES = ("prepare-release", "setup-matrix", "openapi-asset")
# packaged-upgrade-smoke.yml's jobs, called from build.yml. GitHub names a called
# workflow's jobs "<calling job> / <job>", so they are matched on the last part.
UPGRADE_TESTS = ("upgrade (ubuntu-latest)", "upgrade (windows-latest)")
PENDING = 3
# How long the desktop builds may take, counted from when the tag's build run
# started building, so time spent queued for runners or behind other runs is free.
BUILD_TIME = timedelta(hours=4)
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+].*)?")


class NotReady(Exception):
    pass


class Refused(Exception):
    pass


def _api() -> str:
    return f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}/actions"


def api_get(url: str) -> tuple[object, str]:
    """One GitHub API response: its JSON body and the URL of its next page, if any."""
    request = urllib.request.Request(url, headers={
        "Authorization": f"token {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
            link = response.headers.get("Link") or ""
    except urllib.error.HTTPError as e:
        raise Refused(f"GitHub API returned HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Refused(f"GitHub API request failed for {url}: {e}") from e
    return body, next(iter(re.findall(r'<([^>]+)>; *rel="next"', link)), "")


def _list(url: str, key: str) -> list[dict]:
    """Every item of a paginated list, following GitHub's Link header."""
    items: list[dict] = []
    while url:
        body, following = api_get(url)
        try:
            items += body[key]
        except (KeyError, TypeError) as e:
            raise Refused(f"GitHub API request failed for {url}: {e}") from e
        url = following
    return items


def release_number(version: str) -> tuple[int, int, int]:
    """X.Y.Z of a version, ignoring a pre-release or build suffix."""
    m = _VERSION.fullmatch(version.strip().removeprefix("v"))
    if not m:
        raise Refused(f"{version!r} is not an X.Y.Z version")
    return tuple(int(n) for n in m.groups())


def _run(workflow: str) -> dict:
    tag, sha = os.environ["GITHUB_REF_NAME"], os.environ["GITHUB_SHA"]
    runs = _list(f"{_api()}/workflows/{workflow}/runs?head_sha={sha}&event=push&per_page=100", "workflow_runs")
    path = rf"\.github/workflows/{re.escape(workflow)}(@.+)?"
    matching = [r for r in runs if re.fullmatch(path, r.get("path", "")) and r.get("head_sha") == sha
                and r.get("event") == "push" and r.get("head_branch") == tag]
    if not matching:
        raise NotReady(f"no {workflow} run for {tag} at {sha} yet")
    if len(matching) > 1:
        ids = ", ".join(str(r["id"]) for r in matching)
        raise Refused(f"{len(matching)} {workflow} runs for {tag} at {sha} ({ids}), so none can be trusted. "
                      "Re-run the failed jobs of one run instead of pushing the tag again.")
    return matching[0]


def platform_builds(platforms: tuple[str, ...]) -> dict[str, Callable[[str], bool]]:
    """build.yml's matrix build job of each platform, by the name GitHub gives it."""
    return {f"build ({os_name})": lambda name, os_name=os_name: name.startswith(f"build ({os_name},")
            for os_name in platforms}


def latest_jobs(run_id: object) -> list[dict]:
    """The jobs of the run's current attempt, with the jobs a re-run kept."""
    return _list(f"{_api()}/runs/{run_id}/jobs?filter=latest&per_page=100", "jobs")


def unfinished(jobs: list[dict], label: str, required: dict[str, Callable[[str], bool]]) -> list[str]:
    """The required jobs that have not succeeded yet. Refused when one is there
    more than once, or finished without succeeding."""
    found = {name: [j for j in jobs if matches(j["name"])] for name, matches in required.items()}
    for name, same in found.items():
        if len(same) > 1:
            raise Refused(f"{label} has {len(same)} {name} jobs")
        if same and same[0]["status"] == "completed" and same[0]["conclusion"] != "success":
            raise Refused(f"{same[0]['name']} in {label} finished {same[0]['conclusion']}")
    return [name for name, same in found.items() if not same or same[0]["conclusion"] != "success"]


def desktop_builds() -> None:
    """The current attempt of build.yml's run: exactly one successful build per
    platform, one successful upgrade test each on Linux and Windows, and every job
    they depend on or that completes the release assets. Refused once the run has
    been building for BUILD_TIME without them."""
    run = _run("build.yml")
    required = {name: lambda job, name=name: job == name for name in BUILD_PREREQUISITES}
    required |= platform_builds(PLATFORMS)
    required |= {name: lambda job, name=name: job.rsplit(" / ", 1)[-1] == name for name in UPGRADE_TESTS}
    jobs = latest_jobs(run["id"])
    missing = unfinished(jobs, f"build.yml run {run['id']}", required)
    if not missing:
        return
    if run["status"] == "completed":
        raise Refused(f"build.yml run {run['id']} finished without {', '.join(missing)}")
    started = building_since(run, jobs)
    if started and datetime.now(timezone.utc) - _time(started) > BUILD_TIME:
        raise Refused(f"build.yml run {run['id']} started building at {started} and has not finished within "
                      f"{BUILD_TIME.seconds // 3600} hours. Once it finishes, re-run the failed publish job")
    raise NotReady(f"waiting for {', '.join(missing)} in build.yml run {run['id']}")


def _time(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def building_since(run: dict, jobs: list[dict]) -> str | None:
    """When the run's current attempt started building: its first job to get a
    runner, not before the attempt began (a re-run keeps its old successful jobs).
    None while every job still waits for a runner. A skipped job never runs, so
    GitHub stamps it started when it was created."""
    started = [j["started_at"] for j in jobs if j["status"] in ("in_progress", "completed")
               and j["conclusion"] != "skipped" and j.get("started_at")]
    return max(min(started), run["run_started_at"], key=_time) if started else None


def pypi_release() -> None:
    """publish.yml's publish job succeeded. Publishing to PyPI is permanent, so a
    success in any attempt of the run stays true: re-running only the GitHub release
    after a failure needs no new PyPI upload."""
    run = _run("publish.yml")
    jobs = _list(f"{_api()}/runs/{run['id']}/jobs?filter=all&per_page=100", "jobs")
    publish = [j for j in jobs if j["name"] == "publish"]
    if any(j["conclusion"] == "success" for j in publish):
        return
    if run["status"] == "completed":
        found = ", ".join(str(j["conclusion"]) for j in publish) or "no publish job"
        raise Refused(f"publish.yml run {run['id']} finished without publishing to PyPI ({found})")
    raise NotReady(f"waiting for the publish job in publish.yml run {run['id']}")


def make_latest() -> bool:
    """No published release has a higher X.Y.Z than this tag. A release whose tag
    is not a version (dev-latest) has no number to compare."""
    tag = os.environ["GITHUB_REF_NAME"]
    url = f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}/releases?per_page=100"
    while url:
        body, url = api_get(url)
        if not isinstance(body, list):
            raise Refused("GitHub API returned no release list")
        for release in body:
            if release["draft"] or release["prerelease"] or not _VERSION.fullmatch(release["tag_name"].removeprefix("v")):
                continue
            if release_number(release["tag_name"]) > release_number(tag):
                print(f"{release['tag_name']} is already published, so {tag} is published without becoming latest",
                      file=sys.stderr)
                return False
    return True


def main() -> int:
    if sys.argv[1] == "make-latest":
        try:
            print(str(make_latest()).lower())
        except Refused as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0
    check = {"desktop-builds": desktop_builds, "pypi-release": pypi_release}[sys.argv[1]]
    try:
        check()
    except NotReady as e:
        print(e)
        return PENDING
    except Refused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
