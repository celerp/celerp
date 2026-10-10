# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The build the packaged upgrade check installs, read from GitHub Actions.

    python scripts/upgrade_candidate.py check <run id> [branch]
    python scripts/upgrade_candidate.py newer <installed version> <previous version>

check: Build Binaries run <run id> finished successfully, built the commit its
branch (or the branch given) pointed at when the run was created, and carries
the Linux and Windows binaries. A re-run keeps the commit its run was created
with, so the branch head is read from the branch's push history at that moment,
not now. The previous release is PREVIOUS, or the latest release when PREVIOUS
is blank. Writes run, sha, previous, previous_version and guarded (whether the
previous release checks the version of an install or its data before opening
it, from GUARDED_SINCE) to GITHUB_OUTPUT.

newer: the installed version's X.Y.Z is above the previous release's. A
development build installs the next patch with a suffix
(scripts/electron_version.py), so a suffixed version with the previous
release's X.Y.Z was built before that release, not after it.

Reads GITHUB_API_URL, GITHUB_REPOSITORY, GH_TOKEN, PREVIOUS, GUARDED_SINCE and
GITHUB_OUTPUT. Exit status: 0 the candidate is fit to test, 1 it is not (any
API error included).
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote

from release_gate import Refused, _list, api_get

WORKFLOW = "build.yml"
ARTIFACTS = ("binaries-ubuntu-latest", "binaries-windows-latest")
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+].*)?")


def _repo() -> str:
    return f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"


def _get(url: str) -> dict:
    body, _ = api_get(url)
    if not isinstance(body, dict):
        raise Refused(f"GitHub API returned no object for {url}")
    return body


def release_number(version: str) -> tuple[int, int, int]:
    """X.Y.Z of a version, ignoring a pre-release or build suffix."""
    m = _VERSION.fullmatch(version.strip().removeprefix("v"))
    if not m:
        raise Refused(f"{version!r} is not an X.Y.Z version")
    return tuple(int(n) for n in m.groups())


def newer(installed: str, previous: str) -> None:
    if release_number(installed) <= release_number(previous):
        raise Refused(f"the candidate installs {installed}, which is not newer than the previous release {previous}")
    print(f"the candidate installs {installed}, newer than the previous release {previous}")


def branch_head_at(branch: str, moment: str) -> str:
    """The commit the branch pointed at when `moment` (ISO 8601, UTC) came:
    the newest change to the branch at or before it, from its activity."""
    url = f"{_repo()}/activity?ref={quote(f'refs/heads/{branch}', safe='')}&per_page=100"
    while url:
        body, url = api_get(url)
        if not isinstance(body, list):
            raise Refused(f"GitHub API returned no activity list for {branch}")
        for change in body:
            if change["timestamp"] <= moment:
                return change["after"]
    raise Refused(f"no change to {branch} at or before {moment} in its activity")


def check(run_id: str, branch: str = "") -> dict[str, str]:
    if not run_id.isdigit():
        raise Refused(f"candidate run {run_id!r} is not a run id")
    run = _get(f"{_repo()}/actions/runs/{run_id}")
    if not re.fullmatch(rf"\.github/workflows/{re.escape(WORKFLOW)}(@.+)?", run.get("path", "")):
        raise Refused(f"run {run_id} is {run.get('path')}, not Build Binaries")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise Refused(f"build run {run_id} is {run.get('status')}, conclusion {run.get('conclusion')}; "
                      "only a successful build is a candidate")
    sha, built_from = run["head_sha"], run["head_branch"]
    if branch and built_from != branch:
        raise Refused(f"build run {run_id} built {built_from}, not {branch}")
    head = branch_head_at(built_from, run["created_at"])
    if sha != head:
        raise Refused(f"build run {run_id} built {sha}, but {built_from} pointed at {head} "
                      f"when the run was created ({run['created_at']})")
    present = {a["name"] for a in _list(f"{_repo()}/actions/runs/{run_id}/artifacts?per_page=100", "artifacts")
               if not a.get("expired") and a.get("size_in_bytes", 0) > 0}
    missing = [name for name in ARTIFACTS if name not in present]
    if missing:
        raise Refused(f"build run {run_id} carries no {', '.join(missing)}")
    previous = os.environ.get("PREVIOUS", "").strip()
    release = _get(f"{_repo()}/releases/tags/{previous}" if previous else f"{_repo()}/releases/latest")
    previous = release["tag_name"]
    guarded = release_number(previous) >= release_number(os.environ["GUARDED_SINCE"])
    return {"run": run_id, "sha": sha, "previous": previous,
            "previous_version": ".".join(map(str, release_number(previous))), "guarded": str(guarded).lower(),
            "branch": built_from}


def main() -> int:
    try:
        if sys.argv[1] == "newer":
            newer(sys.argv[2], sys.argv[3])
            return 0
        found = check(*sys.argv[2:4])
    except Refused as e:
        print(f"::error::{e}")
        return 1
    print(f"candidate: build run {found['run']}, {found['branch']} at {found['sha']}, Linux and Windows binaries; "
          f"previous release {found['previous']} (checks before opening: {found['guarded']})")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.writelines(f"{k}={v}\n" for k, v in found.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
