"""The scheduled dependency refresh never force-pushes, never touches a branch without an open
refresh PR, and publishes only on current main.

With no open refresh PR the job opens a new one on a new branch from current main, named after the
run. An open refresh PR (same repository, into main, branch name starting with the refresh prefix)
whose branch, merged with main, differs from main only in constraints.txt is updated on top with a
fast-forward push; any other open refresh PR is left unchanged. Runs the real run block of the
workflow's "Open dependency refresh PR and run CI" step (read from the YAML) against a scratch git
origin with a stub ``gh``. Named cases, a table over every input of decide(), an oracle over every
combination of branch shapes (including two merge bases and a criss-cross merge), and a check that
the workflow holds no forced write. Merges are squash merges unless a shape says otherwise.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import subprocess
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Callable

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependency-refresh.yml"
BRANCH = "maintenance/python-dependency-refresh"
RUN_ID = "777"
NEW = f"{BRANCH}-{RUN_ID}"  # the branch a run with no open refresh PR creates
REFRESH_SUBJECT = "Refresh Python dependencies"

GH_STUB = """#!/usr/bin/env bash
echo "gh $*" >> "$GH_LOG"
if [[ -n "${GH_HOOK:-}" && "$1 $2" == "$GH_HOOK_ON" ]]; then bash -c "$GH_HOOK" >&2; fi
if [[ "$1 $2" == "pr list" ]]; then cat "$GH_OPEN_PRS"; fi
if [[ "$1 $2" == "run list" ]]; then cat "$GH_RUNS"; fi
"""


def pr(head: str = BRANCH, number: int = 430, fork: bool = False) -> dict:
    """One entry of the open-PR listing the step asks gh for."""
    return {"number": number, "headRefName": head, "isCrossRepository": fork}


OPEN = [pr()]


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["refresh"]["steps"]


def _run_block() -> str:
    return next(s["run"] for s in _steps() if s.get("name") == "Open dependency refresh PR and run CI")


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

    def commit(self, files: dict[str, str | None], subject: str) -> None:
        """Write (or, for None, delete) the files and commit them."""
        for name, text in files.items():
            if text is None:
                (self.seed / name).unlink()
            else:
                (self.seed / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", subject)

    def push_branch(self, name: str = BRANCH) -> str:
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{name}")
        return self.remote_branch(name)

    def push_main(self) -> None:
        self.git("push", "-q", "origin", "main")

    def remote_branch(self, name: str = BRANCH) -> str | None:
        out = self.git("ls-remote", "--heads", str(self.origin), f"refs/heads/{name}")
        return out.split()[0] if out else None

    def squash_merge(self, branch: str, subject: str) -> None:
        """Squash-merge a local branch into main and push main; the refresh branch stays behind."""
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--squash", branch)
        self.git("commit", "-qm", subject)
        self.push_main()

    def run_job(
        self,
        resolved: str,
        open_prs: list[dict],
        runs: str = "",
        hook: str = "",
        hook_on: str = "run list",
        after_checkout: Callable[[], None] | None = None,
    ) -> tuple[subprocess.CompletedProcess, str]:
        """A fresh checkout of main, the resolve step's output, then the step under test.

        ``open_prs`` the open-PR listing gh answers with, ``runs`` the CI runs it lists for the
        branch head, ``hook`` a command run when the step calls ``gh <hook_on>``,
        ``after_checkout`` a change to the origin made between the checkout and the step (while
        the resolve runs).
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
        (self.root / "open_prs").write_text(json.dumps(open_prs))
        (self.root / "runs").write_text(runs)
        gh_log = self.root / "gh.log"
        gh_log.write_text("")
        env = {
            **self.env,
            "BRANCH": BRANCH,
            "GITHUB_RUN_ID": RUN_ID,
            "GH_LOG": str(gh_log),
            "GH_OPEN_PRS": str(self.root / "open_prs"),
            "GH_RUNS": str(self.root / "runs"),
            "GH_HOOK": hook,
            "GH_HOOK_ON": hook_on,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        }
        result = subprocess.run(["bash", str(script)], cwd=runner, env=env, capture_output=True, text=True)
        return result, gh_log.read_text()


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return Scratch(tmp_path)


