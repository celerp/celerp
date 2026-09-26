# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Self-update ordering, undo rules and resume after an interrupted update.

`run_update` and `reconcile` are driven with recording fake steps, so each test
asserts the exact sequence of work for one path.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from celerp.services import update


class FakeSteps(update.Steps):
    def __init__(self, fail: str | None = None, fail_times: int = 1, on_install=None):
        self.calls: list[str] = []
        self.fail = fail
        self.fail_times = fail_times
        self.on_install = on_install

    def _do(self, name: str, *args):
        self.calls.append(name)
        if self.fail == name and self.fail_times > 0:
            self.fail_times -= 1
            raise update.UpdateError(f"{name} broke")

    def freeze(self, path):
        self._do("freeze")
        path.write_text("celerp==1.0.0\n")

    def dump(self, path):
        self._do("dump")
        path.write_bytes(b"DUMP")

    def stop_cluster(self): self._do("stop_cluster")
    def start_cluster(self): self._do("start_cluster")
    def migrate(self): self._do("migrate")
    def restore(self, path): self._do("restore")
    def reinstall(self, path): self._do("reinstall")
    def stop_children(self, children): self._do("stop_children")

    def install(self, target):
        self._do("install")
        if self.on_install:
            self.on_install(target)

    def verify(self, target):
        self._do("verify")
        return ("api", "ui")


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    return tmp_path


FORWARD = ["freeze", "dump", "stop_cluster", "install", "start_cluster", "migrate"]
UNDO_PACKAGES = ["stop_cluster", "reinstall", "start_cluster"]


def test_success_runs_steps_in_order_and_records_ok(cfg_dir):
    steps = FakeSteps()
    result, children = update.run_update("1.1.0", steps)
    assert steps.calls == FORWARD + ["verify"]
    assert children == ("api", "ui")
    assert result["outcome"] == update.OK and result["ok"] is True
    state = update.read_state()
    assert "in_progress" not in state
    assert state["last_result"]["to"] == "1.1.0"
    assert state["last_result"]["notified"] is False
    assert state.get("failed_versions", []) == []


def test_without_verify_stops_after_migrate(cfg_dir):
    steps = FakeSteps()
    result, children = update.run_update("1.1.0", steps, verify=False)
    assert steps.calls == FORWARD
    assert children == ()
    assert result["outcome"] == update.OK


@pytest.mark.parametrize("step", ["freeze", "dump"])
def test_backup_failure_changes_nothing(cfg_dir, step):
    steps = FakeSteps(fail=step)
    result, _ = update.run_update("1.1.0", steps)
    assert "install" not in steps.calls and "stop_cluster" not in steps.calls
    assert result["outcome"] == update.FAILED
    assert "backup failed" in result["reason"]
    assert update.read_state()["failed_versions"] == ["1.1.0"]


def test_install_failure_reinstalls_packages_without_touching_database(cfg_dir):
    steps = FakeSteps(fail="install")
    result, _ = update.run_update("1.1.0", steps)
    assert steps.calls == ["freeze", "dump", "stop_cluster", "install"] + UNDO_PACKAGES
    assert result["outcome"] == update.FAILED
    assert "install failed" in result["reason"]


def test_migrate_failure_restores_database_and_packages(cfg_dir):
    steps = FakeSteps(fail="migrate")
    result, _ = update.run_update("1.1.0", steps)
    assert steps.calls == FORWARD + ["restore"] + UNDO_PACKAGES
    assert result["outcome"] == update.ROLLED_BACK


def test_verify_failure_restores_database_then_packages(cfg_dir):
    steps = FakeSteps(fail="verify")
    result, children = update.run_update("1.1.0", steps)
    assert steps.calls == FORWARD + ["verify", "restore"] + UNDO_PACKAGES
    assert children == ()
    assert result["outcome"] == update.ROLLED_BACK
    assert "did not start" in result["reason"]
    assert update.read_state()["failed_versions"] == ["1.1.0"]


def test_failed_rollback_is_reported_and_keeps_the_backup(cfg_dir):
    steps = FakeSteps(fail="verify")
    steps.restore = lambda path: (_ for _ in ()).throw(RuntimeError("restore broke"))
    result, _ = update.run_update("1.1.0", steps)
    assert result["outcome"] == update.ROLLBACK_FAILED
    assert "restore broke" in result["reason"]
    freeze, dump = update.backup_paths()
    assert freeze.exists() and dump.exists()
    assert "in_progress" not in update.read_state()


def test_failed_versions_are_not_duplicated(cfg_dir):
    update.run_update("1.1.0", FakeSteps(fail="install"))
    update.run_update("1.1.0", FakeSteps(fail="install"))
    assert update.read_state()["failed_versions"] == ["1.1.0"]


