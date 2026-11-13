# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""scripts/shard_durations.py, the refresh of tests/.test_durations: what it counts as tooling
unlike CI's, the environment it runs pytest in, how it times new tests, and what the refresh and
--check write and answer. The collection itself is tested against the real jobs in
test_ci_shard_split.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from packaging.requirements import Requirement

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import shard_durations as sd  # noqa: E402


def test_tooling_unlike_ci_lists_a_missing_or_unmet_requirement_and_skips_one_not_for_here(monkeypatch):
    monkeypatch.setattr(sd, "dev_extra", lambda: [
        Requirement("pytest>=1"),
        Requirement("pytest<1"),
        Requirement("no-such-package-anywhere>=1"),
        Requirement('no-such-package-elsewhere>=1; python_version < "3"'),
    ])
    unmet = sd.tooling_unlike_ci()
    assert len(unmet) == 2, unmet
    assert unmet[0].startswith("pytest<1 (installed: "), unmet
    assert unmet[1] == "no-such-package-anywhere>=1 (installed: None)"


def test_pytest_runs_without_the_calling_session_s_pytest_variables(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    monkeypatch.setenv("SHARD_DURATIONS_KEEP", "1")
    env = sd._clean_env()
    assert "PYTEST_XDIST_WORKER" not in env
    assert env["SHARD_DURATIONS_KEEP"] == "1"


def _pytest_measuring(measured: dict[str, float], returncode: int = 0):
    """A stand-in for the timing run: writes ``measured`` where --durations-path points."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        path = cmd[cmd.index("--durations-path") + 1]
        with open(path, "w") as f:
            json.dump(measured, f)
        return subprocess.CompletedProcess(cmd, returncode, stdout="out", stderr="err")

    return run, calls


def test_time_returns_what_pytest_split_measured_for_each_test(monkeypatch):
    run, calls = _pytest_measuring({"t.py::a": 1.5, "t.py::b": 0.25, "t.py::other": 9.0})
    monkeypatch.setattr(sd.subprocess, "run", run)
    assert sd._time(["t.py::a", "t.py::b"]) == {"t.py::a": 1.5, "t.py::b": 0.25}
    [(cmd, kwargs)] = calls
    assert "--store-durations" in cmd and "t.py::a" in cmd and "t.py::b" in cmd
    assert kwargs["env"] == sd._clean_env()


@pytest.mark.parametrize(("measured", "returncode", "says"), [
    ({"t.py::a": 1.0}, 1, "these tests must pass before they are timed"),
    ({}, 0, "pytest-split measured none of: t.py::a"),
])
def test_time_refuses_a_failing_or_unmeasured_test(monkeypatch, measured, returncode, says):
    run, _ = _pytest_measuring(measured, returncode)
    monkeypatch.setattr(sd.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        sd._time(["t.py::a"])
    assert says in str(exc.value)


@pytest.fixture
def file_with(tmp_path, monkeypatch):
    """tests/.test_durations stood in by a temporary file holding a kept and a stale entry, one
    job running the kept test and a new one; timing gives the new test 2.0 s."""
    path = tmp_path / ".test_durations"
    path.write_text(json.dumps({"t.py::kept": 1.0, "t.py::gone": 3.0}))
    monkeypatch.setattr(sd, "DURATIONS", path)
    monkeypatch.setattr(sd, "split_jobs", lambda: {"shard": ("", [])})
    monkeypatch.setattr(sd, "ids_the_job_runs", lambda job: frozenset({"t.py::kept", "t.py::new"}))
    monkeypatch.setattr(sd, "_time", lambda ids: {i: 2.0 for i in ids})
    return path


def test_check_lists_what_is_out_of_date_and_writes_nothing(file_with, capsys):
    before = file_with.read_text()
    assert sd.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "unmeasured t.py::new" in out and "stale t.py::gone" in out
    assert file_with.read_text() == before


def test_refresh_times_new_tests_and_drops_stale_entries(file_with):
    assert sd.main([]) == 0
    assert json.loads(file_with.read_text()) == {"t.py::kept": 1.0, "t.py::new": 2.0}
    assert sd.main(["--check"]) == 0


def test_refresh_times_each_job_s_tests_in_a_run_of_their_own(file_with, monkeypatch):
    """A browser job's event loop must not share a pytest process with async unit tests, so
    each job's new tests are timed apart, and a test two jobs share is timed once."""
    runs = []
    monkeypatch.setattr(sd, "split_jobs", lambda: {"unit": ("", []), "browser": ("", [])})
    monkeypatch.setattr(sd, "ids_the_job_runs", lambda job: frozenset(
        {"t.py::kept", "t.py::both", f"t.py::{job}"}))
    monkeypatch.setattr(sd, "_time", lambda ids: runs.append(sorted(ids)) or {i: 2.0 for i in ids})
    assert sd.main([]) == 0
    assert runs == [["t.py::both", "t.py::unit"], ["t.py::browser"]]
    assert json.loads(file_with.read_text()) == {
        "t.py::kept": 1.0, "t.py::both": 2.0, "t.py::unit": 2.0, "t.py::browser": 2.0}