def _tree(s: Scratch, ref: str) -> str:
    return s.git("show", f"{ref}:constraints.txt", cwd=s.origin)


def _dispatched(gh: str) -> str | None:
    m = re.search(r"^gh workflow run ci\.yml --ref (\S+)", gh, re.MULTILINE)
    return m.group(1) if m else None


def _assert_new_pr_from_main(s: Scratch, gh: str, resolved: str) -> None:
    """The run opened a new refresh PR: one commit on current main, on the run's own branch."""
    new = s.remote_branch(NEW)
    assert new and _tree(s, new) == resolved.strip()
    assert s.git("log", "--format=%s", f"main..{new}", cwd=s.origin) == REFRESH_SUBJECT
    assert s.git("merge-base", "main", new, cwd=s.origin) == s.git("rev-parse", "main", cwd=s.origin)
    assert f"gh pr create --base main --head {NEW} " in gh
    assert _dispatched(gh) == NEW


def _assert_nothing_published(s: Scratch, gh: str, before: str | None) -> None:
    assert s.remote_branch() == before
    assert s.remote_branch(NEW) is None
    assert "gh pr create" not in gh and "gh workflow run" not in gh


# Shapes of a branch left on the origin, each built from main on the local branch "refresh" and
# pushed as BRANCH. "two-bases" and "criss-cross" are the review's history shapes: a person's last
# commit puts back a file, which only a merge over every merge base sees as unmerged work.
OWN_COMMIT = {"constraints.txt": "dep==1.1\n"}
HUMAN_COMMIT = {"conftest.py": "fix\n"}


def _two_bases(s: Scratch) -> None:
    """The PR merged with a merge commit while the person had merged main into their copy and
    then took their own fix out; they push after the merge."""
    s.git("switch", "-qc", "refresh")
    s.commit(OWN_COMMIT, REFRESH_SUBJECT)
    s.commit(HUMAN_COMMIT, "Make the suite pass on the new pins")
    pushed = s.push_branch()
    s.git("switch", "-q", "main")
    s.commit({"app.py": "v2\n"}, "main work")
    s.push_main()
    s.git("switch", "-q", "refresh")
    s.git("merge", "-q", "--no-edit", "main")
    s.commit({"conftest.py": None}, "The conftest fix was wrong, take it out")
    s.git("switch", "-q", "main")
    s.git("merge", "-q", "--no-ff", "--no-edit", pushed)
    s.push_main()
    s.git("switch", "-q", "refresh")
    s.push_branch()
    s.git("switch", "-q", "main")


def _criss_cross(s: Scratch) -> None:
    """main merged the branch, the branch merged main's earlier tip, then put app.py back."""
    s.git("switch", "-qc", "refresh")
    s.commit({"notes.txt": "b1\n"}, "B1")
    s.git("switch", "-q", "main")
    s.commit({"app.py": "m1\n"}, "main moves on m1")
    s.git("merge", "-q", "--no-edit", "refresh")
    s.push_main()
    s.git("switch", "-q", "refresh")
    s.git("merge", "-q", "--no-edit", "main~1")
    s.commit({"app.py": None}, "Put app.py back")
    s.push_branch()
    s.git("switch", "-q", "main")


def _linear(s: Scratch, *commits: tuple[dict, str], squash: bool = False) -> None:
    s.git("switch", "-qc", "refresh")
    for files, subject in commits:
        s.commit(files, subject)
    s.push_branch()
    if squash:
        s.squash_merge("refresh", f"{REFRESH_SUBJECT} (#423)")
    s.git("switch", "-q", "main")


def _unrelated(s: Scratch) -> None:
    s.git("switch", "-q", "--orphan", "refresh")
    s.commit(OWN_COMMIT, REFRESH_SUBJECT)
    s.push_branch()
    s.git("switch", "-q", "main")