@pytest.mark.parametrize("bad", ["1.1.0+local", "1.1.0.dev1", "latest", "1.0; rm -rf /", ""])
def test_target_must_be_a_release_version(cfg_dir, bad):
    steps = FakeSteps()
    with pytest.raises(update.UpdateError):
        update.run_update(bad, steps)
    assert steps.calls == []


# ── State written before each step (what reconcile resumes from) ─────────────


def test_each_step_is_recorded_before_it_runs(cfg_dir):
    seen = {}

    class Spy(FakeSteps):
        def _do(self, name, *args):
            seen[name] = (update.read_state().get("in_progress") or {}).get("step")
            super()._do(name)

    update.run_update("1.1.0", Spy())
    assert seen["freeze"] == "snapshot"
    assert seen["dump"] == "backup"
    assert seen["install"] == "install"
    assert seen["migrate"] == "migrate"
    assert seen["verify"] == "verify"


def test_rollback_step_is_recorded_before_undo(cfg_dir):
    seen = {}

    class Spy(FakeSteps):
        def restore(self, path):
            seen["restore"] = update.read_state()["in_progress"]["step"]

    update.run_update("1.1.0", Spy(fail="verify"))
    assert seen["restore"] == "rollback"


# ── reconcile ────────────────────────────────────────────────────────────────


def _interrupted(step: str, to: str = "1.1.0") -> None:
    update.write_state({"in_progress": {"from": "1.0.0", "to": to, "step": step, "started_at": "x"}})


def test_reconcile_without_pending_update_does_nothing(cfg_dir):
    steps = FakeSteps()
    assert update.reconcile(steps) == (None, ())
    assert steps.calls == []


@pytest.mark.parametrize("step", ["snapshot", "backup"])
def test_reconcile_before_install_just_records_failure(cfg_dir, step):
    _interrupted(step)
    steps = FakeSteps()
    result, _ = update.reconcile(steps)
    assert steps.calls == []
    assert result["outcome"] == update.FAILED


def test_reconcile_install_interrupted_old_version_on_disk_reinstalls(cfg_dir):
    _interrupted("install")
    steps = FakeSteps()
    result, _ = update.reconcile(steps)
    assert steps.calls == UNDO_PACKAGES
    assert result["outcome"] == update.FAILED


@pytest.mark.parametrize("step", ["install", "migrate", "verify"])
def test_reconcile_target_installed_finishes_the_update(cfg_dir, monkeypatch, step):
    _interrupted(step)
    monkeypatch.setattr(update, "installed_version", lambda: "1.1.0")
    steps = FakeSteps()
    result, children = update.reconcile(steps)
    assert steps.calls == ["start_cluster", "migrate", "verify"]
    assert result["outcome"] == update.OK
    assert children == ("api", "ui")


def test_reconcile_finish_that_fails_verify_rolls_back(cfg_dir, monkeypatch):
    _interrupted("verify")
    monkeypatch.setattr(update, "installed_version", lambda: "1.1.0")
    steps = FakeSteps(fail="verify")
    result, _ = update.reconcile(steps)
    assert steps.calls == ["start_cluster", "migrate", "verify", "restore"] + UNDO_PACKAGES
    assert result["outcome"] == update.ROLLED_BACK


@pytest.mark.parametrize("step", ["migrate", "verify"])
def test_reconcile_partial_package_state_restores_everything(cfg_dir, step):
    # The target is not what is installed (a half-finished pip run): undo fully.
    _interrupted(step)
    steps = FakeSteps()
    result, _ = update.reconcile(steps)
    assert steps.calls == ["restore"] + UNDO_PACKAGES
    assert result["outcome"] == update.ROLLED_BACK


def test_reconcile_interrupted_rollback_repeats_it(cfg_dir, monkeypatch):
    _interrupted("rollback")
    monkeypatch.setattr(update, "installed_version", lambda: "1.1.0")
    steps = FakeSteps()
    result, _ = update.reconcile(steps)
    assert steps.calls == ["restore"] + UNDO_PACKAGES
    assert result["outcome"] == update.ROLLED_BACK


def test_reconcile_twice_is_a_no_op_the_second_time(cfg_dir):
    _interrupted("verify")
    update.reconcile(FakeSteps())
    steps = FakeSteps()
    assert update.reconcile(steps) == (None, ())
    assert steps.calls == []


# ── Requests, window, blockers, availability ─────────────────────────────────


