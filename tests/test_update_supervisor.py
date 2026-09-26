# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""`celerp start` as the self-update supervisor.

The update steps themselves are covered in test_update_service.py; these tests
pin what the supervisor does with each outcome: hand over to the new version,
keep serving the current one, or stop when an undo failed.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from celerp.services import update

CFG = {
    "server": {"api_port": 8000, "ui_port": 8080},
    "database": {"url": "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"},
    "auth": {"jwt_secret": "test"},
    "modules": {"enabled": []},
}


class _Proc:
    def __init__(self, name, dead=False, code=0):
        self.name = name
        self._dead = dead
        self.returncode = code
        self.terminated = False

    def poll(self):
        return self.returncode if self._dead or self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        pass


class _Handover(Exception):
    pass


def _run_supervisor(tmp_path, *, sentinel_text=None, run_update=None, reconcile=None,
                    max_sleeps=3):
    """Run `_start` with the first API process exiting, leaving `sentinel_text`
    as its restart request; return what happened."""
    from celerp.cli import _start

    events: list[str] = []
    spawned: list[_Proc] = []

    def fake_popen(cmd, env):
        name = "api" if any("celerp.main" in s for s in cmd) else "ui"
        first_api = name == "api" and not any(p.name == "api" for p in spawned)
        proc = _Proc(name, dead=first_api)
        if first_api and sentinel_text is not None:
            (tmp_path / ".restart_requested").write_text(sentinel_text)
        spawned.append(proc)
        events.append(f"spawn:{name}")
        return proc

    sleeps = [0]

    def fake_sleep(n):
        sleeps[0] += 1
        if sleeps[0] > max_sleeps:
            raise SystemExit("still serving")

    steps = MagicMock()
    steps.stop_children.side_effect = lambda children: events.append("stop_children")
    hand_over = MagicMock(side_effect=lambda *a: (events.append("hand_over"), (_ for _ in ()).throw(_Handover()))[1])
    migrate = MagicMock(side_effect=lambda url: events.append("migrate"))

    def _run_update(target, s):
        events.append(f"run_update:{target}")
        return run_update(target, s)

    def _reconcile(s):
        events.append("reconcile")
        return reconcile(s)

    with (
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("celerp.cli._read_config", return_value=CFG),
        patch("celerp.cli._config_to_env", return_value={}),
        patch("celerp.config.config_path", return_value=tmp_path / "config.toml"),
        patch("celerp.cli.time.sleep", side_effect=fake_sleep),
        patch("celerp.cli._migrate_to_head", migrate),
        patch("celerp.cli._wait_ready"),
        patch("celerp.cli._update_steps", return_value=steps),
        patch("celerp.cli._hand_over", hand_over),
        patch.object(update, "run_update", side_effect=_run_update),
        patch.object(update, "reconcile", side_effect=_reconcile),
        patch("signal.signal"),
    ):
        try:
            _start({**CFG, "database": dict(CFG["database"])})
            outcome = None
        except SystemExit as exc:
            outcome = exc.code
        except _Handover:
            outcome = "handed_over"
    return outcome, events, spawned


def _result(outcome, reason=""):
    return {"ok": outcome == update.OK, "outcome": outcome, "from": "1.0.0", "to": "1.1.0",
            "reason": reason}


def test_update_request_installs_then_hands_over(tmp_path):
    new = (_Proc("api"), _Proc("ui"))
    outcome, events, spawned = _run_supervisor(
        tmp_path, sentinel_text="update 1.1.0",
        run_update=lambda t, s: (_result(update.OK), new))
    assert outcome == "handed_over"
    assert events == ["migrate", "spawn:api", "spawn:ui", "run_update:1.1.0",
                      "stop_children", "hand_over"]
    ui = next(p for p in spawned if p.name == "ui")
    assert ui.terminated, "the current UI is stopped before packages change"
    assert not (tmp_path / ".restart_requested").exists()


@pytest.mark.parametrize("result_outcome", [update.FAILED, update.ROLLED_BACK])
def test_failed_update_keeps_serving_the_current_version(tmp_path, result_outcome):
    outcome, events, _ = _run_supervisor(
        tmp_path, sentinel_text="update 1.1.0",
        run_update=lambda t, s: (_result(result_outcome, "install_failed"), ()))
    assert outcome == "still serving"
    assert events == ["migrate", "spawn:api", "spawn:ui", "run_update:1.1.0",
                      "spawn:api", "spawn:ui"]


def test_failed_undo_stops_the_supervisor(tmp_path):
    outcome, events, _ = _run_supervisor(
        tmp_path, sentinel_text="update 1.1.0",
        run_update=lambda t, s: (_result(update.ROLLBACK_FAILED, "verify_failed"), ()))
    assert outcome == 1
    assert events[-1] == "run_update:1.1.0"


@pytest.mark.parametrize("text", ["", "update not-a-version"])
def test_plain_or_invalid_sentinel_only_restarts(tmp_path, text):
    outcome, events, _ = _run_supervisor(tmp_path, sentinel_text=text)
    assert outcome == "still serving"
    assert events == ["migrate", "spawn:api", "spawn:ui", "spawn:api", "spawn:ui"]


