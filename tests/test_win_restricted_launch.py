# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Windows restricted launcher passes the program its arguments and exit code intact,
and fails with the line the desktop app recognises."""
import subprocess
import sys
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


def test_no_program_is_a_failure(capsys):
    assert launch.main([]) == 1
    assert capsys.readouterr().err.startswith(launch.FAILURE_PREFIX)


def test_the_app_recognises_the_failure_prefix():
    errors = (ROOT / "electron" / "windows-start-errors.js").read_text()
    assert launch.FAILURE_PREFIX in errors


@pytest.mark.skipif(sys.platform != "win32", reason="restricted tokens are a Windows API")
def test_runs_the_program_without_administrator_rights(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("import ctypes, sys\nsys.stdout.write(str(ctypes.windll.shell32.IsUserAnAdmin()))\nsys.exit(7)\n")
    result = subprocess.run([sys.executable, "-I", launch.__file__, sys.executable, "-I", str(probe)],
                            capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout == "0"
