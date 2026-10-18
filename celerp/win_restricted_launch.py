# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Runs a program on Windows with this process's administrator rights removed.

Postgres refuses to start under an administrator token. The desktop app normally
avoids one by relaunching itself through Explorer, but with User Account Control
turned off (or as the built-in Administrator) Explorer is elevated too. The app
then starts postgres through this script, which does what pg_ctl does for the
same reason: it makes a restricted copy of its own token with the Administrators
and Power Users groups set to deny-only and every privilege but change-notify
removed, adds the user to that token's default access list (an administrator's
grants only Administrators, so the program could not reach the processes and pipes
it creates, and Postgres fails its "postgres -V" self-check), starts the program with that token and the same standard handles, waits,
and exits with the program's exit code. Like pg_ctl, it puts the program in a job
object that ends it when this script ends, so killing the script (the process the
app started) stops the program too.

  python -I win_restricted_launch.py PROGRAM ARGS...

The app also asks it what kind of token it was started with, read straight from
the token rather than from any Windows service:

  python -I win_restricted_launch.py --token-state

prints "standard" (no administrator rights), "elevated" (administrator, with a
standard token behind it: UAC on, Run as administrator) or "admin"
(administrator with nothing behind it: UAC off, or the built-in Administrator).

It also makes the folder the app keeps Postgres paths in when the user's own
folder has letters Postgres cannot read:

  python -I win_restricted_launch.py --private-dir PARENT PREFIX

creates PARENT\\PREFIX<user SID> owned by the token's user and open only to that
user, SYSTEM and Administrators, or checks that the folder already there is a
real folder owned by that user and open to no one else, and prints its path. A
folder another account made in its place, or one other accounts can open, is
refused, never used.

On failure it writes one line starting with FAILURE_PREFIX to stderr and exits 1;
the app recognises that prefix (electron/windows-start-errors.js).
"""
import ctypes
import re
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
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS TokenUser
TOKEN_DEFAULT_DACL_CLASS = 6  # TOKEN_INFORMATION_CLASS TokenDefaultDacl
TOKEN_ELEVATION_TYPE_CLASS = 18  # TOKEN_INFORMATION_CLASS TokenElevationType
GENERIC_ALL = 0x10000000
GRANT_ACCESS = 1
NO_INHERITANCE = 0
TRUSTEE_IS_SID = 0
TRUSTEE_IS_USER = 1
TOKEN_ELEVATION_DEFAULT = 1
TOKEN_ELEVATION_FULL = 2
TOKEN_ELEVATION_LIMITED = 3
WAIT_FAILED = 0xFFFFFFFF
CREATE_SUSPENDED = 0x4
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
RESUME_FAILED = 0xFFFFFFFF
SDDL_REVISION_1 = 1
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
ERROR_ALREADY_EXISTS = 183
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


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


def private_dir_sddl(user_sid: str) -> str:
    """The security of a private folder: owned by the user, with full access for the
    user, SYSTEM and Administrators, passed on to everything inside it, and nothing
    inherited from the folder it sits in."""
    return f"O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


def private_dir_problem(attributes: int, owner_sid: str | None, dacl: str | None, private_dacl: str,
                        user_sid: str) -> str | None:
    """Why the existing folder cannot be used as the user's private folder, or None.
    `owner_sid` and `dacl` (its access list as SDDL) are read only for a real folder
    (None otherwise); `private_dacl` is private_dir_sddl's access list as Windows
    writes it back, with the same short names for well-known accounts. The folder
    must grant no one more than that does, and take nothing from the folder above."""
    if attributes == INVALID_FILE_ATTRIBUTES or not attributes & FILE_ATTRIBUTE_DIRECTORY:
        return "is not a folder"
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return "is a link to another place"
    if owner_sid != user_sid:
        return f"belongs to another account ({owner_sid})"
    found = re.fullmatch(r"D:([A-Z_]*)((?:\([^()]*\))*)", dacl or "")
    if not found:
        return f"has an access list Celerp cannot read ({dacl})"
    flags, entries = found.groups()
    allowed = set(re.findall(r"\([^()]*\)", private_dacl))
    if "P" not in flags or "NO_ACCESS_CONTROL" in flags or not set(re.findall(r"\([^()]*\)", entries)) <= allowed:
        return "can be opened by other accounts"
    return None


def _api():
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class Trustee(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                    ("grfInheritance", wintypes.DWORD), ("Trustee", Trustee)]

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

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation), ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

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
        (kernel32, "CreateJobObjectW"): (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        (kernel32, "SetInformationJobObject"): (wintypes.BOOL, [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        (kernel32, "AssignProcessToJobObject"): (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        (kernel32, "ResumeThread"): (wintypes.DWORD, [wintypes.HANDLE]),
        (kernel32, "TerminateProcess"): (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        (advapi32, "CheckTokenMembership"): (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]),
        (advapi32, "GetTokenInformation"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
        (advapi32, "OpenProcessToken"): (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]),
        (advapi32, "ConvertStringSidToSidW"): (wintypes.BOOL, [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]),
        (advapi32, "CreateRestrictedToken"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SidAndAttributes),
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]),
        (advapi32, "SetTokenInformation"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]),
        (advapi32, "SetEntriesInAclW"): (wintypes.DWORD, [
            wintypes.ULONG, ctypes.POINTER(ExplicitAccess), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        (kernel32, "CreateDirectoryW"): (wintypes.BOOL, [wintypes.LPCWSTR, ctypes.c_void_p]),
        (kernel32, "GetFileAttributesW"): (wintypes.DWORD, [wintypes.LPCWSTR]),
        (advapi32, "ConvertSidToStringSidW"): (wintypes.BOOL, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        (advapi32, "ConvertSecurityDescriptorToStringSecurityDescriptorW"): (wintypes.BOOL, [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]),
        (advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW"): (wintypes.BOOL, [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]),
        (advapi32, "GetNamedSecurityInfoW"): (wintypes.DWORD, [
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        (advapi32, "CreateProcessAsUserW"): (wintypes.BOOL, [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInformation)]),
    }
    for (dll, name), (restype, argtypes) in signatures.items():
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = restype, argtypes
    return (kernel32, advapi32, wintypes, SidAndAttributes, StartupInfo, ProcessInformation,
            ExtendedLimitInformation, ExplicitAccess)


def _token_information(advapi32, wintypes, token, info_class: int, call: str):
    """The token's `info_class` record, in a buffer that starts with the record."""
    size = wintypes.DWORD()
    advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    _check(advapi32.GetTokenInformation(token, info_class, buffer, size, ctypes.byref(size)), call)
    return buffer