SHAPES: dict[str, Callable[[Scratch], None]] = {
    "none": lambda s: None,
    "behind-main": lambda s: s.push_branch(),
    "squashed-own": lambda s: _linear(s, (OWN_COMMIT, REFRESH_SUBJECT), squash=True),
    "squashed-human": lambda s: _linear(
        s, (OWN_COMMIT, REFRESH_SUBJECT), (HUMAN_COMMIT, "A person's commit"), squash=True
    ),
    "pins": lambda s: _linear(s, (OWN_COMMIT, REFRESH_SUBJECT)),
    "pins-human": lambda s: _linear(s, (OWN_COMMIT, REFRESH_SUBJECT), (HUMAN_COMMIT, "A person's commit")),
    "pins-more": lambda s: _linear(s, ({**OWN_COMMIT, "pyproject.toml": "deps\nextra\n"}, REFRESH_SUBJECT)),
    "human": lambda s: _linear(s, (HUMAN_COMMIT, "A person's commit")),
    "unrelated": _unrelated,
    "two-bases": _two_bases,
    "criss-cross": _criss_cross,
}
# Shapes whose merge into main would change nothing but constraints.txt.
PINS_ONLY = ("behind-main", "squashed-own", "squashed-human", "pins")


def test_no_open_pr_opens_a_new_branch_from_main(scratch: Scratch) -> None:
    result, gh = scratch.run_job("dep==1.1\n", open_prs=[])

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_new_pr_from_main(scratch, gh, "dep==1.1\n")
    assert "gh pr list --state open --base main " in gh
    assert scratch.remote_branch() is None


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda s: SHAPES["pins-human"](s), id="closed-unmerged"),
        pytest.param(lambda s: SHAPES["squashed-human"](s), id="squash-merged"),
        pytest.param(lambda s: _linear(s, (OWN_COMMIT, REFRESH_SUBJECT)), id="failed-pr-create"),
        pytest.param(_two_bases, id="two-merge-bases-revert"),
        pytest.param(_criss_cross, id="criss-cross-revert"),
        pytest.param(_unrelated, id="unrelated-history"),
    ],
)
def test_leftover_branch_with_no_open_pr_is_never_touched(scratch: Scratch, build: Callable) -> None:
    """A branch with no open PR, whatever its history, never stops a run and is never read or
    written: the run opens a new PR on its own branch and the person's commits stay where they are."""
    build(scratch)
    before = scratch.remote_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_prs=[])

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    _assert_new_pr_from_main(scratch, gh, "dep==1.2\n")


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param([pr(fork=True)], id="fork-pr-same-head-name"),
        pytest.param([pr(head="feature/other")], id="other-branch-pr"),
    ],
)
def test_a_pr_that_is_not_the_refresh_pr_is_ignored(scratch: Scratch, listing: list[dict]) -> None:
    """A fork's PR named like the refresh branch, or another same-repo PR, is not the refresh PR:
    the leftover branch of that name is untouched and the run opens its own PR."""
    SHAPES["pins"](scratch)
    before = scratch.remote_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_prs=listing)

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    _assert_new_pr_from_main(scratch, gh, "dep==1.2\n")


def test_open_refresh_pr_on_a_run_branch_is_updated(scratch: Scratch) -> None:
    """The refresh PR an earlier run opened lives on a run-named branch; it is the one updated."""
    earlier = f"{BRANCH}-123"
    scratch.git("switch", "-qc", "refresh")
    scratch.commit(OWN_COMMIT, REFRESH_SUBJECT)
    before = scratch.push_branch(earlier)
    scratch.git("switch", "-q", "main")

    result, gh = scratch.run_job("dep==1.2\n", open_prs=[pr(fork=True), pr(head=earlier, number=431)])

    assert result.returncode == 0, result.stdout + result.stderr
    after = scratch.remote_branch(earlier)
    scratch.git("merge-base", "--is-ancestor", before, after, cwd=scratch.origin)
    assert _tree(scratch, after) == "dep==1.2"
    assert scratch.remote_branch(NEW) is None and "gh pr create" not in gh
    assert _dispatched(gh) == earlier


