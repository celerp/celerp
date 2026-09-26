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

from celerp import runtime
from celerp.services import update


class FakeSteps(update.Steps):
    def __init__(self, fail: str | None = None, fail_times: int = 1):
        self.calls: list[str] = []
        self.fail = fail
        self.fail_times = fail_times

    def _do(self, name: str, *args):
        self.calls.append(name)
        if self.fail == name and self.fail_times > 0:
            self.fail_times -= 1
            raise update.UpdateError(f"{name} broke")

    def dump(self, path):
        self._do("dump")
        path.write_bytes(b"DUMP")

    def stage(self, target):
        self._do("stage")
        runtime.release_dir(target).mkdir(parents=True)

    def migrate(self, target): self._do("migrate")
    def restore(self, path): self._do("restore")
    def stop_children(self, children): self._do("stop_children")

    def verify(self, target):
        self._do("verify")
        return ("api", "ui")


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    return tmp_path


FORWARD = ["dump", "stage", "migrate", "verify"]


def _staged() -> list[str]:
    root = runtime.release_dir("x").parent
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def test_success_runs_steps_in_order_then_switches(cfg_dir):
    steps = FakeSteps()
    result, children = update.run_update("1.1.0", steps)
    assert steps.calls == FORWARD
    assert children == ("api", "ui")
    assert result["outcome"] == update.OK and result["ok"] is True
    assert runtime.pointed() == "1.1.0"
    state = update.read_state()
    assert "in_progress" not in state
    assert state["last_result"]["to"] == "1.1.0"
    assert state["last_result"]["notified"] is False
    assert state.get("failed_versions", []) == []


def test_success_prunes_releases_other_than_old_and_new(cfg_dir):
    runtime.release_dir("0.9.0").mkdir(parents=True)
    runtime.staging_dir("1.0.5").mkdir(parents=True)
    update.run_update("1.1.0", FakeSteps())
    assert _staged() == ["1.1.0", "current"]


def test_backup_failure_changes_nothing(cfg_dir):
    steps = FakeSteps(fail="dump")
    result, _ = update.run_update("1.1.0", steps)
    assert steps.calls == ["dump"]
    assert result["outcome"] == update.FAILED
    assert result["reason"] == "backup_failed"
    assert runtime.pointed() is None
    assert update.read_state()["failed_versions"] == ["1.1.0"]


def test_install_failure_discards_the_staged_release_without_touching_database(cfg_dir):
    class Partial(FakeSteps):
        def stage(self, target):
            runtime.staging_dir(target).mkdir(parents=True)
            super().stage(target)

    steps = Partial(fail="stage")
    result, _ = update.run_update("1.1.0", steps)
    assert steps.calls == ["dump", "stage"]
    assert result["outcome"] == update.FAILED
    assert result["reason"] == "install_failed"
    assert runtime.pointed() is None and _staged() == []


@pytest.mark.parametrize("step", ["migrate", "verify"])
def test_failure_after_install_restores_database_and_keeps_old_release(cfg_dir, step):
    steps = FakeSteps(fail=step)
    result, children = update.run_update("1.1.0", steps)
    assert steps.calls == FORWARD[:FORWARD.index(step) + 1] + ["restore"]
    assert children == ()
    assert result["outcome"] == update.ROLLED_BACK
    assert result["reason"] == f"{step}_failed"
    assert runtime.pointed() is None and _staged() == []
    assert update.read_state()["failed_versions"] == ["1.1.0"]


def test_switch_failure_stops_the_new_version_and_restores(cfg_dir, monkeypatch):
    def broken_switch(version):
        raise OSError("disk full")

    monkeypatch.setattr(update.runtime, "switch", broken_switch)
    steps = FakeSteps()
    result, children = update.run_update("1.1.0", steps)
    assert steps.calls == FORWARD + ["stop_children", "restore"]
    assert children == ()
    assert result["outcome"] == update.ROLLED_BACK


def test_failed_rollback_stays_in_progress_and_keeps_the_backup(cfg_dir):
    steps = FakeSteps(fail="verify")
    steps.restore = lambda path: (_ for _ in ()).throw(RuntimeError("restore broke"))
    result, _ = update.run_update("1.1.0", steps)
    assert result["outcome"] == update.ROLLBACK_FAILED
    assert result["reason"] == "verify_failed"
    assert update.dump_path().exists()
    pending = update.read_state()["in_progress"]
    assert pending["step"] == "rollback" and pending["reason"] == "verify_failed"
    assert update.update_in_progress() is True


def test_failed_rollback_is_retried_by_reconcile(cfg_dir):
    steps = FakeSteps(fail="verify")
    steps.restore = lambda path: (_ for _ in ()).throw(RuntimeError("restore broke"))
    update.run_update("1.1.0", steps)
    retry = FakeSteps()
    result = update.reconcile(retry)
    assert retry.calls == ["restore"]
    assert result["outcome"] == update.ROLLED_BACK
    assert result["reason"] == "verify_failed"
    assert "in_progress" not in update.read_state()