def _add_user_to_default_dacl(kernel32, advapi32, wintypes, ExplicitAccess, token) -> None:
    """Grant the token's user full access in its default access list, as pg_ctl does
    (AddUserToTokenDacl): every object the program creates gets that list."""
    user = _token_information(advapi32, wintypes, token, TOKEN_USER_CLASS, "GetTokenInformation(TokenUser)")
    dacl = _token_information(advapi32, wintypes, token, TOKEN_DEFAULT_DACL_CLASS,
                              "GetTokenInformation(TokenDefaultDacl)")
    grant = ExplicitAccess(GENERIC_ALL, GRANT_ACCESS, NO_INHERITANCE)
    grant.Trustee.TrusteeForm, grant.Trustee.TrusteeType = TRUSTEE_IS_SID, TRUSTEE_IS_USER
    # Both records start with a pointer: TOKEN_USER to the user's SID, TOKEN_DEFAULT_DACL to the list.
    grant.Trustee.ptstrName = ctypes.c_void_p.from_buffer(user).value
    widened = ctypes.c_void_p()
    error = advapi32.SetEntriesInAclW(1, ctypes.byref(grant), ctypes.c_void_p.from_buffer(dacl).value,
                                      ctypes.byref(widened))
    if error:
        raise LaunchError(f"SetEntriesInAclW failed (error {error})")
    try:
        record = ctypes.c_void_p(widened.value)
        _check(advapi32.SetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, ctypes.byref(record),
                                            ctypes.sizeof(record)), "SetTokenInformation(TokenDefaultDacl)")
    finally:
        kernel32.LocalFree(widened)


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


def _sid_string(kernel32, advapi32, wintypes, sid) -> str:
    text = ctypes.c_void_p()
    _check(advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)), "ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


def _dacl_sddl(kernel32, advapi32, descriptor) -> str:
    """The access list of a security descriptor, as SDDL."""
    text = ctypes.c_void_p()
    _check(advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor, SDDL_REVISION_1, DACL_SECURITY_INFORMATION, ctypes.byref(text), None),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


