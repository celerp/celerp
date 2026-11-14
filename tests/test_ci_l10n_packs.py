# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The release machinery ships the country packs and never ships without them.

The real packs sit in private repositories, so only runs that have the read token fetch them:
pushes to any branch of this repository, tags and manual runs. Each of those runs the real-pack
golden (one workflow, also called by the others) and a build or release waits for it to pass.
Pull requests, forks included, never fetch and run the tests on the committed fixture pack set. Every artifact is
built in a job that fetched the packs first, and checked against the committed lock after.
"""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
GOLDEN = "./.github/workflows/l10n-golden.yml"
ACTION = "./.github/actions/l10n-packs"
GUARD = "github.event_name != 'pull_request'"
SHELL_GUARD = 'if [ "${{ github.event_name }}" != "pull_request" ]; then'
SECRETS = {"CELERP_L10N_TOKEN": "${{ secrets.CELERP_L10N_TOKEN }}",
           "CELERP_L10N_SOURCES": "${{ secrets.CELERP_L10N_SOURCES }}"}
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
        if job.get("uses") == GOLDEN and GUARD not in job_if:
            leaks.append(f"{name}: calls the golden")
        for i, step in enumerate(job.get("steps") or []):
            step_if = job_if + " " + str(step.get("if", ""))
            run = str(step.get("run", ""))
            if step.get("uses") == ACTION and GUARD not in step_if:
                leaks.append(f"{name} step {i}: fetches")
            for call in ("l10n_packs.py fetch", "l10n_packs.py check"):
                if call in run and GUARD not in step_if and not _shell_guarded(run, call):
                    leaks.append(f"{name} step {i}: {call}")
    return leaks


def test_check_golden_gate_hard():
    for name in ("ci.yml", "build.yml", "publish.yml"):
        job = _load(name)["jobs"]["l10n-golden"]
        assert job["uses"] == GOLDEN, name
        assert job["secrets"] == SECRETS, name

    allowed_skip = "(needs.l10n-golden.result == 'success' || (github.event_name == 'pull_request' && needs.l10n-golden.result == 'skipped'))"
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
    events = _events(golden)
    assert set(events) == {"workflow_call", "push", "workflow_dispatch"}  # never pull_request(_target)
    # A push runs it once: the branches whose push workflows call it are left out of its own trigger.
    callers = {b for name in ("ci.yml", "build.yml") for b in _events(_load(name))["push"].get("branches", [])}
    assert set(events["push"]["branches-ignore"]) == callers and "branches" not in events["push"]
    assert golden["jobs"]["golden"]["if"] == "github.repository == 'celerp/celerp'"
    assert set(_events(golden)["workflow_call"]["secrets"]) == set(SECRETS)
    [job] = golden["jobs"].values()
    assert "postgres" in job["services"]
    steps = job["steps"]
    install = next(i for i, s in enumerate(steps) if 'uv pip install --system -e ".[dev]" -c constraints.txt' in str(s.get("run", "")))
    fetch = next(i for i, s in enumerate(steps) if s.get("uses") == ACTION)
    validate = next(i for i, s in enumerate(steps) if "l10n_packs.py validate" in str(s.get("run", "")))
    run = next(i for i, s in enumerate(steps) if "-m real_packs" in str(s.get("run", "")))
    assert install < fetch < validate < run
    assert steps[run]["run"].startswith("env -u CELERP_L10N_GOLDEN_WRITE pytest ")
    assert steps[fetch]["with"] == {"token": SECRETS["CELERP_L10N_TOKEN"], "sources": SECRETS["CELERP_L10N_SOURCES"]}

    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert pull_request_leaks(_load(path.name)) == [], path.name

    unguarded = _load("build.yml")
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
    """Every command that builds or uploads a wheel runs in a workflow job that fetched the packs first."""
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


def test_check_public_tree_names_no_country_repo():
    """The country repositories are private: the public tree never names one."""
    done = subprocess.run(["git", "grep", "-n", "celerp-l10n" + "-"], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 1 and done.stdout == "", done.stdout


def test_pull_request_leak_check_reads_a_copy():
    """The leak check works on the parsed workflow it is given, not on the files."""
    workflow = copy.deepcopy(_load("ci.yml"))
    workflow["jobs"]["extra"] = {"uses": GOLDEN}
    assert pull_request_leaks(workflow) == ["extra: calls the golden"]