def _pending(tmp_path):
    (tmp_path / update.STATE_FILE).write_text(
        '{"in_progress": {"from": "1.0.0", "to": "1.1.0", "step": "verify", "started_at": "x"}}')


@pytest.mark.parametrize("result_outcome", [update.OK, update.FAILED, update.ROLLED_BACK])
def test_interrupted_update_is_resolved_before_migrating(tmp_path, result_outcome):
    _pending(tmp_path)
    outcome, events, _ = _run_supervisor(
        tmp_path, reconcile=lambda s: _result(result_outcome))
    assert outcome == 0  # then serves; the first fake API exits without a sentinel
    assert events[:4] == ["reconcile", "migrate", "spawn:api", "spawn:ui"]


def test_interrupted_update_that_cannot_be_undone_does_not_start(tmp_path):
    _pending(tmp_path)
    outcome, events, _ = _run_supervisor(
        tmp_path, reconcile=lambda s: _result(update.ROLLBACK_FAILED, "verify_failed"))
    assert outcome == 1
    assert events == ["reconcile"]


def test_unreadable_update_record_does_not_start(tmp_path):
    (tmp_path / update.STATE_FILE).write_text("{torn")
    outcome, events, _ = _run_supervisor(tmp_path)
    assert outcome == 1
    assert events == []


def test_second_supervisor_for_the_same_config_does_not_start(tmp_path):
    # Another supervisor or `celerp upgrade` holds the update lock (exclusion
    # itself is covered in test_config_store.py).
    with patch("celerp.config_store.hold_lock", return_value=None) as hold:
        outcome, events, _ = _run_supervisor(tmp_path)
    assert hold.call_args.args[0] == str(tmp_path / "update.lock")
    assert outcome == 1
    assert events == []


def test_supervisor_releases_the_update_lock_when_it_stops(tmp_path):
    _run_supervisor(tmp_path)
    assert not (tmp_path / "update.lock").exists()


def test_request_left_by_a_stopped_supervisor_is_not_acted_on(tmp_path):
    (tmp_path / ".restart_requested").write_text("update 1.1.0")
    outcome, events, _ = _run_supervisor(tmp_path)
    assert outcome == 0
    assert not any(e.startswith("run_update") for e in events)


def test_no_pending_update_skips_reconcile(tmp_path):
    _, events, _ = _run_supervisor(tmp_path)
    assert "reconcile" not in events


# ── hand over ────────────────────────────────────────────────────────────────


def test_hand_over_posix_replaces_the_process(monkeypatch):
    from celerp import cli

    order = []
    steps = MagicMock()
    steps.stop_cluster.side_effect = lambda: order.append("stop_cluster")
    execve = MagicMock(side_effect=lambda *a: (order.append("exec"), (_ for _ in ()).throw(_Handover()))[1])
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.os, "execve", execve)
    monkeypatch.setenv("CELERP_PKG_ROOT", "/old/release")
    with pytest.raises(_Handover):
        cli._hand_over(steps, lambda: order.append("unlock"))
    assert order == ["stop_cluster", "unlock", "exec"]
    exe, argv, env = execve.call_args.args
    assert argv == [cli.sys.executable, "-m", "celerp", "start"]
    assert "CELERP_PKG_ROOT" not in env, "the new supervisor picks its release afresh"


def test_hand_over_windows_waits_on_the_new_supervisor(monkeypatch):
    from celerp import cli

    steps = MagicMock()
    unlock = MagicMock()
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.subprocess, "call", MagicMock(return_value=3))
    monkeypatch.setattr(cli.signal, "signal", MagicMock())
    with pytest.raises(SystemExit) as exc:
        cli._hand_over(steps, unlock)
    assert exc.value.code == 3
    steps.stop_cluster.assert_called_once()
    unlock.assert_called_once()
    assert cli.subprocess.call.call_args.args == ([cli.sys.executable, "-m", "celerp", "start"],)


def test_command_runs_on_the_release_a_self_update_switched_to(tmp_path, monkeypatch):
    from celerp import cli, runtime

    exec_celerp = MagicMock(side_effect=_Handover())
    monkeypatch.setattr(cli, "_exec_celerp", exec_celerp)
    monkeypatch.setattr(runtime, "active", lambda: tmp_path)
    monkeypatch.delenv("CELERP_PKG_ROOT", raising=False)
    monkeypatch.setattr(cli.sys, "argv", ["celerp", "start"])
    with pytest.raises(_Handover):
        cli._run_active_release()
    args, env = exec_celerp.call_args.args
    assert args == ["start"]
    assert env["CELERP_PKG_ROOT"] == str(tmp_path)
    assert env["PYTHONPATH"].split(cli.os.pathsep)[0] == str(tmp_path)

    exec_celerp.reset_mock()
    monkeypatch.setenv("CELERP_PKG_ROOT", str(tmp_path))  # already running it
    cli._run_active_release()
    exec_celerp.assert_not_called()


