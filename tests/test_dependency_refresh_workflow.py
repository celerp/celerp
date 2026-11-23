"""The scheduled dependency refresh may replace its branch only when the branch is its own.

Runs the real run block of the workflow's "Open dependency refresh PR and run CI" step
(read from the YAML) against a scratch git origin with a stub ``gh``: named cases per state
of the refresh branch, plus an oracle over every combination of the facts the step decides on.
Merges are squash merges, as in this repository: the merged branch is left behind and its
commits never become ancestors of main.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Callable

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependency-refresh.yml"
BRANCH = "maintenance/python-dependency-refresh"
REFRESH_SUBJECT = "Refresh Python dependencies"

GH_STUB = """#!/usr/bin/env bash
echo "gh $*" >> "$GH_LOG"
if [[ "$1 $2" == "pr list" && " $* " == *" --state open "* ]]; then cat "$GH_OPEN_PR"; fi
if [[ "$1 $2" == "pr list" && " $* " == *" --state merged "* ]]; then cat "$GH_MERGED"; fi
if [[ "$1 $2" == "run list" ]]; then
  cat "$GH_RUNS"
  if [[ -n "${GH_HOOK:-}" ]]; then bash -c "$GH_HOOK" >&2; fi
fi
"""
OTHER_SHA = "0" * 40


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

    def squash_merge(self, branch: str, subject: str) -> None:
        """Squash-merge a local branch into main and push main; the refresh branch stays behind."""
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--squash", branch)
        self.git("commit", "-qm", subject)
        self.git("push", "-q", "origin", "main")

    def run_job(
        self,
        resolved: str,
        open_pr: str,
        merged: str = "",
        runs: str = "",
        hook: str = "",
        after_checkout: Callable[[], None] | None = None,
    ) -> tuple[subprocess.CompletedProcess, str]:
        """A fresh checkout of main, the resolve step's output, then the step under test.

        ``merged`` is what the API lists as head shas of merged PRs from the branch, ``runs``
        the CI runs it lists for the branch head, ``hook`` a command run while the step
        inspects the branch (after it has read the branch head), ``after_checkout`` a change to
        the origin made between the checkout and the step (while the resolve runs).
        """
        runner = self.root / "runner"
        self.git("clone", "-q", "-b", "main", str(self.origin), str(runner), cwd=self.root)
        if after_checkout:
            after_checkout()
        (runner / "constraints.txt").write_text(resolved)
        script = self.root / "step.sh"
        script.write_text(_run_block())
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(GH_STUB)
        (bindir / "gh").chmod(0o755)
        (self.root / "open_pr").write_text(open_pr)
        (self.root / "merged").write_text(merged)
        (self.root / "runs").write_text(runs)
        gh_log = self.root / "gh.log"
        gh_log.write_text("")
        env = {
            **self.env,
            "BRANCH": BRANCH,
            "GH_LOG": str(gh_log),
            "GH_OPEN_PR": str(self.root / "open_pr"),
            "GH_MERGED": str(self.root / "merged"),
            "GH_RUNS": str(self.root / "runs"),
            "GH_HOOK": hook,
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
    """A refresh PR with a person's commit, squash-merged, its branch left behind; main moved on
    since, so only the merged PR's head sha says the branch holds nothing unmerged."""
    scratch.git("switch", "-qc", "old")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    scratch.commit({"conftest.py": "fix\n"}, "Make the suite pass on the new pins")
    left_behind = scratch.push_branch()
    scratch.squash_merge("old", f"{REFRESH_SUBJECT} (#423)")
    scratch.commit({"conftest.py": "fix\nlater\n"}, "Later work on main")
    scratch.git("push", "-q", "origin", "main")

    result, gh = scratch.run_job("dep==1.2\n", open_pr="", merged=f"{OTHER_SHA}\n{left_behind}\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(scratch, scratch.remote_branch()) == "dep==1.2"
    assert "gh pr create" in gh and "gh workflow run" in gh


def test_no_pr_and_unmerged_branch_fails_the_job(scratch: Scratch) -> None:
    """A branch with no PR whose work is on no merged PR and not on main stops the job loudly."""
    scratch.git("switch", "-qc", "old")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    scratch.commit({"conftest.py": "fix\n"}, "Unmerged work")
    before = scratch.push_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_pr="", merged=f"{OTHER_SHA}\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert BRANCH in result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "gh pr create" not in gh and "gh workflow run" not in gh


def test_a_push_to_the_branch_during_the_run_is_never_overwritten(scratch: Scratch) -> None:
    """The force push is leased on the head the step inspected."""
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    scratch.push_branch()
    scratch.commit({"conftest.py": "fix\n"}, "A person's fix, pushed mid-run")
    interloper = scratch.git("rev-parse", "HEAD")
    hook = f"git -C {scratch.seed} push -q origin HEAD:refs/heads/{BRANCH}"

    result, gh = scratch.run_job("dep==1.2\n", open_pr="430", hook=hook)

    assert result.returncode != 0, result.stdout + result.stderr
    assert scratch.remote_branch() == interloper
    assert "gh workflow run" not in gh


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

    result, gh = scratch.run_job("dep==1.1\n", open_pr="430", runs="12345\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "already current" in result.stdout
    assert "gh workflow run" not in gh


def test_current_pr_whose_ci_never_started_gets_it_started(scratch: Scratch) -> None:
    """A run whose CI dispatch failed leaves a current PR with no CI; the next run starts it."""
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\n"}, REFRESH_SUBJECT)
    before = scratch.push_branch()

    result, gh = scratch.run_job("dep==1.1\n", open_pr="430", runs="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert f"gh workflow run ci.yml --ref {BRANCH}" in gh
    assert "gh pr create" not in gh


def _main_moves(s: Scratch) -> None:
    s.git("switch", "-q", "main")
    s.commit({"app.py": "x\n"}, "Unrelated work on main")
    s.git("push", "-q", "origin", "main")


@pytest.mark.parametrize("open_pr", ["", "430"], ids=["no-pr", "open-pr"])
def test_branch_with_no_history_shared_with_main_is_never_replaced(scratch: Scratch, open_pr: str) -> None:
    """No merge base: git cannot say what the branch would lose, so the job keeps it."""
    scratch.git("switch", "-q", "--orphan", "refresh")
    scratch.commit(OWN_COMMIT, REFRESH_SUBJECT)
    before = scratch.push_branch()
    scratch.git("switch", "-q", "main")

    result, gh = scratch.run_job("dep==1.2\n", open_pr=open_pr)

    assert result.returncode == (0 if open_pr else 1), result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "gh workflow run" not in gh and "gh pr create" not in gh


@pytest.mark.parametrize("open_pr", ["", "430"], ids=["no-pr", "open-pr"])
def test_contributor_commit_with_the_jobs_subject_is_never_lost(scratch: Scratch, open_pr: str) -> None:
    """A person's single commit named like the job's, touching only constraints.txt: with an open
    PR it is updated on top (its commit stays in the history); with no PR it is left and the job fails."""
    scratch.git("switch", "-qc", "refresh")
    scratch.commit({"constraints.txt": "dep==1.1\nhand==2.0\n"}, REFRESH_SUBJECT)
    before = scratch.push_branch()
    scratch.git("switch", "-q", "main")

    result, gh = scratch.run_job("dep==1.2\n", open_pr=open_pr)

    after = scratch.remote_branch()
    if open_pr:
        assert result.returncode == 0, result.stdout + result.stderr
        assert after != before
        scratch.git("merge-base", "--is-ancestor", before, after, cwd=scratch.origin)
        assert _tree(scratch, after) == "dep==1.2"
        assert "gh workflow run ci.yml" in gh
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert after == before
        assert "gh workflow run" not in gh and "gh pr create" not in gh


def test_pr_merged_into_another_branch_is_never_replaced(scratch: Scratch) -> None:
    """The API lists the branch head as a merged PR's head, but that PR went to another base:
    the branch's work is not on main, so the job keeps it whatever the API says."""
    scratch.git("switch", "-qc", "refresh")
    scratch.commit(OWN_COMMIT, REFRESH_SUBJECT)
    scratch.commit(HUMAN_COMMIT, "A person's commit")
    before = scratch.push_branch()
    scratch.git("switch", "-q", "main")

    result, gh = scratch.run_job("dep==1.2\n", open_pr="", merged=f"{before}\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert "gh workflow run" not in gh and "gh pr create" not in gh


@pytest.mark.parametrize("has_branch", [False, True], ids=["no-branch", "open-pr-branch"])
def test_main_moving_during_the_resolve_never_publishes(scratch: Scratch, has_branch: bool) -> None:
    """The resolve ran on the checked-out main; if main moved since, nothing is published."""
    before = None
    if has_branch:
        scratch.git("switch", "-qc", "refresh")
        scratch.commit(OWN_COMMIT, REFRESH_SUBJECT)
        before = scratch.push_branch()

    result, gh = scratch.run_job(
        "dep==1.2\n", open_pr="430" if has_branch else "", after_checkout=lambda: _main_moves(scratch)
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "main" in result.stdout
    assert scratch.remote_branch() == before
    assert "gh workflow run" not in gh and "gh pr create" not in gh


# The oracle: every combination of the observed facts, generated, against a reference predicate.
# Branch shapes. "behind-main": the branch is an ancestor of main. "squashed-*": a refresh PR
# squash-merged into main, the branch left behind ("-human" adds a person's commit). "own": the
# job's refresh commit alone, unmerged. "own-human": that plus a person's commit. "own-more": a
# commit with the job's subject that touches more than constraints.txt.
SHAPES = ("none", "behind-main", "squashed-own", "squashed-human", "own", "own-human", "own-more")
OWN_COMMIT = {"constraints.txt": "dep==1.1\n"}
HUMAN_COMMIT = {"conftest.py": "fix\n"}


@dataclass(frozen=True)
class Facts:
    open_pr: bool
    shape: str
    merged: bool  # the branch head is the head sha of a merged PR
    resolved_current: bool  # the resolve equals the PR branch's pins (open PR) or main's (no PR)
    main_moved: bool  # main gained a commit after the branch was built
    ci_started: bool  # the API lists a CI run for the branch head


def _fact_space() -> list[Facts]:
    combos = itertools.product([False, True], SHAPES, [False, True], [False, True], [False, True], [False, True])
    return [f for f in map(lambda c: Facts(*c), combos) if f.shape != "none" or not (f.merged or f.ci_started)]


def _reference(f: Facts) -> str:
    """What the step must do, from the shape the test built (independent of the step's git checks)."""
    if f.shape != "none":
        loses_nothing = f.merged or f.shape in ("behind-main", "squashed-own", "squashed-human", "own")
        if not loses_nothing:
            return "leave-open-pr" if f.open_pr else "fail"
    if f.open_pr:
        if f.shape == "none":
            return "fail"
        on_current_main = f.shape in ("behind-main", "own", "own-human", "own-more") and not f.main_moved
        if f.resolved_current and on_current_main:
            return "current-skip" if f.ci_started else "current-dispatch-ci"
        return "replace"
    if f.resolved_current:
        return "current-skip"
    return "create" if f.shape == "none" else "replace"


def _build(s: Scratch, f: Facts) -> str | None:
    """Realise the shape on the scratch origin; return the remote branch head (None: no branch)."""
    if f.shape == "none":
        pass
    elif f.shape == "behind-main":
        s.push_branch()
    else:
        s.git("switch", "-qc", "refresh")
        if f.shape == "own-more":
            s.commit({**OWN_COMMIT, "pyproject.toml": "deps\nextra\n"}, REFRESH_SUBJECT)
        else:
            s.commit(OWN_COMMIT, REFRESH_SUBJECT)
        if f.shape.endswith("-human"):
            s.commit(HUMAN_COMMIT, "A person's commit")
        s.push_branch()
        if f.shape.startswith("squashed-"):
            s.squash_merge("refresh", f"{REFRESH_SUBJECT} (#423)")
        s.git("switch", "-q", "main")
    if f.main_moved:
        s.commit({"app.py": "x\n"}, "Unrelated work on main")
        s.git("push", "-q", "origin", "main")
    return s.remote_branch()


@pytest.mark.parametrize("facts", _fact_space(), ids=lambda f: "-".join(str(v) for v in astuple(f)))
def test_refresh_step_outcome_matches_the_reference(scratch: Scratch, facts: Facts) -> None:
    before = _build(scratch, facts)
    pinned_ref = before if facts.open_pr and before else "main"
    pinned = scratch.git("show", f"{pinned_ref}:constraints.txt", cwd=scratch.origin) + "\n"
    resolved = pinned if facts.resolved_current else "dep==9.9\n"
    merged = f"{OTHER_SHA}\n" + (f"{before}\n" if facts.merged else "")
    runs = "12345\n" if facts.ci_started else ""

    result, gh = scratch.run_job(resolved, open_pr="430" if facts.open_pr else "", merged=merged, runs=runs)

    after = scratch.remote_branch()
    outcome = _reference(facts)
    pushes = outcome in ("create", "replace")
    observed = (result.returncode, after != before, "gh pr create" in gh, "gh workflow run ci.yml" in gh)
    expected = (
        1 if outcome == "fail" else 0,
        pushes,
        pushes and not facts.open_pr,
        pushes or outcome == "current-dispatch-ci",
    )
    assert observed == expected, f"{outcome}: {result.stdout}{result.stderr}{gh}"
    if pushes:
        assert _tree(scratch, after) == resolved.strip()
        main = scratch.git("rev-parse", "main", cwd=scratch.origin)
        assert scratch.git("merge-base", "main", after, cwd=scratch.origin) == main
