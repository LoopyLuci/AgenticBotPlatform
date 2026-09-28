"""Run a command with no network on Windows, without elevation: an AppContainer.

The `network: none` option of the `local` and `windows_job` sandbox backends (see
sandbox.py). An AppContainer is the Windows sandbox that UWP apps and browser renderers
run in; a process started in one with **no capabilities** has no network access at
all: no internet, no LAN, not even loopback, and DNS lookups fail. Windows enforces this
in the kernel, so no firewall rule or elevation is needed. Creating the container
profile is a per-user operation.

**How a command gets there.** Python's subprocess and asyncio APIs cannot pass the
`SECURITY_CAPABILITIES` process attribute, so sandbox.py starts this file as a small
launcher (an ordinary child process whose pipes asyncio already knows how to read). The
launcher then:

1. creates (or reuses) the `ABP.Sandbox.Offline` container profile;
2. grants the container access to the workspace, and read/run access to any extra
   folders configured;
3. creates the real command (`cmd.exe /d /s /c <command>`) inside the container,
   **suspended**;
4. puts it in a kill-on-close Job Object, and only then lets it run.

Step 4 closes the race that `win_job.assign()` documents: nothing the command starts can
escape the job. The command inherits the launcher's stdout/stderr pipe, so output flows
to the agent unchanged. The launcher's exit code is the command's. If the launcher is
killed, its job handle closes and Windows kills the whole contained tree.

**Also confines the filesystem, and that has a cost.** A container process can open
only what grants it access: the workspace (granted in step 2), system folders
(Windows grants "ALL APPLICATION PACKAGES" read access there), and the folders listed
in `sandbox.network_none.extra_paths`. Tools installed under the user profile, such as a
per-user Python or a virtualenv outside the workspace, are therefore invisible until
listed there. This is deliberate: the alternative is a network block that a command
could bypass by reading the user's saved credentials instead.

Fails closed. If any step fails (an old Windows, an unsupported filesystem such as a
network drive, an ACL that cannot be changed), the command is not run and the error
says why.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

PROFILE_NAME = "ABP.Sandbox.Offline"
_DISPLAY = "AgenticBotPlatform offline sandbox"
_DESCRIPTION = "Runs agent shell commands with no network access"

# Access masks: modify (read, write, run, delete) for the workspace; read and run for extras.
MODIFY = 0x1301BF
READ_EXECUTE = 0x1200A9
_INHERIT_BOTH = 0x3            # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_ACCESS_ALLOWED_ACE_TYPE = 0
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x4
_GRANT_ACCESS = 1
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_WELL_KNOWN_GROUP = 5
_ALREADY_EXISTS = -2147024713  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS) as a signed 32-bit value

_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x1
_INFINITE = 0xFFFFFFFF

_is_windows = os.name == "nt"


def is_supported() -> bool:
    return _is_windows


if _is_windows:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _uenv = ctypes.WinDLL("userenv", use_last_error=True)
    _ole = ctypes.WinDLL("ole32")

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class _SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", wintypes.LPVOID), ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
                    ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", wintypes.LPVOID), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class _TRUSTEE_W(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", wintypes.LPVOID), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", wintypes.LPVOID)]

    class _EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                    ("grfInheritance", wintypes.DWORD), ("Trustee", _TRUSTEE_W)]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wintypes.WORD)]

    class _ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [("Header", _ACE_HEADER), ("Mask", wintypes.DWORD), ("SidStart", wintypes.DWORD)]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    def _proto(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, list(argtypes)

    _proto(_uenv.CreateAppContainerProfile, ctypes.c_long, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
           wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID))
    _proto(_uenv.DeriveAppContainerSidFromAppContainerName, ctypes.c_long, wintypes.LPCWSTR,
           ctypes.POINTER(wintypes.LPVOID))
    _proto(_uenv.GetAppContainerFolderPath, ctypes.c_long, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR))
    _proto(_adv.ConvertSidToStringSidW, wintypes.BOOL, wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    _proto(_adv.FreeSid, wintypes.LPVOID, wintypes.LPVOID)
    _proto(_adv.EqualSid, wintypes.BOOL, wintypes.LPVOID, wintypes.LPVOID)
    _proto(_adv.GetNamedSecurityInfoW, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
           wintypes.LPVOID, wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
           ctypes.POINTER(wintypes.LPVOID))
    _proto(_adv.SetNamedSecurityInfoW, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
           wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID)
    _proto(_adv.SetEntriesInAclW, wintypes.DWORD, wintypes.ULONG, ctypes.POINTER(_EXPLICIT_ACCESS_W),
           wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID))
    _proto(_adv.GetAclInformation, wintypes.BOOL, wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int)
    _proto(_adv.GetAce, wintypes.BOOL, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID))
    _proto(_k32.LocalFree, wintypes.LPVOID, wintypes.LPVOID)
    _proto(_k32.InitializeProcThreadAttributeList, wintypes.BOOL, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
           ctypes.POINTER(ctypes.c_size_t))
    _proto(_k32.UpdateProcThreadAttribute, wintypes.BOOL, wintypes.LPVOID, wintypes.DWORD, ctypes.c_size_t,
           wintypes.LPVOID, ctypes.c_size_t, wintypes.LPVOID, wintypes.LPVOID)
    _proto(_k32.DeleteProcThreadAttributeList, None, wintypes.LPVOID)
    _proto(_k32.CreateProcessW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID,
           wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
           ctypes.POINTER(_STARTUPINFOEXW), ctypes.POINTER(_PROCESS_INFORMATION))
    _proto(_k32.GetStdHandle, wintypes.HANDLE, wintypes.DWORD)
    _proto(_k32.SetHandleInformation, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    _proto(_k32.AssignProcessToJobObject, wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
    _proto(_k32.ResumeThread, wintypes.DWORD, wintypes.HANDLE)
    _proto(_k32.TerminateProcess, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _proto(_k32.WaitForSingleObject, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _proto(_k32.GetExitCodeProcess, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _proto(_k32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
    _proto(_k32.SetEnvironmentVariableW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPCWSTR)


def _hr_check(hr: int, what: str) -> None:
    if hr != 0:
        raise OSError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def _win_check(ok, what: str) -> None:
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"{what} failed: {ctypes.FormatError(err).strip()}")


def container_sid():
    """The container's SID (caller frees it with FreeSid), creating the profile the first time."""
    sid = wintypes.LPVOID()
    hr = _uenv.CreateAppContainerProfile(PROFILE_NAME, _DISPLAY, _DESCRIPTION, None, 0, ctypes.byref(sid))
    if hr == _ALREADY_EXISTS:
        hr = _uenv.DeriveAppContainerSidFromAppContainerName(PROFILE_NAME, ctypes.byref(sid))
    _hr_check(hr, "creating the AppContainer profile")
    return sid


