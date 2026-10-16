# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Runs a program on Windows with this process's administrator rights removed.

Postgres refuses to start under an administrator token. The desktop app normally
avoids one by relaunching itself through Explorer, but with User Account Control
turned off (or as the built-in Administrator) Explorer is elevated too. The app
then starts postgres through this script, which does what pg_ctl does for the
same reason: it makes a restricted copy of its own token with the Administrators
and Power Users groups set to deny-only and every privilege but change-notify
removed, starts the program with that token and the same standard handles, waits,
and exits with the program's exit code.

  python -I win_restricted_launch.py PROGRAM ARGS...

The app also asks it what kind of token it was started with, read straight from
the token rather than from any Windows service:

  python -I win_restricted_launch.py --token-state

prints "standard" (no administrator rights), "elevated" (administrator, with a
standard token behind it: UAC on, Run as administrator) or "admin"
(administrator with nothing behind it: UAC off, or the built-in Administrator).

On failure it writes one line starting with FAILURE_PREFIX to stderr and exits 1;
the app recognises that prefix (electron/windows-start-errors.js).
"""
import ctypes
import subprocess
import sys

FAILURE_PREFIX = "win_restricted_launch:"

ADMINISTRATORS = "S-1-5-32-544"
POWER_USERS = "S-1-5-32-547"

DISABLE_MAX_PRIVILEGE = 0x1
TOKEN_ALL_ACCESS = 0xF01FF  # as pg_ctl opens it
HANDLE_FLAG_INHERIT = 0x1
STARTF_USESTDHANDLES = 0x100
STD_HANDLES = (-10, -11, -12)  # input, output, error
INFINITE = 0xFFFFFFFF
TOKEN_QUERY = 0x8
TOKEN_ELEVATION_TYPE_CLASS = 18  # TOKEN_INFORMATION_CLASS TokenElevationType
TOKEN_ELEVATION_DEFAULT = 1
TOKEN_ELEVATION_FULL = 2
TOKEN_ELEVATION_LIMITED = 3
WAIT_FAILED = 0xFFFFFFFF


class LaunchError(Exception):
    """A Windows call failed; the message names the call and its error code."""


def command_line(argv: list[str]) -> str:
    """The Windows command line that hands `argv` to the program unchanged."""
    return subprocess.list2cmdline(argv)


def exit_status(code: int) -> int:
    """A Windows exit code (an unsigned 32-bit value) as the signed int sys.exit passes on intact."""
    return ctypes.c_int32(code).value


def classify(is_admin: bool, elevation_type: int) -> str:
    """The token state the app acts on: "standard", "elevated" or "admin"."""
    if not is_admin:
        return "standard"
    return "elevated" if elevation_type == TOKEN_ELEVATION_FULL else "admin"


def _api():
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
        ]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    signatures = {
        (kernel32, "GetCurrentProcess"): (wintypes.HANDLE, []),
        (kernel32, "GetStdHandle"): (wintypes.HANDLE, [wintypes.DWORD]),
        (kernel32, "SetHandleInformation"): (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]),
        (kernel32, "WaitForSingleObject"): (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        (kernel32, "GetExitCodeProcess"): (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
        (kernel32, "CloseHandle"): (wintypes.BOOL, [wintypes.HANDLE]),
        (kernel32, "LocalFree"): (ctypes.c_void_p, [ctypes.c_void_p]),
        (advapi32, "CheckTokenMembership"): (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]),
        (advapi32, "GetTokenInformation"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
        (advapi32, "OpenProcessToken"): (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]),
        (advapi32, "ConvertStringSidToSidW"): (wintypes.BOOL, [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]),
        (advapi32, "CreateRestrictedToken"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SidAndAttributes),
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]),
        (advapi32, "CreateProcessAsUserW"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInformation)]),
    }
    for (dll, name), (restype, argtypes) in signatures.items():
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = restype, argtypes
    return kernel32, advapi32, wintypes, SidAndAttributes, StartupInfo, ProcessInformation


def _check(ok, call: str) -> None:
    if not ok:
        raise LaunchError(f"{call} failed (error {ctypes.get_last_error()})")


def _sids(advapi32, sids: list) -> None:
    """Append the Administrators and Power Users SIDs to `sids`; the caller frees each
    with LocalFree, including any appended before a failure."""
    for text in (ADMINISTRATORS, POWER_USERS):
        sid = ctypes.c_void_p()
        _check(advapi32.ConvertStringSidToSidW(text, ctypes.byref(sid)), "ConvertStringSidToSidW")
        sids.append(sid)


def token_state() -> str:
    """This process's token state. Administrator means a member of the groups Postgres
    checks (pgwin32_is_admin): Administrators or Power Users."""
    kernel32, advapi32, wintypes, *_ = _api()
    sids = []
    try:
        _sids(advapi32, sids)
        is_admin = False
        for sid in sids:
            member = wintypes.BOOL()
            # A null token checks this thread's effective token.
            _check(advapi32.CheckTokenMembership(None, sid, ctypes.byref(member)), "CheckTokenMembership")
            is_admin = is_admin or bool(member.value)
    finally:
        for sid in sids:
            kernel32.LocalFree(sid)

    token = wintypes.HANDLE()
    _check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)),
           "OpenProcessToken")
    try:
        elevation_type = wintypes.DWORD()
        size = wintypes.DWORD()
        _check(advapi32.GetTokenInformation(token, TOKEN_ELEVATION_TYPE_CLASS, ctypes.byref(elevation_type),
                                            ctypes.sizeof(elevation_type), ctypes.byref(size)),
               "GetTokenInformation")
    finally:
        kernel32.CloseHandle(token)
    return classify(is_admin, elevation_type.value)


def run_restricted(argv: list[str]) -> int:
    """Start `argv` with the restricted token, wait for it, and return its exit code."""
    kernel32, advapi32, wintypes, SidAndAttributes, StartupInfo, ProcessInformation = _api()

    token = wintypes.HANDLE()
    _check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ALL_ACCESS, ctypes.byref(token)),
           "OpenProcessToken")
    sids = []
    try:
        _sids(advapi32, sids)
        disable = (SidAndAttributes * len(sids))(*(SidAndAttributes(s, 0) for s in sids))
        restricted = wintypes.HANDLE()
        _check(advapi32.CreateRestrictedToken(token, DISABLE_MAX_PRIVILEGE, len(sids), disable,
                                              0, None, 0, None, ctypes.byref(restricted)),
               "CreateRestrictedToken")
    finally:
        for sid in sids:
            kernel32.LocalFree(sid)
        kernel32.CloseHandle(token)

    try:
        startup = StartupInfo(cb=ctypes.sizeof(StartupInfo), dwFlags=STARTF_USESTDHANDLES)
        handles = [kernel32.GetStdHandle(n) for n in STD_HANDLES]
        for handle in handles:
            # The program writes to the same pipes the app reads from this script.
            kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        process = ProcessInformation()
        line = ctypes.create_unicode_buffer(command_line(argv))
        _check(advapi32.CreateProcessAsUserW(restricted, argv[0], line, None, None, True, 0, None, None,
                                             ctypes.byref(startup), ctypes.byref(process)),
               "CreateProcessAsUserW")
    finally:
        kernel32.CloseHandle(restricted)

    try:
        _check(kernel32.WaitForSingleObject(process.hProcess, INFINITE) != WAIT_FAILED, "WaitForSingleObject")
        code = wintypes.DWORD()
        _check(kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(code)), "GetExitCodeProcess")
        return code.value
    finally:
        kernel32.CloseHandle(process.hThread)
        kernel32.CloseHandle(process.hProcess)


def main(argv: list[str]) -> int:
    if not argv:
        print(f"{FAILURE_PREFIX} usage: win_restricted_launch.py PROGRAM ARGS...", file=sys.stderr)
        return 1
    try:
        if argv == ["--token-state"]:
            print(token_state())
            return 0
        return exit_status(run_restricted(argv))
    except LaunchError as e:
        print(f"{FAILURE_PREFIX} {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
