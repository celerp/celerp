# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Windows restricted launcher passes the program its arguments and exit code intact,
and fails with the line the desktop app recognises."""
import os
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


# The private folder holds Postgres's paths when the user's own folder has letters
# Postgres cannot read; another account must not be able to make or reach it.
def test_the_private_folder_is_the_users_own_and_closed_to_other_accounts():
    sid = "S-1-5-21-1-2-3-1001"
    assert launch.private_dir_sddl(sid) == (
        f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")


@pytest.mark.parametrize("attributes, owner, problem", [
    (launch.FILE_ATTRIBUTE_DIRECTORY, "S-1-5-21-9", None),
    (launch.FILE_ATTRIBUTE_DIRECTORY, "S-1-5-21-8", "belongs to another account (S-1-5-21-8)"),
    (launch.FILE_ATTRIBUTE_DIRECTORY | launch.FILE_ATTRIBUTE_REPARSE_POINT, None, "is a link to another place"),
    (0x20, None, "is not a folder"),  # FILE_ATTRIBUTE_ARCHIVE: a file
    (launch.INVALID_FILE_ATTRIBUTES, None, "is not a folder"),
])
def test_a_private_folder_found_in_place_is_used_only_when_it_is_the_users_own(attributes, owner, problem):
    assert launch.private_dir_problem(attributes, owner, "S-1-5-21-9") == problem


def test_the_private_folder_flag_prints_its_path(monkeypatch, capsys):
    monkeypatch.setattr(launch, "private_dir", lambda parent, prefix: f"{parent}\\{prefix}S-1-5-21-9")
    assert launch.main(["--private-dir", "C:\\ProgramData", "Celerp-"]) == 0
    assert capsys.readouterr().out == "C:\\ProgramData\\Celerp-S-1-5-21-9\n"


def test_a_private_folder_that_cannot_be_used_is_a_recognised_failure(monkeypatch, capsys):
    def refuse(parent, prefix):
        raise launch.LaunchError("C:\\ProgramData\\Celerp-S-1 belongs to another account (S-1-5-21-8)")

    monkeypatch.setattr(launch, "private_dir", refuse)
    assert launch.main(["--private-dir", "C:\\ProgramData", "Celerp-"]) == 1
    assert capsys.readouterr().err == (
        "win_restricted_launch: C:\\ProgramData\\Celerp-S-1 belongs to another account (S-1-5-21-8)\n")


def test_the_app_asks_for_the_private_folder_the_launcher_makes():
    assert '"--private-dir"' in (ROOT / "electron" / "app-main.js").read_text()


def _private_dir(parent) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", launch.__file__, "--private-dir", str(parent), "Celerp-"],
                          capture_output=True, text=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows security descriptors")
def test_the_private_folder_is_made_once_owned_by_the_user_and_closed_to_others(tmp_path):
    first = _private_dir(tmp_path)
    assert first.returncode == 0, first.stderr
    folder = Path(first.stdout.strip())
    assert folder.parent == tmp_path and folder.name.startswith("Celerp-S-1-5-")
    (folder / "kept").write_text("ok")
    again = _private_dir(tmp_path)
    assert (again.returncode, again.stdout) == (0, first.stdout)
    assert (folder / "kept").read_text() == "ok"
    acl = subprocess.run(["icacls", str(folder)], capture_output=True, text=True).stdout
    # Only the user, SYSTEM and Administrators, none of it inherited from the parent.
    assert "BUILTIN\\Users" not in acl and "Everyone" not in acl and "(I)" not in acl, acl
    # .NET directly: Get-Acl's module does not load in Windows PowerShell started from a pwsh session.
    owner = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"[System.IO.Directory]::GetAccessControl('{folder}').GetOwner("
         "[System.Security.Principal.SecurityIdentifier]).Value"],
        capture_output=True, text=True)
    user_sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True).stdout.strip().split(",")[-1].strip('"')
    assert owner.stdout.strip() == user_sid == folder.name.removeprefix("Celerp-"), owner.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows security descriptors")
def test_a_private_folder_other_accounts_can_open_is_refused_and_left_as_found(tmp_path):
    folder = Path(_private_dir(tmp_path).stdout.strip())
    (folder / "postgres").mkdir()
    (folder / "postgres" / "PG_VERSION").write_text("17")
    # Every user of the PC may read the folder and the files in it.
    subprocess.run(["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)R", "/t"], check=True, capture_output=True)
    refused = _private_dir(tmp_path)
    assert refused.returncode == 1
    assert refused.stderr == f"{launch.FAILURE_PREFIX} {folder} can be opened by other accounts\n"
    assert (folder / "postgres" / "PG_VERSION").read_text() == "17"
    # Closed again, it is used as it is.
    subprocess.run(["icacls", str(folder), "/remove:g", "*S-1-5-32-545", "/t"], check=True, capture_output=True)
    again = _private_dir(tmp_path)
    assert (again.returncode, again.stdout.strip()) == (0, str(folder)), again.stderr
    assert (folder / "postgres" / "PG_VERSION").read_text() == "17"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_a_link_or_file_in_place_of_the_private_folder_is_refused(tmp_path):
    folder = Path(_private_dir(tmp_path).stdout.strip())
    folder.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(folder), str(elsewhere)], check=True, capture_output=True)
    linked = _private_dir(tmp_path)
    assert linked.returncode == 1
    assert linked.stderr == f"{launch.FAILURE_PREFIX} {folder} is a link to another place\n"
    os.rmdir(folder)  # removes the link only
    folder.write_text("")
    filed = _private_dir(tmp_path)
    assert filed.returncode == 1
    assert filed.stderr == f"{launch.FAILURE_PREFIX} {folder} is not a folder\n"