def sid_string(sid) -> str:
    out = wintypes.LPWSTR()
    _win_check(_adv.ConvertSidToStringSidW(sid, ctypes.byref(out)), "ConvertSidToStringSid")
    try:
        return out.value
    finally:
        _k32.LocalFree(out)


def container_folder(sid) -> Path:
    """The container's own writable folder (%LOCALAPPDATA%\\Packages\\<name>\\AC)."""
    out = wintypes.LPWSTR()
    _hr_check(_uenv.GetAppContainerFolderPath(sid_string(sid), ctypes.byref(out)), "GetAppContainerFolderPath")
    try:
        return Path(out.value)
    finally:
        _ole.CoTaskMemFree(out)


def _has_grant(path: str, sid, mask: int) -> bool:
    dacl, sd = wintypes.LPVOID(), wintypes.LPVOID()
    err = _adv.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None,
                                     ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise OSError(err, f"reading the permissions of {path} failed: {ctypes.FormatError(err).strip()}")
    try:
        if not dacl:
            return True                     # a NULL DACL grants everyone everything
        info = _ACL_SIZE_INFORMATION()
        _win_check(_adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2), "GetAclInformation")
        for i in range(info.AceCount):
            ace = wintypes.LPVOID()
            if not _adv.GetAce(dacl, i, ctypes.byref(ace)):
                continue
            allowed = ctypes.cast(ace, ctypes.POINTER(_ACCESS_ALLOWED_ACE)).contents
            if allowed.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                continue
            ace_sid = ace.value + _ACCESS_ALLOWED_ACE.SidStart.offset
            if (_adv.EqualSid(ace_sid, sid) and allowed.Mask & mask == mask
                    and allowed.Header.AceFlags & _INHERIT_BOTH == _INHERIT_BOTH):
                return True
        return False
    finally:
        _k32.LocalFree(sd)


