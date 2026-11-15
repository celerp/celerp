# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The release machinery ships the country packs and never ships without them.

The real packs are fetched only by pushes to main, tags and manual runs, in one job: the real-pack
golden (one workflow, called by the others) in the country-packs environment. It uploads the packs
it checked, and a build or release waits for it to pass, then downloads them and checks them
against the committed lock. Pull requests and pushes to other branches never fetch and run the
tests on the committed fixture pack set. Every artifact is built in a job that got the checked
packs first, and checked against the committed lock after.
"""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
GOLDEN = "./.github/workflows/l10n-golden.yml"
ACTION = "./.github/actions/l10n-packs"
GUARD = "github.event_name != 'pull_request'"
# build.yml also runs on branch pushes, so it fetches only on a tag or a manual run (one output).
PACKS = "needs.setup-matrix.outputs.packs == 'true'"
PACKS_EVENTS = "${{ github.event_name == 'workflow_dispatch' || startsWith(github.ref, 'refs/tags/v') }}"
GUARDS = (GUARD, PACKS)
SHELL_GUARD = 'if [ "${{ needs.setup-matrix.outputs.packs }}" = "true" ]; then'
ENVIRONMENT = "country-packs"
TOKEN = "secrets.CELERP_L10N_TOKEN"
SECRETS = {"CELERP_L10N_TOKEN": "${{ secrets.CELERP_L10N_TOKEN }}"}
PACKS_CHECK = "--lock celerp/localization/packs.lock.json"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _events(workflow: dict) -> dict:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` key as True
    return on if isinstance(on, dict) else {name: None for name in ([on] if isinstance(on, str) else on)}


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _shell_guarded(run: str, call: str) -> bool:
    """Whether every line of ``run`` holding ``call`` sits inside the pull-request shell guard."""
    inside, ok = False, True
    for line in run.splitlines():
        stripped = line.strip()
        if stripped == SHELL_GUARD:
            inside = True
        elif stripped == "fi":
            inside = False
        elif call in line and not inside:
            ok = False
    return ok


def pull_request_leaks(workflow: dict) -> list[str]:
    """Every job or step of a workflow that triggers on pull_request which would fetch, check or
    run the golden on one: each must carry the pull-request guard in its own or its job's if."""
    if "pull_request" not in _events(workflow):
        return []
    leaks = []
    for name, job in (workflow.get("jobs") or {}).items():
        job_if = str(job.get("if", ""))
        if job.get("uses") == GOLDEN and not any(g in job_if for g in GUARDS):
            leaks.append(f"{name}: calls the golden")
        for i, step in enumerate(job.get("steps") or []):
            step_if = job_if + " " + str(step.get("if", ""))
            run = str(step.get("run", ""))
            if step.get("uses") == ACTION and not any(g in step_if for g in GUARDS):
                leaks.append(f"{name} step {i}: fetches")
            for call in ("l10n_packs.py fetch", "l10n_packs.py check"):
                if call in run and not any(g in step_if for g in GUARDS) and not _shell_guarded(run, call):
                    leaks.append(f"{name} step {i}: {call}")
    return leaks