def test_env_for_another_release_runs_that_release(tmp_path):
    from celerp.cli import _config_to_env

    (tmp_path / "default_modules").mkdir()
    env = _config_to_env({**CFG, "cloud": {"token": ""}}, tmp_path)
    assert env["CELERP_PKG_ROOT"] == str(tmp_path)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)
    assert str(tmp_path / "default_modules") in env["MODULE_DIR"].split(",")


def test_supervised_children_are_marked():
    from celerp.cli import _config_to_env

    env = _config_to_env({**CFG, "cloud": {"token": ""}})
    assert env["CELERP_SUPERVISED"] == "1"


# ── celerp upgrade ───────────────────────────────────────────────────────────


def _upgrade(tmp_path, *, running=False, blockers=(), target="1.1.0", result=None, children=()):
    from click.testing import CliRunner

    from celerp.cli import main

    steps = MagicMock()
    run_update = MagicMock(return_value=(result or _result(update.OK), children))
    with (
        patch("celerp.cli._read_config", return_value=CFG),
        patch("celerp.cli._config_to_env", return_value={}),
        patch("celerp.cli.ensure_database"),
        patch("celerp.cli._update_steps", return_value=steps),
        patch("celerp.config.config_path", return_value=tmp_path / "config.toml"),
        patch.object(update, "get_json", return_value={"status": "ok"} if running else None),
        patch.object(update, "self_update_blockers", return_value=list(blockers)),
        patch.object(update, "available_update",
                     **({"side_effect": target} if isinstance(target, Exception) else {"return_value": target})),
        patch.object(update, "installed_version", return_value="1.0.0"),
        patch.object(update, "run_update", run_update),
    ):
        res = CliRunner().invoke(main, ["upgrade"])
    run_update.steps = steps
    return res, run_update


def test_upgrade_verifies_then_stops_the_new_version(tmp_path):
    children = (_Proc("api"), _Proc("ui"))
    res, run_update = _upgrade(tmp_path, children=children)
    assert res.exit_code == 0, res.output
    assert "Upgraded to 1.1.0" in res.output
    assert run_update.call_args.args[0] == "1.1.0"
    assert run_update.call_args.kwargs == {}
    run_update.steps.stop_children.assert_called_once_with(children)
    run_update.steps.stop_cluster.assert_called_once()
    assert not (tmp_path / "update.lock").exists()


def test_upgrade_refuses_while_celerp_is_running(tmp_path):
    res, run_update = _upgrade(tmp_path, running=True)
    assert res.exit_code == 1
    assert "Stop it first" in res.output
    run_update.assert_not_called()


@pytest.mark.parametrize("setup", ["pending", "requested", "unreadable", "locked"])
def test_upgrade_refuses_while_another_update_may_run(tmp_path, setup):
    held = patch("celerp.config_store.hold_lock", return_value=None) if setup == "locked" else None
    if setup == "pending":
        _pending(tmp_path)
    elif setup == "requested":
        (tmp_path / ".restart_requested").write_text("update 1.1.0")
    elif setup == "unreadable":
        (tmp_path / update.STATE_FILE).write_text("[]")
    if held:
        held.start()
    try:
        with patch.object(update, "sentinel_path", return_value=tmp_path / ".restart_requested"):
            res, run_update = _upgrade(tmp_path)
    finally:
        if held:
            held.stop()
    assert res.exit_code == 1
    run_update.assert_not_called()


@pytest.mark.parametrize("code", update.PIP_BLOCKERS)
def test_upgrade_refuses_when_pip_cannot_install(tmp_path, code):
    res, run_update = _upgrade(tmp_path, blockers=[code])
    assert res.exit_code == 1
    assert "Cannot upgrade" in res.output
    run_update.assert_not_called()


def test_upgrade_ignores_blockers_that_only_apply_in_app(tmp_path):
    res, run_update = _upgrade(tmp_path, blockers=["unsupervised", "dev_build"])
    assert res.exit_code == 0, res.output
    run_update.assert_called_once()


def test_upgrade_with_nothing_newer_changes_nothing(tmp_path):
    res, run_update = _upgrade(tmp_path, target=None)
    assert res.exit_code == 0
    assert "No newer version found (installed: 1.0.0)" in res.output
    run_update.assert_not_called()


def test_upgrade_failure_exits_nonzero(tmp_path):
    res, _ = _upgrade(tmp_path, result=_result(update.FAILED, "install_failed"))
    assert res.exit_code == 1
    assert update.reason_text("install_failed") in res.output


def test_upgrade_whose_undo_failed_says_the_database_needs_restoring(tmp_path):
    res, _ = _upgrade(tmp_path, result=_result(update.ROLLBACK_FAILED, "verify_failed"))
    assert res.exit_code == 1
    assert "could not be restored" in res.output


def test_upgrade_that_cannot_check_exits_nonzero(tmp_path):
    res, run_update = _upgrade(
        tmp_path, target=update.UpdateError("could not reach the package index"))
    assert res.exit_code == 1
    assert "could not reach the package index" in res.output
    run_update.assert_not_called()
