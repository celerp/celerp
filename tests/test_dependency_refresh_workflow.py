"""The scheduled dependency refresh may replace its branch only when the branch is its own.

Runs the real run block of the workflow's "Open dependency refresh PR and run CI" step
(read from the YAML) against a scratch git origin with a stub ``gh``, one case per state
of the refresh branch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependency-refresh.yml"
BRANCH = "maintenance/python-dependency-refresh"
REFRESH_SUBJECT = "Refresh Python dependencies"

GH_STUB = """#!/usr/bin/env bash
echo "gh $*" >> "$GH_LOG"
if [[ "$1 $2" == "pr list" ]]; then cat "$GH_OPEN_PR"; fi
"""


def _run_block() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["refresh"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Open dependency refresh PR and run CI")


class Scratch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        self.git("init", "-q", "--bare", str(self.origin), cwd=root)
        self.git("init", "-q", "-b", "main", str(self.seed), cwd=root)
        self.commit({"constraints.txt": "dep==1.0\n", "pyproject.toml": "deps\n"}, "main")
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "origin", "main")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.seed, env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files: dict[str, str], subject: str) -> None:
        for name, text in files.items():
            (self.seed / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", subject)

    def push_branch(self) -> str:
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
        return self.remote_branch()

    def remote_branch(self) -> str | None:
        out = self.git("ls-remote", "--heads", str(self.origin), f"refs/heads/{BRANCH}")
        return out.split()[0] if out else None

    def run_job(self, resolved: str, open_pr: str) -> tuple[subprocess.CompletedProcess, str]:
        """A fresh checkout of main, the resolve step's output, then the step under test."""
        runner = self.root / "runner"
        self.git("clone", "-q", "-b", "main", str(self.origin), str(runner), cwd=self.root)
        (runner / "constraints.txt").write_text(resolved)
        script = self.root / "step.sh"
        script.write_text(_run_block())
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(GH_STUB)
        (bindir / "gh").chmod(0o755)
        (self.root / "open_pr").write_text(open_pr)
        gh_log = self.root / "gh.log"
        gh_log.write_text("")
        env = {
            **self.env,
            "BRANCH": BRANCH,
            "GH_LOG": str(gh_log),
            "GH_OPEN_PR": str(self.root / "open_pr"),
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        }
        result = subprocess.run(["bash", str(script)], cwd=runner, env=env, capture_output=True, text=True)
        return result, gh_log.read_text()


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return Scratch(tmp_path)


def _tree(s: Scratch, ref: str) -> str:
    return s.git("show", f"{ref}:constraints.txt", cwd=s.origin)


def test_no_pr_and_no_branch_opens_a_pr(scratch: Scratch) -> None:
    result, gh = scratch.run_job("dep==1.1\n", open_pr="")

    assert result.returncode == 0, result.stdout + result.stderr
    after = scratch.remote_branch()
    assert after and _tree(scratch, after) == "dep==1.1"
    assert scratch.git("log", "--format=%s", f"main..{after}", cwd=scratch.origin) == REFRESH_SUBJECT
    assert "gh pr create" in gh
    assert f"gh workflow run ci.yml --ref {BRANCH}" in gh


def test_no_pr_and_merged_branch_is_replaced(scratch: Scratch) -> None:
    """A branch left behind by a merged refresh PR holds nothing that is not on main."""
    scratch.git("switch", "-qc", "old")
    scratch.commit({"pyproject.toml": "deps\nmore\n"}, "Someone's merged work")
    scratch.push_branch()
    scratch.git("push", "-q", "origin", "HEAD:main")

    result, gh = scratch.run_job("dep==1.1\n", open_pr="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(scratch, scratch.remote_branch()) == "dep==1.1"
    assert "gh pr create" in gh and "gh workflow run" in gh


@pytest.mark.parametrize("main_moved", [False, True], ids=["new-release", "main-moved"])
def test_own_stale_branch_is_rebuilt(scratch: Scratch, main_moved: bool) -> None:
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    before = scratch.push_branch()
    resolved = "dep==1.2\n"
    if main_moved:
        scratch.git("switch", "-q", "main")
        scratch.commit({"app.py": "x\n"}, "Unrelated work on main")
        scratch.git("push", "-q", "origin", "main")
        resolved = "dep==1.1\n"

    result, gh = scratch.run_job(resolved, open_pr="430")

    assert result.returncode == 0, result.stdout + result.stderr
    after = scratch.remote_branch()
    assert after != before
    assert _tree(scratch, after) == resolved.strip()
    assert scratch.git("merge-base", "main", after, cwd=scratch.origin) == scratch.git(
        "rev-parse", "main", cwd=scratch.origin
    )
    assert "gh pr create" not in gh
    assert "gh workflow run ci.yml" in gh


@pytest.mark.parametrize(
    "files, subject",
    [
        ({"pyproject.toml": "deps\nextra\n", "constraints.txt": "dep==1.1\nextra==0.4\n"}, "Add a dependency"),
        ({"ci.yml": "smoke\n"}, "Add a CI smoke"),
        ({"constraints.txt": "dep==1.1\nextra==0.4\n"}, "Pin by hand"),
    ],
    ids=["pyproject", "other-file", "constraints-by-hand"],
)
def test_branch_with_other_commits_is_never_replaced(scratch: Scratch, files: dict, subject: str) -> None:
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    scratch.commit(files, subject)
    before = scratch.push_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_pr="430")

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "leaving it unchanged" in result.stdout
    assert "gh workflow run" not in gh and "gh pr create" not in gh


def test_own_commit_touching_more_than_constraints_is_never_replaced(scratch: Scratch) -> None:
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\n", "pyproject.toml": "deps\nextra\n"}, REFRESH_SUBJECT)
    before = scratch.push_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_pr="430")

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "gh workflow run" not in gh


def test_current_branch_on_current_main_is_left_alone(scratch: Scratch) -> None:
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    before = scratch.push_branch()

    result, gh = scratch.run_job("dep==1.1\n", open_pr="430")

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "already current" in result.stdout
    assert "gh workflow run" not in gh