def test_check_golden_gate_hard():
    for name in ("ci.yml", "build.yml", "publish.yml"):
        job = _load(name)["jobs"]["l10n-golden"]
        assert job["uses"] == GOLDEN and job["secrets"] == "inherit", name

    allowed_skip = "(needs.l10n-golden.result == 'success' || (needs.setup-matrix.outputs.packs != 'true' && needs.l10n-golden.result == 'skipped'))"
    ci = _load("ci.yml")["jobs"]["test"]
    assert "l10n-golden" in _needs(ci)
    verify = "\n".join(str(step.get("run", "")) for step in ci["steps"])
    assert ('if [ "${{ needs.l10n-golden.result }}" != "success" ] && ! { [ "${{ github.event_name }}" = "pull_request" ]'
            ' && [ "${{ needs.l10n-golden.result }}" = "skipped" ]; }; then') in verify
    build = _load("build.yml")["jobs"]["build"]
    assert "l10n-golden" in _needs(build) and allowed_skip in build["if"]
    publish = _load("publish.yml")
    assert "pull_request" not in _events(publish)
    assert "l10n-golden" in _needs(publish["jobs"]["build"])

    golden = _load("l10n-golden.yml")
    assert golden["jobs"]["golden"]["if"] == "github.repository == 'celerp/celerp'"
    [job] = golden["jobs"].values()
    assert "postgres" in job["services"]
    steps = job["steps"]
    install = next(i for i, s in enumerate(steps) if 'uv pip install --system -e ".[dev]" -c constraints.txt' in str(s.get("run", "")))
    fetch = next(i for i, s in enumerate(steps) if "l10n_packs.py fetch" in str(s.get("run", "")))
    validate = next(i for i, s in enumerate(steps) if "l10n_packs.py validate" in str(s.get("run", "")))
    run = next(i for i, s in enumerate(steps) if "-m real_packs" in str(s.get("run", "")))
    assert install < fetch < validate < run
    assert steps[run]["run"].startswith("env -u CELERP_L10N_GOLDEN_WRITE pytest ")

    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert pull_request_leaks(_load(path.name)) == [], path.name

    unguarded = _load("build.yml")
    assert unguarded["jobs"]["setup-matrix"]["outputs"]["packs"] == PACKS_EVENTS
    for step in unguarded["jobs"]["build"]["steps"]:
        if step.get("uses") == ACTION:
            step.pop("if")
    assert pull_request_leaks(unguarded)


def _pytest_mark_expressions(text: str) -> list[str]:
    text = text.replace("\\\n", " ")
    found = []
    for command in re.findall(r"pytest\b[^\n]*", text):
        for quoted_d, quoted_s, bare in re.findall(r"""\s-m\s+(?:"([^"]*)"|'([^']*)'|(\S+))""", command):
            found.append(quoted_d or quoted_s or bare)
    return found


