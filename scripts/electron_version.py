# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stamp electron/package.json with the version this build installs.

A release tag `vX.Y.Z` installs exactly X.Y.Z. Anything else on a release tag
stops the build: the Windows installer compares versions by their dotted numbers
only, so a release called 2.5.4-rc1 would look identical to 2.5.4 to it.
Development builds install the next patch with a `-dev.N+g<sha>` suffix.

    python scripts/electron_version.py   # reads GITHUB_REF / GITHUB_REF_NAME
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_JSON = REPO_ROOT / "electron" / "package.json"

_RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_DESCRIBE = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)-([0-9]+)-g([0-9a-f]+)")


def release_version(tag: str) -> str:
    """The version a release tag installs. Raises ValueError unless it is vX.Y.Z."""
    version = tag.removeprefix("v")
    if not tag.startswith("v") or not _RELEASE.fullmatch(version):
        raise ValueError(f"release tag {tag!r} is not vX.Y.Z; the installer needs a plain X.Y.Z version")
    return version


def dev_version(describe: str, commit_count: str, short_sha: str) -> str:
    """The version of a development build, from `git describe --long` output."""
    m = _DESCRIBE.fullmatch(describe.strip())
    if not m:
        return f"0.0.1-dev.{commit_count}+g{short_sha}"
    major, minor, patch, ahead, sha = m.groups()
    return f"{major}.{minor}.{int(patch) + 1}-dev.{ahead}+g{sha}"


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout.strip()


def build_version(ref: str, ref_name: str) -> str:
    if ref.startswith("refs/tags/v"):
        return release_version(ref_name)
    return dev_version(
        _git("describe", "--tags", "--match", "v[0-9]*", "--long", "--abbrev=7", "HEAD"),
        _git("rev-list", "--count", "HEAD"),
        _git("rev-parse", "--short=7", "HEAD"),
    )


def main(package_json: Path = PACKAGE_JSON) -> int:
    try:
        version = build_version(os.environ.get("GITHUB_REF", ""), os.environ.get("GITHUB_REF_NAME", ""))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    data = json.loads(package_json.read_text())
    data["version"] = version
    package_json.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Electron package version set to {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