def test_a_push_to_the_branch_during_the_run_is_never_overwritten(scratch: Scratch) -> None:
    """The update push is plain: the remote takes it only as a fast-forward of the inspected head."""
    SHAPES["pins"](scratch)
    scratch.git("switch", "-q", "refresh")
    scratch.commit({"conftest.py": "fix\n"}, "A person's fix, pushed mid-run")
    interloper = scratch.git("rev-parse", "HEAD")
    hook = f"git -C {scratch.seed} push -q origin HEAD:refs/heads/{BRANCH}"

    result, gh = scratch.run_job("dep==1.2\n", open_prs=OPEN, hook=hook)

    assert result.returncode != 0, result.stdout + result.stderr
    assert scratch.remote_branch() == interloper
    assert "gh workflow run" not in gh


@pytest.mark.parametrize("main_change", [None, "app.py", "constraints.txt"], ids=["new-release", "main-moved", "main-repinned"])
def test_stale_pins_only_branch_is_updated_on_top(scratch: Scratch, main_change: str | None) -> None:
    """Also when main changed constraints.txt too, so the merge conflicts there and only there."""
    SHAPES["pins"](scratch)
    before = scratch.remote_branch()
    resolved = "dep==1.2\n"
    if main_change:
        scratch.commit({main_change: "dep==1.0\nother==2.0\n"}, "Other work on main")
        scratch.push_main()
        resolved = "dep==1.1\n"

    result, gh = scratch.run_job(resolved, open_prs=OPEN)

    assert result.returncode == 0, result.stdout + result.stderr
    after = scratch.remote_branch()
    assert after != before
    scratch.git("merge-base", "--is-ancestor", before, after, cwd=scratch.origin)
    assert _tree(scratch, after) == resolved.strip()
    assert scratch.git("merge-base", "main", after, cwd=scratch.origin) == scratch.git(
        "rev-parse", "main", cwd=scratch.origin
    )
    assert "gh pr create" not in gh
    assert _dispatched(gh) == BRANCH


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda s: _linear(
                s,
                (OWN_COMMIT, REFRESH_SUBJECT),
                ({"pyproject.toml": "deps\nextra\n", "constraints.txt": "dep==1.1\nextra==0.4\n"}, "Add a dep"),
            ),
            id="pyproject",
        ),
        pytest.param(lambda s: SHAPES["pins-human"](s), id="other-file"),
        pytest.param(lambda s: SHAPES["pins-more"](s), id="refresh-named-commit-touching-more"),
        pytest.param(_two_bases, id="two-merge-bases-revert"),
        pytest.param(_criss_cross, id="criss-cross-revert"),
    ],
)
def test_open_pr_branch_with_other_unmerged_work_is_left_unchanged(scratch: Scratch, build: Callable) -> None:
    build(scratch)
    before = scratch.remote_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_prs=OPEN)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "leaving it unchanged" in result.stdout
    _assert_nothing_published(scratch, gh, before)