def test_request_update_round_trips_through_the_sentinel(cfg_dir, monkeypatch):
    sentinel = cfg_dir / ".restart_requested"
    monkeypatch.setattr(update, "sentinel_path", lambda: sentinel)
    assert update.update_in_progress() is False
    update.request_update("1.1.0")
    assert update.requested_target(sentinel.read_text()) == "1.1.0"
    assert update.update_in_progress() is True


def test_plain_restart_sentinel_is_not_an_update():
    assert update.requested_target("") is None
    assert update.requested_target("restart") is None
    with pytest.raises(update.UpdateError):
        update.requested_target("update 1.1.0 && evil")


@pytest.mark.parametrize("hour_utc,tz,expected", [
    (20, "Asia/Bangkok", True),    # 03:00 Bangkok
    (21, "Asia/Bangkok", True),    # 04:00 Bangkok
    (22, "Asia/Bangkok", False),   # 05:00 Bangkok
    (19, "Asia/Bangkok", False),   # 02:00 Bangkok
    (3, None, True),
    (3, "Not/AZone", True),        # unknown zone falls back to UTC
    (5, None, False),
])
def test_install_window(hour_utc, tz, expected):
    now = datetime(2026, 9, 26, hour_utc, 30, tzinfo=timezone.utc)
    assert update.in_install_window(now, tz) is expected


def test_blockers_for_a_supervised_pip_install(monkeypatch):
    monkeypatch.setenv("CELERP_SUPERVISED", "1")
    monkeypatch.delenv("CELERP_INSTALL_CHANNEL", raising=False)
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    monkeypatch.setattr(update, "_pip_blocker", lambda: None)
    monkeypatch.setattr(update.os, "access", lambda path, mode: True)
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    assert update.self_update_blockers() == []


def test_blockers_name_each_reason(monkeypatch):
    monkeypatch.delenv("CELERP_SUPERVISED", raising=False)
    monkeypatch.setenv("CELERP_INSTALL_CHANNEL", "electron")
    monkeypatch.setattr(update, "installed_version", lambda: "0.0.0+dev")
    monkeypatch.setattr(update, "_pip_blocker", lambda: "pip_old")
    monkeypatch.setattr(update.os, "access", lambda path, mode: False)
    monkeypatch.setattr(update.Path, "exists", lambda self: str(self) == "/.dockerenv")
    assert update.self_update_blockers() == [
        "channel", "dev_build", "unsupervised", "pip_old", "not_writable", "container"]


def _pip_report(monkeypatch, *, returncode=0, stdout="", exc=None):
    def fake_run(*args, **kwargs):
        if exc:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="boom")
    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")


def test_available_update_reads_pips_resolution(monkeypatch):
    report = {"install": [{"metadata": {"name": "fastapi", "version": "9.9"}},
                          {"metadata": {"name": "celerp", "version": "1.2.0"}}]}
    _pip_report(monkeypatch, stdout=json.dumps(report))
    assert update.available_update() == "1.2.0"


@pytest.mark.parametrize("kwargs", [
    {"stdout": json.dumps({"install": []})},                                   # up to date
    {"stdout": json.dumps({"install": [{"metadata": {"name": "celerp", "version": "0.9"}}]})},
])
def test_available_update_is_none_when_nothing_newer(monkeypatch, kwargs):
    _pip_report(monkeypatch, **kwargs)
    assert update.available_update() is None


@pytest.mark.parametrize("kwargs", [
    {"returncode": 1},                                                           # offline, no index
    {"stdout": "not json"},
    {"stdout": json.dumps({"install": [{"metadata": {"name": "celerp", "version": "x!"}}]})},
    {"exc": subprocess.TimeoutExpired("pip", 1)},
])
def test_available_update_raises_when_it_cannot_tell(monkeypatch, kwargs):
    _pip_report(monkeypatch, **kwargs)
    with pytest.raises(update.UpdateError):
        update.available_update()


def test_stuck_step_fails_instead_of_waiting(monkeypatch):
    """A step that never ends (a stalled pip or migration) raises UpdateError,
    which every step of run_update turns into the undo path above."""
    monkeypatch.setattr(update, "STEP_TIMEOUT_SECONDS", 1)
    with pytest.raises(update.UpdateError, match="did not finish"):
        update._step("-c", "import time; time.sleep(60)")


def test_failed_step_reports_its_output():
    with pytest.raises(update.UpdateError, match="step broke"):
        update._step("-c", "import sys; sys.exit('step broke')")


def test_step_output_outside_the_legacy_code_page_survives(monkeypatch):
    """pip prints characters such as a greater-or-equal sign; a child whose
    output defaults to a narrow code page (Windows) must not fail on them."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    assert update._step("-c", "print('\\u2265 2.0')").strip() == "≥ 2.0"