def test_check_mark_expressions_exclude_real_packs():
    def ok(expr: str) -> bool:
        return (re.fullmatch(r"\w+", expr) is not None and expr != "real_packs") or "not real_packs" in expr

    seen = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job in (_load(path.name).get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for expr in _pytest_mark_expressions(str(step.get("run", ""))):
                    seen += 1
                    if path.name == "l10n-golden.yml" and expr == "real_packs":
                        continue
                    assert ok(expr), f"{path.name}: -m {expr!r}"
    assert seen
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    addopts = re.search(r'^addopts = "(.*)"$', pyproject, re.M).group(1)
    [expr] = _pytest_mark_expressions("pytest " + addopts)
    assert ok(expr), expr
    assert re.search(r'"real_packs: ', pyproject)


_ARTIFACT = r"python -m build|-m build\b|pip wheel|bdist_wheel|twine upload"


def test_check_artifact_commands_fetch_packs():
    """Every command that builds or uploads a wheel runs in a workflow job that got the checked packs first."""
    hits = subprocess.run(["git", "grep", "-n", "-E", _ARTIFACT, "--", ".", ":!tests", ":!docs",
                           ":!default_modules/*/tests"], cwd=ROOT, capture_output=True, text=True).stdout
    assert hits.strip(), "the release build command was not found"
    for hit in hits.splitlines():
        path, _, text = hit.split(":", 2)
        assert path.startswith(".github/workflows/"), hit
        found = False
        for job in (_load(Path(path).name).get("jobs") or {}).values():
            steps = job.get("steps") or []
            for i, step in enumerate(steps):
                if text.strip() in str(step.get("run", "")):
                    found = True
                    assert any(s.get("uses") == ACTION for s in steps[:i]), hit
        assert found, hit


def test_check_packaged_l10n_packs():
    steps = _load("build.yml")["jobs"]["build"]["steps"]
    [packaged] = [s for s in steps if "scripts/check_packaged_modules.py" in str(s.get("run", ""))]
    run = packaged["run"]
    assert packaged["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert 'RES=' in run and 'PY=' in run
    assert 'PYTHONPATH="$RES/app" "$PY" scripts/l10n_packs.py check' in run
    assert f'"$RES/app/celerp/localization" {PACKS_CHECK}' in run
    assert _shell_guarded(run, "scripts/l10n_packs.py check") and SHELL_GUARD in run

    verify = _load("publish.yml")["jobs"]["verify"]["steps"]
    check = next(i for i, s in enumerate(verify) if "l10n_packs.py check" in str(s.get("run", "")))
    install = next(i for i, s in enumerate(verify) if s.get("name") == "Fresh install and update from the current release")
    assert check < install
    run = verify[check]["run"]
    assert 'python -m zipfile -e dist/*.whl "$RUNNER_TEMP/whl"' in run
    assert f'scripts/l10n_packs.py check "$RUNNER_TEMP/whl/celerp/localization" {PACKS_CHECK}' in run

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"localization/packs.lock.json"' in pyproject and '"localization/packs/*.json"' in pyproject
    assert "recursive-include celerp/localization *.json" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "celerp/localization/packs/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_pull_request_leak_check_reads_a_copy():
    """The leak check works on the parsed workflow it is given, not on the files."""
    workflow = copy.deepcopy(_load("ci.yml"))
    workflow["jobs"]["extra"] = {"uses": GOLDEN}
    assert pull_request_leaks(workflow) == ["extra: calls the golden"]


def _token_jobs(workflow: dict) -> dict[str, dict]:
    """Every job that reads the pack token itself or runs the golden (which reads it)."""
    return {name: job for name, job in (workflow.get("jobs") or {}).items()
            if job.get("uses") == GOLDEN or TOKEN in str(job)}


def _credential_events(workflow: dict) -> dict:
    """The events under which a workflow's token-reading jobs can run, after each job's own guard."""
    events = dict(_events(workflow))
    guards = {str(job.get("if", "")) + str(job.get("environment", "")) for job in _token_jobs(workflow).values()}
    if guards and all(GUARD in g or PACKS in g for g in guards):
        events.pop("pull_request", None)
        events.pop("pull_request_target", None)
    if guards and all(PACKS in g for g in guards):
        events["push"] = {"tags": (events.get("push") or {}).get("tags", [])}
    return events


def token_defects(workflow: dict) -> list[str]:
    """Every way a workflow could fetch the packs outside a country-packs job on main, a tag or a manual run."""
    defects = [f"{name}: not in the {ENVIRONMENT} environment" for name, job in _token_jobs(workflow).items()
               if job.get("uses") != GOLDEN and job.get("environment") != ENVIRONMENT]
    if not _token_jobs(workflow):
        return defects
    events = _credential_events(workflow)
    push = events.get("push") or {}
    if {"pull_request", "pull_request_target"} & set(events):
        defects.append("reads the token on a pull request")
    if not set(push) <= {"branches", "tags"} or not set(push.get("branches", [])) <= {"main"}:
        defects.append("reads the token on a push to a branch other than main")
    if not set(events) <= {"push", "workflow_dispatch", "workflow_call"}:
        defects.append(f"reads the token on {sorted(set(events) - {'push', 'workflow_dispatch', 'workflow_call'})}")
    return defects


def test_packs_are_fetched_only_on_main_tags_and_manual_runs():
    """The packs are fetched only in country-packs jobs, on a push to main, a tag or a manual run."""
    golden = _load("l10n-golden.yml")
    assert set(_events(golden)) == {"workflow_dispatch", "workflow_call"}
    [job] = golden["jobs"].values()
    assert job["environment"] == ENVIRONMENT and job["permissions"] == {"contents": "read"}
    assert _load("publish.yml")["jobs"]["build"]["permissions"] == {"contents": "read"}
    readers = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert "CELERP_L10N_SOURCES" not in path.read_text(encoding="utf-8"), path.name
        workflow = _load(path.name)
        assert token_defects(workflow) == [], path.name
        readers += [f"{path.name}:{n}" for n, j in _token_jobs(workflow).items() if j.get("uses") != GOLDEN]
    assert readers == ["l10n-golden.yml:golden"]


@pytest.mark.parametrize("change, defect", [
    (lambda w: w["jobs"]["golden"].pop("environment"), "golden: not in the country-packs environment"),
    (lambda w: w.update({"on": {"push": {"branches-ignore": ["main"]}}}),
     "reads the token on a push to a branch other than main"),
    (lambda w: w.update({"on": {"pull_request_target": None}}), "reads the token on a pull request"),
])
def test_pack_fetch_check_catches_a_run_outside_those(change, defect):
    workflow = copy.deepcopy(_load("l10n-golden.yml"))
    change(workflow)
    assert defect in token_defects(workflow)


def test_pack_token_check_catches_a_branch_push_build():
    build = _load("build.yml")
    build["jobs"]["l10n-golden"]["if"] = GUARD
    assert "reads the token on a push to a branch other than main" in token_defects(build)


ARTIFACT = "country-packs"
PACKS_DIR = "celerp/localization/packs"


def _steps_using(steps: list[dict], action: str) -> list[int]:
    return [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith(action + "@")]


def test_pack_token_is_read_in_one_job_and_shared_as_a_checked_artifact():
    """Only the protected golden job reads the pack token. It uploads the packs it fetched and
    checked; every caller passes it the secrets, and every other job that needs the packs
    downloads that artifact and checks it against packs.lock.json without the token."""
    action_text = (ROOT / ".github" / "actions" / "l10n-packs" / "action.yml").read_text(encoding="utf-8")
    assert "CELERP_L10N_TOKEN" not in action_text
    holders, callers, users = [], [], []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        jobs = _load(path.name).get("jobs") or {}
        golden_callers = {n for n, j in jobs.items() if j.get("uses") == GOLDEN}
        for name, job in jobs.items():
            if "CELERP_L10N_TOKEN" in str(job):
                holders.append(f"{path.name}:{name}")
            if name in golden_callers:
                callers.append(path.name)
                assert job.get("secrets") == "inherit", f"{path.name}:{name}"
            if any(s.get("uses") == ACTION for s in job.get("steps") or []):
                users.append(f"{path.name}:{name}")
                assert golden_callers & set(_needs(job)), f"{path.name}:{name} does not wait for the golden"
                assert "environment" not in job, f"{path.name}:{name}"
    assert holders == ["l10n-golden.yml:golden"]
    assert sorted(callers) == ["build.yml", "ci.yml", "publish.yml"]
    assert sorted(users) == ["build.yml:build", "publish.yml:build"]

    [golden] = _load("l10n-golden.yml")["jobs"].values()
    assert golden["environment"] == ENVIRONMENT
    steps = golden["steps"]
    [fetch] = [i for i, s in enumerate(steps) if TOKEN in str(s)]
    assert steps[fetch]["env"] == SECRETS and steps[fetch]["run"] == "python scripts/l10n_packs.py fetch"
    [upload] = _steps_using(steps, "actions/upload-artifact")
    assert fetch < upload
    assert steps[upload]["with"] == {"name": ARTIFACT, "path": PACKS_DIR, "if-no-files-found": "error",
                                     "retention-days": 1}

    action = yaml.safe_load(action_text)
    assert "inputs" not in action
    steps = action["runs"]["steps"]
    [download] = _steps_using(steps, "actions/download-artifact")
    assert steps[download]["with"] == {"name": ARTIFACT, "path": PACKS_DIR}
    [check] = [i for i, s in enumerate(steps) if "scripts/l10n_packs.py check" in str(s.get("run", ""))]
    assert download < check
    assert steps[check]["run"].endswith("scripts/l10n_packs.py check celerp/localization")