def test_error_detail_is_logged_not_recorded(cfg_dir, caplog):
    class Leaky(FakeSteps):
        def migrate(self, target):
            raise update.UpdateError("password=hunter2 at /srv/secret")

    result, _ = update.run_update("1.1.0", Leaky())
    assert result["reason"] == "migrate_failed"
    assert "hunter2" not in json.dumps(update.read_state())
    assert "hunter2" in caplog.text


def test_failed_versions_are_not_duplicated(cfg_dir):
    update.run_update("1.1.0", FakeSteps(fail="stage"))
    update.run_update("1.1.0", FakeSteps(fail="stage"))
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
    assert seen == {"dump": "backup", "stage": "install", "migrate": "migrate", "verify": "verify"}


def test_rollback_step_is_recorded_before_undo(cfg_dir):
    seen = {}

    class Spy(FakeSteps):
        def restore(self, path):
            seen["restore"] = update.read_state()["in_progress"]["step"]

    update.run_update("1.1.0", Spy(fail="verify"))
    assert seen["restore"] == "rollback"


# ── Update record that cannot be read ─────────────────────────────────────────


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", ""])
def test_unreadable_state_fails_closed(cfg_dir, body):
    (cfg_dir / update.STATE_FILE).write_text(body)
    with pytest.raises(update.UpdateStateError):
        update.read_state()
    steps = FakeSteps()
    with pytest.raises(update.UpdateStateError):
        update.run_update("1.1.0", steps)
    with pytest.raises(update.UpdateStateError):
        update.reconcile(steps)
    assert steps.calls == []
    assert update.update_in_progress() is True


def test_missing_state_is_no_update(cfg_dir):
    assert update.read_state() == {}


# ── reconcile ────────────────────────────────────────────────────────────────


def _interrupted(step: str, to: str = "1.1.0") -> None:
    update.write_state({"in_progress": {"from": "1.0.0", "to": to, "step": step, "started_at": "x"}})


def test_reconcile_without_pending_update_does_nothing(cfg_dir):
    steps = FakeSteps()
    assert update.reconcile(steps) is None
    assert steps.calls == []


@pytest.mark.parametrize("step", ["backup", "install"])
def test_reconcile_before_migrate_discards_the_staged_release(cfg_dir, step):
    _interrupted(step)
    runtime.staging_dir("1.1.0").mkdir(parents=True)
    steps = FakeSteps()
    result = update.reconcile(steps)
    assert steps.calls == []
    assert result["outcome"] == update.FAILED and result["reason"] == "interrupted"
    assert _staged() == []


@pytest.mark.parametrize("step", ["migrate", "verify", "rollback"])
def test_reconcile_after_migrate_started_restores_the_database(cfg_dir, step):
    _interrupted(step)
    runtime.release_dir("1.1.0").mkdir(parents=True)
    steps = FakeSteps()
    result = update.reconcile(steps)
    assert steps.calls == ["restore"]
    assert result["outcome"] == update.ROLLED_BACK and result["reason"] == "interrupted"
    assert _staged() == []


def test_reconcile_after_the_switch_finishes_the_update(cfg_dir, monkeypatch):
    # Stopped between the switch and recording the outcome: this start already
    # runs the target, so the update is done and nothing is undone.
    _interrupted("verify")
    runtime.release_dir("1.1.0").mkdir(parents=True)
    runtime.switch("1.1.0")
    monkeypatch.setattr(update, "installed_version", lambda: "1.1.0")
    steps = FakeSteps()
    result = update.reconcile(steps)
    assert steps.calls == []
    assert result["outcome"] == update.OK
    assert runtime.pointed() == "1.1.0"


def test_reconcile_twice_is_a_no_op_the_second_time(cfg_dir):
    _interrupted("verify")
    update.reconcile(FakeSteps())
    steps = FakeSteps()
    assert update.reconcile(steps) is None
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


def test_failed_step_logs_its_output_and_raises_only_the_exit_code(caplog):
    with pytest.raises(update.UpdateError) as exc:
        update._step("-c", "import sys; sys.stderr.write('se' + 'cret'); sys.exit(3)")
    assert "secret" not in str(exc.value) and str(exc.value).endswith("exited 3")
    assert "secret" in caplog.text


def test_step_output_outside_the_legacy_code_page_survives(monkeypatch):
    """pip prints characters such as a greater-or-equal sign; a child whose
    output defaults to a narrow code page (Windows) must not fail on them."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    assert update._step("-c", "print('\\u2265 2.0')").strip() == "≥ 2.0"