def grant(path: Path, sid, mask: int) -> bool:
    """Give the container `mask` on `path` and everything under it. A no-op (returns
    False) when an equal grant is already there: re-applying rewrites the ACL of every
    file in the tree, which is slow on a large workspace."""
    target = str(Path(path).resolve())
    if _has_grant(target, sid, mask):
        return False
    dacl, sd = wintypes.LPVOID(), wintypes.LPVOID()
    err = _adv.GetNamedSecurityInfoW(target, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None,
                                     ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise OSError(err, f"reading the permissions of {target} failed: {ctypes.FormatError(err).strip()}")
    new_dacl = wintypes.LPVOID()
    try:
        ea = _EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = mask
        ea.grfAccessMode = _GRANT_ACCESS
        ea.grfInheritance = _INHERIT_BOTH
        ea.Trustee.TrusteeForm = _TRUSTEE_IS_SID
        ea.Trustee.TrusteeType = _TRUSTEE_IS_WELL_KNOWN_GROUP
        ea.Trustee.ptstrName = sid
        err = _adv.SetEntriesInAclW(1, ctypes.byref(ea), dacl, ctypes.byref(new_dacl))
        if err:
            raise OSError(err, f"building the permissions for {target} failed: {ctypes.FormatError(err).strip()}")
        err = _adv.SetNamedSecurityInfoW(target, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None,
                                         new_dacl, None)
        if err:
            raise OSError(err, f"granting the offline sandbox access to {target} failed: "
                               f"{ctypes.FormatError(err).strip()}")
        return True
    finally:
        _k32.LocalFree(new_dacl)
        _k32.LocalFree(sd)


def _inheritable_std_handles() -> tuple[int, int, int]:
    handles = []
    for which in (-10, -11, -12):              # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        h = _k32.GetStdHandle(which & 0xFFFFFFFF)
        if h and h != wintypes.HANDLE(-1).value:
            _k32.SetHandleInformation(h, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT)
        handles.append(h or 0)
    return tuple(handles)


def run(command: str, cwd: Path, *, grants: list[tuple[Path, int]], memory_mb: int = 0,
        active_process_limit: int = 0) -> int:
    """Run `command` through cmd.exe inside the container, wait, and return its exit code."""
    from bot.agent_runtime import win_job

    sid = container_sid()
    try:
        for path, mask in grants:
            grant(path, sid, mask)
        # The command's temp files go to the container's own folder; the user's %TEMP% is out of reach.
        temp = container_folder(sid) / "Temp"
        temp.mkdir(parents=True, exist_ok=True)
        for name in ("TEMP", "TMP"):              # the child inherits this process's environment block
            _win_check(_k32.SetEnvironmentVariableW(name, str(temp)), "SetEnvironmentVariable")

        caps = _SECURITY_CAPABILITIES(AppContainerSid=sid, Capabilities=None, CapabilityCount=0, Reserved=0)
        size = ctypes.c_size_t()
        _k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        _win_check(_k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)),
                   "InitializeProcThreadAttributeList")
        try:
            _win_check(_k32.UpdateProcThreadAttribute(attrs, 0, _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                                      ctypes.byref(caps), ctypes.sizeof(caps), None, None),
                       "UpdateProcThreadAttribute")
            si = _STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(si)
            si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = _inheritable_std_handles()
            si.lpAttributeList = ctypes.cast(attrs, wintypes.LPVOID)
            comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
            cmdline = ctypes.create_unicode_buffer(f'"{comspec}" /d /s /c "{command}"')
            pi = _PROCESS_INFORMATION()
            _win_check(_k32.CreateProcessW(comspec, cmdline, None, None, True,
                                           _EXTENDED_STARTUPINFO_PRESENT | _CREATE_SUSPENDED,
                                           None, str(cwd), ctypes.byref(si), ctypes.byref(pi)),
                       "starting the command in the AppContainer")
        finally:
            _k32.DeleteProcThreadAttributeList(attrs)
    finally:
        _adv.FreeSid(sid)

    job = None
    try:
        job = win_job.create(memory_mb=memory_mb, active_process_limit=active_process_limit)
        _win_check(_k32.AssignProcessToJobObject(job, pi.hProcess), "AssignProcessToJobObject")
    except OSError:
        _k32.TerminateProcess(pi.hProcess, 1)
        _k32.CloseHandle(pi.hThread)
        _k32.CloseHandle(pi.hProcess)
        if job:
            win_job.terminate(job)
        raise
    _k32.ResumeThread(pi.hThread)
    _k32.CloseHandle(pi.hThread)
    _k32.WaitForSingleObject(pi.hProcess, _INFINITE)
    code = wintypes.DWORD()
    _k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    _k32.CloseHandle(pi.hProcess)
    win_job.terminate(job, code.value)       # anything the command left running in the background goes too
    return code.value


def launcher_argv(python: str, command: str, cwd: Path, workspace: Path, *, extra_paths=(), memory_mb: int = 0,
                  active_process_limit: int = 0) -> list[str]:
    """The argv sandbox.py starts: this file, run as a script, with the command after `--`."""
    argv = [python, "-I", str(Path(__file__).resolve()), "--cwd", str(cwd), "--workspace", str(workspace),
            "--memory-mb", str(int(memory_mb)), "--procs", str(int(active_process_limit))]
    for p in extra_paths:
        argv += ["--read", str(p)]
    return argv + ["--", command]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="appcontainer", description=__doc__.splitlines()[0])
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--read", action="append", default=[])
    ap.add_argument("--memory-mb", type=int, default=0)
    ap.add_argument("--procs", type=int, default=0)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    rest = args.command[1:] if args.command[:1] == ["--"] else args.command
    command = " ".join(rest)
    if not command.strip():
        print("abp offline sandbox: no command given", file=sys.stderr)
        return 2
    if not is_supported():
        print("abp offline sandbox: AppContainers only exist on Windows; the command was not run", file=sys.stderr)
        return 126
    grants = [(Path(args.workspace), MODIFY)] + [(Path(p), READ_EXECUTE) for p in args.read]
    try:
        return run(command, Path(args.cwd), grants=grants, memory_mb=args.memory_mb, active_process_limit=args.procs)
    except OSError as exc:
        print(f"abp offline sandbox: {exc}; the command was not run", file=sys.stderr)
        return 126


if __name__ == "__main__":
    # Run by path with -I (see launcher_argv): make the checkout importable for win_job.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main(sys.argv[1:]))
