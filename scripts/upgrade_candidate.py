# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The build the packaged upgrade check installs, read from GitHub Actions.

    python scripts/upgrade_candidate.py check <run id>
    python scripts/upgrade_candidate.py newer <installed version> <previous version>

check: Build Binaries run <run id> succeeded, built a commit after the previous
release, and carries the Linux and Windows binaries. A tag's release build runs
the check inside itself (GITHUB_RUN_ID), before the run has finished: there the
latest attempt of its Linux and Windows build jobs must have succeeded. Any
other run must have finished successfully. The commit is the run's head_sha,
which every re-run keeps; a tag build must have built the commit the tag points
at. The previous release is PREVIOUS, or when PREVIOUS is blank the latest
release, which is never a draft or a prerelease. The commit must contain the
previous release and add to it: a commit on a side branch, an older one or the
release's own commit is refused whatever version it carries. Writes run, sha,
previous, previous_version, guarded (whether the previous release checks the
version of an install or its data before opening it, from GUARDED_SINCE) and ref
to GITHUB_OUTPUT.

newer: the installed version's X.Y.Z is above the previous release's. A
development build installs the next patch with a suffix
(scripts/electron_version.py), so a suffixed version with the previous
release's X.Y.Z was built before that release, not after it.

Reads GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID, GH_TOKEN, PREVIOUS,
GUARDED_SINCE and GITHUB_OUTPUT. Exit status: 0 the candidate is fit to test, 1 it is not (any
API error included).
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote

from release_gate import Refused, _list, api_get, platform_builds, release_number, unfinished

WORKFLOW = "build.yml"
PLATFORMS = ("ubuntu-latest", "windows-latest")
ARTIFACTS = tuple(f"binaries-{os_name}" for os_name in PLATFORMS)


def _repo() -> str:
    return f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"


def _get(url: str) -> dict:
    body, _ = api_get(url)
    if not isinstance(body, dict):
        raise Refused(f"GitHub API returned no object for {url}")
    return body


def newer(installed: str, previous: str) -> None:
    if release_number(installed) <= release_number(previous):
        raise Refused(f"the candidate installs {installed}, which is not newer than the previous release {previous}")
    print(f"the candidate installs {installed}, newer than the previous release {previous}")


def tag_commit(name: str) -> str | None:
    """The commit tag `name` points at, or None when there is no such tag."""
    body, _ = api_get(f"{_repo()}/git/matching-refs/tags/{quote(name)}?per_page=100")
    if not isinstance(body, list):
        raise Refused(f"GitHub API returned no ref list for tag {name}")
    target = next((ref["object"] for ref in body if ref.get("ref") == f"refs/tags/{name}"), None)
    while target is not None and target["type"] == "tag":  # an annotated tag points at its tag object
        target = _get(f"{_repo()}/git/tags/{target['sha']}")["object"]
    return None if target is None else target["sha"]


def check(run_id: str) -> dict[str, str]:
    if not run_id.isdigit():
        raise Refused(f"candidate run {run_id!r} is not a run id")
    run = _get(f"{_repo()}/actions/runs/{run_id}")
    label = f"build run {run_id}"
    if not re.fullmatch(rf"\.github/workflows/{re.escape(WORKFLOW)}(@.+)?", run.get("path", "")):
        raise Refused(f"run {run_id} is {run.get('path')}, not Build Binaries")
    if run.get("status") != "completed":
        if run_id != os.environ.get("GITHUB_RUN_ID"):
            raise Refused(f"{label} is {run.get('status')}; "
                          "only a finished build, or the release build this check runs in, is a candidate")
        missing = unfinished(run_id, label, platform_builds(PLATFORMS))
        if missing:
            raise Refused(f"{label} has not finished {', '.join(missing)}")
    elif run.get("conclusion") != "success":
        raise Refused(f"{label} is {run.get('status')}, conclusion {run.get('conclusion')}; "
                      "only a successful build is a candidate")
    sha, ref = run["head_sha"], run["head_branch"]
    tagged = tag_commit(ref)
    if tagged is not None and sha != tagged:
        raise Refused(f"{label} built {sha}, but tag {ref} points at {tagged}")
    present = {a["name"] for a in _list(f"{_repo()}/actions/runs/{run_id}/artifacts?per_page=100", "artifacts")
               if not a.get("expired") and a.get("size_in_bytes", 0) > 0}
    missing = [name for name in ARTIFACTS if name not in present]
    if missing:
        raise Refused(f"{label} carries no {', '.join(missing)}")
    previous = os.environ.get("PREVIOUS", "").strip()
    release = _get(f"{_repo()}/releases/tags/{previous}" if previous else f"{_repo()}/releases/latest")
    previous = release["tag_name"]
    lineage = _get(f"{_repo()}/compare/{quote(previous)}...{sha}?per_page=1")
    if lineage.get("status") != "ahead":
        raise Refused(f"{label} built {sha}, which is {lineage.get('status')} {previous}, not a commit after it")
    behind = _get(f"{_repo()}/compare/{sha}...main?per_page=1")["ahead_by"]
    print(f"commit {sha} is {lineage['ahead_by']} commits after {previous} and {behind} commits behind main")
    guarded = release_number(previous) >= release_number(os.environ["GUARDED_SINCE"])
    return {"run": run_id, "sha": sha, "previous": previous,
            "previous_version": ".".join(map(str, release_number(previous))), "guarded": str(guarded).lower(),
            "ref": ref}


def main() -> int:
    try:
        if sys.argv[1] == "newer":
            newer(sys.argv[2], sys.argv[3])
            return 0
        found = check(sys.argv[2] if len(sys.argv) > 2 else "")
    except Refused as e:
        print(f"::error::{e}")
        return 1
    print(f"candidate: build run {found['run']}, {found['ref']} at {found['sha']}, Linux and Windows binaries; "
          f"previous release {found['previous']} (checks before opening: {found['guarded']})")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.writelines(f"{k}={v}\n" for k, v in found.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
