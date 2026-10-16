# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Windows restricted launcher passes the program its arguments and exit code intact,
and fails with the line the desktop app recognises."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from celerp import win_restricted_launch as launch

ROOT = Path(__file__).resolve().parent.parent


def test_command_line_quotes_paths_with_spaces():
    argv = [r"C:\Users\a b\AppData\Local\Programs\Celerp\postgres.exe", "-D", r"C:\Users\a b\data", "-p", "5432"]
    assert launch.command_line(argv) == (
        r'"C:\Users\a b\AppData\Local\Programs\Celerp\postgres.exe" -D "C:\Users\a b\data" -p 5432')


def test_command_line_keeps_quotes_and_trailing_backslashes():
    assert launch.command_line(["prog.exe", 'say "hi"', "C:\\dir with space\\"]) == (
        r'prog.exe "say \"hi\"" "C:\dir with space\\"')


def test_windows_exit_codes_pass_through_as_signed_ints():
    assert launch.exit_status(0) == 0
    assert launch.exit_status(1) == 1
    # STATUS_DLL_NOT_FOUND: sys.exit(-1073741515) ends the process with 0xC0000135.
    assert launch.exit_status(0xC0000135) == -1073741515


def test_main_returns_the_program_exit_code(monkeypatch):
    seen = []
    monkeypatch.setattr(launch, "run_restricted", lambda argv: seen.append(argv) or 0xC0000135)
    assert launch.main(["postgres.exe", "-D", "data"]) == -1073741515
    assert seen == [["postgres.exe", "-D", "data"]]


def test_a_failed_windows_call_writes_the_recognised_line(monkeypatch, capsys):
    def fail(argv):
        raise launch.LaunchError("CreateRestrictedToken failed (error 5)")

    monkeypatch.setattr(launch, "run_restricted", fail)
    assert launch.main(["postgres.exe"]) == 1
    assert capsys.readouterr().err == "win_restricted_launch: CreateRestrictedToken failed (error 5)\n"


@pytest.mark.parametrize("is_admin, elevation_type, state", [
    (False, launch.TOKEN_ELEVATION_DEFAULT, "standard"),
    (False, launch.TOKEN_ELEVATION_LIMITED, "standard"),
    # UAC on, Run as administrator: a standard token sits behind this one.
    (True, launch.TOKEN_ELEVATION_FULL, "elevated"),
    # UAC off, or the built-in Administrator: no standard token to go back to.
    (True, launch.TOKEN_ELEVATION_DEFAULT, "admin"),
])
def test_token_state_from_admin_membership_and_elevation_type(is_admin, elevation_type, state):
    assert launch.classify(is_admin, elevation_type) == state


def test_the_token_state_flag_prints_the_state(monkeypatch, capsys):
    monkeypatch.setattr(launch, "token_state", lambda: "admin")
    assert launch.main(["--token-state"]) == 0
    assert capsys.readouterr().out == "admin\n"


def test_a_token_that_cannot_be_read_is_a_recognised_failure(monkeypatch, capsys):
    def fail():
        raise launch.LaunchError("GetTokenInformation failed (error 5)")

    monkeypatch.setattr(launch, "token_state", fail)
    assert launch.main(["--token-state"]) == 1
    assert capsys.readouterr().err.startswith(launch.FAILURE_PREFIX)


def test_the_app_asks_for_the_token_state_the_launcher_answers():
    app_main = (ROOT / "electron" / "app-main.js").read_text()
    assert '"--token-state"' in app_main
    deelevation = (ROOT / "electron" / "deelevation.js").read_text()
    for state in ("standard", "elevated", "admin"):
        assert f'"{state}"' in deelevation


def test_no_program_is_a_failure(capsys):
    assert launch.main([]) == 1
    assert capsys.readouterr().err.startswith(launch.FAILURE_PREFIX)


def test_the_app_recognises_the_failure_prefix():
    errors = (ROOT / "electron" / "windows-start-errors.js").read_text()
    assert launch.FAILURE_PREFIX in errors


@pytest.mark.skipif(sys.platform != "win32", reason="restricted tokens are a Windows API")
def test_runs_the_program_without_administrator_rights():
    # Both sides: this process holds administrator rights and the program does not.
    assert launch.token_state() in ("elevated", "admin")
    # Probes run with -c: a file under pytest's tmp_path is readable by its owner
    # only, which for an administrator is the Administrators group the program loses.
    probe = "import ctypes, sys; sys.stdout.write(str(ctypes.windll.shell32.IsUserAnAdmin())); sys.exit(7)"
    result = subprocess.run([sys.executable, "-I", launch.__file__, sys.executable, "-I", "-c", probe],
                            capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout == "0"
    restricted = subprocess.run([sys.executable, "-I", launch.__file__, sys.executable, "-I", launch.__file__,
                                 "--token-state"], capture_output=True, text=True)
    assert restricted.stdout == "standard\n"


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are a Windows API")
def test_the_program_ends_when_the_launcher_is_killed():
    # The app stops a server by killing the process it started, which here is the
    # launcher: the program it runs must not outlive it and keep the server's port.
    probe = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
    launcher = subprocess.Popen([sys.executable, "-I", launch.__file__, sys.executable, "-I", "-c", probe],
                                stdout=subprocess.PIPE, text=True)
    pid = int(launcher.stdout.readline())
    launcher.kill()
    launcher.wait(timeout=10)
    deadline = time.monotonic() + 10
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _running(pid)


def _running(pid: int) -> bool:
    listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in listed


@pytest.mark.skipif(sys.platform != "win32", reason="restricted tokens are a Windows API")
def test_the_program_can_use_the_private_folders_it_creates():
    # The servers run restricted and make owner-only temp folders (tempfile.mkdtemp,
    # used by backup and restore); the restricted token must own what it creates.
    probe = ("import os, tempfile; d = tempfile.mkdtemp(); p = os.path.join(d, 'x');"
             " open(p, 'w').write('ok'); print(open(p).read())")
    result = subprocess.run([sys.executable, "-I", launch.__file__, sys.executable, "-I", "-c", probe],
                            capture_output=True, text=True)
    assert (result.returncode, result.stdout) == (0, "ok\n"), result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="restricted tokens are a Windows API")
def test_the_program_can_start_programs_of_its_own_and_read_their_output():
    # Postgres checks its own binary at start by running "postgres -V" through the
    # shell and reading the pipe. Started from an administrator's token, the program
    # must still be able to do that, or Postgres stops with "Permission denied".
    probe = ("import subprocess, sys;"
             " print(subprocess.check_output(f'\"{sys.executable}\" -I -c \"print(5)\"', shell=True, text=True), end='')")
    result = subprocess.run([sys.executable, "-I", launch.__file__, sys.executable, "-I", "-c", probe],
                            capture_output=True, text=True)
    assert (result.returncode, result.stdout) == (0, "5\n"), result.stderr