def test_current_branch_on_current_main_is_left_alone(scratch: Scratch) -> None:
    SHAPES["pins"](scratch)
    before = scratch.remote_branch()

    result, gh = scratch.run_job("dep==1.1\n", open_prs=OPEN, runs="12345\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already current" in result.stdout
    _assert_nothing_published(scratch, gh, before)


def test_current_pr_whose_ci_never_started_gets_it_started(scratch: Scratch) -> None:
    """A run whose CI dispatch failed leaves a current PR with no CI; the next run starts it."""
    SHAPES["pins"](scratch)
    before = scratch.remote_branch()

    result, gh = scratch.run_job("dep==1.1\n", open_prs=OPEN, runs="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert scratch.remote_branch() == before
    assert _dispatched(gh) == BRANCH
    assert "gh pr create" not in gh


@pytest.mark.parametrize("open_prs", [[], OPEN], ids=["no-pr", "open-pr"])
def test_contributor_commit_with_the_jobs_subject_is_never_lost(scratch: Scratch, open_prs: list) -> None:
    """A person's single commit named like the job's, touching only constraints.txt: with an open
    PR it is updated on top (its commit stays in the history); with no PR it is untouched."""
    _linear(scratch, ({"constraints.txt": "dep==1.1\nhand==2.0\n"}, REFRESH_SUBJECT))
    before = scratch.remote_branch()

    result, gh = scratch.run_job("dep==1.2\n", open_prs=open_prs)

    assert result.returncode == 0, result.stdout + result.stderr
    after = scratch.remote_branch()
    if open_prs:
        scratch.git("merge-base", "--is-ancestor", before, after, cwd=scratch.origin)
        assert after != before and _tree(scratch, after) == "dep==1.2"
        assert _dispatched(gh) == BRANCH
    else:
        assert after == before
        _assert_new_pr_from_main(scratch, gh, "dep==1.2\n")


def _main_moves(s: Scratch) -> None:
    s.git("switch", "-q", "main")
    s.commit({"app.py": "x\n"}, "Unrelated work on main")
    s.push_main()


@pytest.mark.parametrize(
    "has_pr, hook_on",
    [(False, None), (True, None), (False, "pr list"), (True, "run list")],
    ids=["create-during-resolve", "update-during-resolve", "create-after-check", "update-after-check"],
)
def test_main_moving_before_the_push_never_publishes(scratch: Scratch, has_pr: bool, hook_on: str | None) -> None:
    """The resolve ran on the checked-out main. If main moves while it resolves, or after the
    first check and before the push (here while the step asks gh about PRs or CI runs), nothing
    is published."""
    before = None
    if has_pr:
        SHAPES["pins"](scratch)
        before = scratch.remote_branch()
    moves = lambda: _main_moves(scratch)  # noqa: E731
    if hook_on:
        hook = f"cd {shlex.quote(str(scratch.seed))} && git switch -q main && echo y >> app.py && " \
               "git add -A && git commit -qm 'main moves mid-run' && git push -q origin main"
        result, gh = scratch.run_job("dep==1.2\n", open_prs=OPEN if has_pr else [], hook=hook, hook_on=hook_on)
    else:
        result, gh = scratch.run_job("dep==1.2\n", open_prs=OPEN if has_pr else [], after_checkout=moves)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "main moved" in result.stdout
    _assert_nothing_published(scratch, gh, before)


# The decision table: decide() itself, extracted from the step, over every combination of its inputs.
DECIDE_INPUTS = ("open", "pins_only", "current", "ci")


def _decide_table(open: bool, pins_only: bool, current: bool, ci: bool) -> str:
    """The rule, written as the table it is: what each combination of facts must do."""
    if not open:
        return "current-skip" if current else "create"
    if not pins_only:
        return "leave-open-pr"  # unmerged work beyond constraints.txt stays for its PR
    if current:
        return "current-skip" if ci else "current-dispatch-ci"
    return "update"


def test_decide_matches_the_table_for_every_input() -> None:
    decide = re.search(r"^decide\(\) \{\n.*?^\}$", _run_block(), re.MULTILINE | re.DOTALL)
    assert decide, "decide() not found in the step"
    combos = list(itertools.product([0, 1], repeat=len(DECIDE_INPUTS)))
    calls = "\n".join(f"decide {' '.join(map(str, c))}" for c in combos)
    out = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{decide.group(0)}\n{calls}"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert dict(zip(combos, out)) == {c: _decide_table(*map(bool, c)) for c in combos}


# The oracle: every combination of branch shape and the other observed facts, built in git and run
# through the step, against a reference predicate on the shape alone.
@dataclass(frozen=True)
class Facts:
    open_pr: bool  # BRANCH has an open refresh PR
    shape: str
    resolved_current: bool  # the resolve equals the PR branch's pins (open PR) or main's (no PR)
    main_moved: bool  # main gained an unrelated commit after the branch was built
    ci_started: bool  # the API lists a CI run for the PR branch head


def _fact_space() -> list[Facts]:
    combos = itertools.product([False, True], SHAPES, [False, True], [False, True], [False, True])
    # An open PR always has its branch (GitHub closes a PR whose head branch is deleted), and CI
    # runs are only asked for an open PR's branch.
    return [
        f
        for f in map(lambda c: Facts(*c), combos)
        if (f.shape != "none" or not f.open_pr) and (f.open_pr or not f.ci_started)
    ]


def _reference(f: Facts) -> str:
    """What the step must do, from the shape the test built (independent of the step's git checks)."""
    if not f.open_pr:
        return "current-skip" if f.resolved_current else "create"
    if f.shape not in PINS_ONLY:
        return "leave-open-pr"
    on_current_main = f.shape in ("behind-main", "pins") and not f.main_moved
    if f.resolved_current and on_current_main:
        return "current-skip" if f.ci_started else "current-dispatch-ci"
    return "update"


@pytest.mark.parametrize("facts", _fact_space(), ids=lambda f: "-".join(str(v) for v in astuple(f)))
def test_refresh_step_outcome_matches_the_reference(scratch: Scratch, facts: Facts) -> None:
    SHAPES[facts.shape](scratch)
    if facts.main_moved:
        scratch.commit({"app.py": "moved\n"}, "Unrelated work on main")
        scratch.push_main()
    before = scratch.remote_branch()
    pinned_ref = BRANCH if facts.open_pr else "main"
    pinned = scratch.git("show", f"{pinned_ref}:constraints.txt", cwd=scratch.origin) + "\n"
    resolved = pinned if facts.resolved_current else "dep==9.9\n"

    result, gh = scratch.run_job(resolved, open_prs=OPEN if facts.open_pr else [], runs="12345\n" * facts.ci_started)

    after = scratch.remote_branch()
    outcome = _reference(facts)
    observed = (result.returncode, after != before, scratch.remote_branch(NEW) is not None, "gh pr create" in gh, _dispatched(gh))
    expected = {
        "create": (0, False, True, True, NEW),
        "update": (0, True, False, False, BRANCH),
        "current-dispatch-ci": (0, False, False, False, BRANCH),
    }.get(outcome, (0, False, False, False, None))
    assert observed == expected, f"{outcome}: {result.stdout}{result.stderr}{gh}"
    if outcome == "create":
        _assert_new_pr_from_main(scratch, gh, resolved)
    if outcome == "update":
        scratch.git("merge-base", "--is-ancestor", before, after, cwd=scratch.origin)
        assert _tree(scratch, after) == resolved.strip()
        main = scratch.git("rev-parse", "main", cwd=scratch.origin)
        assert scratch.git("merge-base", "main", after, cwd=scratch.origin) == main


# The class seal: the job publishes only by adding to the remote. Any forced or deleting write of a
# ref anywhere in the workflow fails here.
def _forced_writes(script: str) -> list[str]:
    found = []
    for line in script.replace("\\\n", " ").splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            words = line.split()
        for i, word in enumerate(words):
            rest = words[i + 1 :]
            if word == "git" and "push" in rest:
                args = rest[rest.index("push") + 1 :]
                if any(
                    a.startswith(("--force", "--delete", "--mirror", "--prune", "+", ":"))
                    or re.fullmatch(r"-[a-zA-Z]*[fd][a-zA-Z]*", a)
                    for a in args
                ):
                    found.append(line.strip())
            elif word == "git" and rest[:1] == ["branch"] and any(a in ("-D", "-d", "--delete") for a in rest):
                found.append(line.strip())
            elif word == "gh" and rest[:1] == ["api"] and re.search(r"\b(DELETE|PATCH)\b", " ".join(rest)):
                found.append(line.strip())
    return found


@pytest.mark.parametrize(
    "line",
    [
        "git push --force origin HEAD:refs/heads/x",
        "git push --force-with-lease=refs/heads/x:abc origin HEAD:refs/heads/x",
        "git push -f origin x",
        "git push -qf origin x",
        "git push origin +HEAD:refs/heads/x",
        "git push origin :refs/heads/x",
        "git push --delete origin x",
        "git push origin -d x",
        "git branch -D x",
        "gh api -X DELETE repos/o/r/git/refs/heads/x",
    ],
)
def test_the_forced_write_check_catches_each_spelling(line: str) -> None:
    assert _forced_writes(line) == [line]


def test_the_workflow_never_force_writes_a_ref() -> None:
    scripts = [s["run"] for s in _steps() if "run" in s]
    assert any("git push origin" in s for s in scripts), "the plain push is where this check looks"
    assert [w for s in scripts for w in _forced_writes(s)] == []