def private_dir(parent: str, prefix: str) -> str:
    """Create or check the user's private folder PARENT\\PREFIX<user SID>; return its path."""
    kernel32, advapi32, wintypes, *_ = _api()

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    token = wintypes.HANDLE()
    _check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)),
           "OpenProcessToken")
    try:
        user = _token_information(advapi32, wintypes, token, TOKEN_USER_CLASS, "GetTokenInformation(TokenUser)")
        # TOKEN_USER starts with a pointer to the user's SID.
        user_sid = _sid_string(kernel32, advapi32, wintypes, ctypes.c_void_p.from_buffer(user).value)
    finally:
        kernel32.CloseHandle(token)
    folder = parent.rstrip("\\") + "\\" + prefix + user_sid

    # Created with its security in the one call, so no other account can reach it
    # in between.
    descriptor = ctypes.c_void_p()
    _check(advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        private_dir_sddl(user_sid), SDDL_REVISION_1, ctypes.byref(descriptor), None),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        private_dacl = _dacl_sddl(kernel32, advapi32, descriptor)
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value, False)
        if not kernel32.CreateDirectoryW(folder, ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != ERROR_ALREADY_EXISTS:
                raise LaunchError(f"CreateDirectoryW failed (error {error})")
    finally:
        kernel32.LocalFree(descriptor)

    found = kernel32.GetFileAttributesW(folder)
    owner_sid = dacl = None
    if found != INVALID_FILE_ATTRIBUTES and found & FILE_ATTRIBUTE_DIRECTORY and not found & FILE_ATTRIBUTE_REPARSE_POINT:
        owner = ctypes.c_void_p()
        security = ctypes.c_void_p()
        error = advapi32.GetNamedSecurityInfoW(folder, SE_FILE_OBJECT,
                                               OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                               ctypes.byref(owner), None, None, None, ctypes.byref(security))
        if error:
            raise LaunchError(f"GetNamedSecurityInfoW failed (error {error})")
        try:
            owner_sid = _sid_string(kernel32, advapi32, wintypes, owner)
            dacl = _dacl_sddl(kernel32, advapi32, security)
        finally:
            kernel32.LocalFree(security)
    problem = private_dir_problem(found, owner_sid, dacl, private_dacl, user_sid)
    if problem:
        raise LaunchError(f"{folder} {problem}")
    return folder


def run_restricted(argv: list[str]) -> int:
    """Start `argv` with the restricted token, wait for it, and return its exit code."""
    (kernel32, advapi32, wintypes, SidAndAttributes, StartupInfo, ProcessInformation,
     ExtendedLimitInformation, ExplicitAccess) = _api()

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

    # Never closed: the job, and the program in it, ends when this script does.
    job = kernel32.CreateJobObjectW(None, None)
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        _add_user_to_default_dacl(kernel32, advapi32, wintypes, ExplicitAccess, restricted)
        _check(job, "CreateJobObjectW")
        _check(kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                                ctypes.byref(limits), ctypes.sizeof(limits)),
               "SetInformationJobObject")
        startup = StartupInfo(cb=ctypes.sizeof(StartupInfo), dwFlags=STARTF_USESTDHANDLES)
        handles = [kernel32.GetStdHandle(n) for n in STD_HANDLES]
        for handle in handles:
            # The program writes to the same pipes the app reads from this script.
            kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        process = ProcessInformation()
        line = ctypes.create_unicode_buffer(command_line(argv))
        _check(advapi32.CreateProcessAsUserW(restricted, argv[0], line, None, None, True, CREATE_SUSPENDED,
                                             None, None, ctypes.byref(startup), ctypes.byref(process)),
               "CreateProcessAsUserW")
    finally:
        kernel32.CloseHandle(restricted)

    try:
        # Suspended until it is in the job, so nothing it starts can escape it.
        if not kernel32.AssignProcessToJobObject(job, process.hProcess):
            error = ctypes.get_last_error()
            kernel32.TerminateProcess(process.hProcess, 1)
            raise LaunchError(f"AssignProcessToJobObject failed (error {error})")
        if kernel32.ResumeThread(process.hThread) == RESUME_FAILED:
            error = ctypes.get_last_error()
            kernel32.TerminateProcess(process.hProcess, 1)
            raise LaunchError(f"ResumeThread failed (error {error})")
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
        if argv[0] == "--private-dir" and len(argv) == 3:
            print(private_dir(argv[1], argv[2]))
            return 0
        return exit_status(run_restricted(argv))
    except LaunchError as e:
        print(f"{FAILURE_PREFIX} {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
