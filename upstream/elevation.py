"""One UAC prompt for a whole batch: run Upstream itself as administrator.

Machine-wide installers each raise their own UAC prompt. The supported way to see one prompt per batch is to run the
whole program elevated for that session (relaunch_elevated). A privileged helper service was rejected on purpose: a
persistent elevated process that other programs on the PC can talk to is a ready-made escalation hole.

Elevation only suits an administrator account: its token is a "limited" half of a split token and the elevated copy runs
as the same user. A standard user would be asked for another account's password and the elevated copy would then read
that account's settings and registry, so can_relaunch_elevated() says when offering the restart makes sense.

The new process gets `--elevated --wait-pid <old pid>` and waits (wait_for_exit) for this one to quit, so the two
never run side by side."""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from .i18n import N_, trn
from .installer import _ShellExecuteInfo, _shell32

log = logging.getLogger("upstream.elevation")

RESTART_LABEL = N_("Restart as administrator")      # the banner's button

_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION, _TOKEN_ELEVATION_TYPE = 20, 18     # TOKEN_INFORMATION_CLASS values
_ELEVATION_TYPE_LIMITED = 3                          # TokenElevationTypeLimited: an admin account, not elevated yet
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_SEE_MASK_NOASYNC = 0x00000100
_SW_SHOWNORMAL = 1
_ERROR_CANCELLED = 1223        # the user said No to the UAC prompt
_ERROR_INVALID_PARAMETER = 87  # OpenProcess: no process has that id (it already exited)
_PACKAGED_TYPES = frozenset({"msix", "appx", "msixbundle", "appxbundle"})

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
_advapi32.GetTokenInformation.restype = wintypes.BOOL


def banner_text(count: int) -> str:
    """The line for the banner the main window shows when `count` updates will each ask for administrator rights."""
    return trn("{n} update needs administrator rights. Restart Upstream as administrator to approve it with one prompt.",
               "{n} updates need administrator rights. Restart Upstream as administrator to approve them all with one prompt.",
               count)


def _token_value(info_class: int) -> int | None:
    """One DWORD of this process's token (TokenElevation, TokenElevationType); None when Windows won't say."""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        value, size = wintypes.DWORD(), wintypes.DWORD()
        if not _advapi32.GetTokenInformation(token, info_class, ctypes.byref(value), ctypes.sizeof(value),
                                             ctypes.byref(size)):
            return None
        return value.value
    finally:
        _kernel32.CloseHandle(token)


def is_elevated() -> bool:
    """True when this process has the full administrator token (a UAC-elevated run, or an account with UAC off)."""
    return bool(_token_value(_TOKEN_ELEVATION))


def can_relaunch_elevated() -> bool:
    """True when relaunch_elevated() would give the same user an elevated copy: an administrator account that is
    not elevated yet. False when already elevated, for a standard user and when Windows can't tell."""
    return _token_value(_TOKEN_ELEVATION_TYPE) == _ELEVATION_TYPE_LIMITED


def _own_command() -> tuple[str, list[str], str]:
    """(program, leading arguments, working folder) that start this program again."""
    if getattr(sys, "frozen", False):
        return sys.executable, [], str(Path(sys.executable).parent)
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    if python.name.lower() == "python.exe" and windowless.is_file():
        python = windowless
    root = Path(__file__).resolve().parent.parent
    return str(python), [str(root / "main.py")], str(root)


def _shell_execute(verb: str, file: str, params: str, directory: str) -> int:
    """ShellExecuteEx without waiting for the new process. 0 when it started, else the Windows error code."""
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOASYNC
    info.lpVerb = verb
    info.lpFile = file
    info.lpParameters = params
    info.lpDirectory = directory
    info.nShow = _SW_SHOWNORMAL
    if _shell32.ShellExecuteExW(ctypes.byref(info)):
        return 0
    return ctypes.get_last_error() or 1


def relaunch_elevated(extra_args: tuple = ()) -> bool:
    """Start this program again with administrator rights (one UAC prompt). True when it was launched;
    the caller then quits. False when the user declined (or Windows could not start it; both are logged).
    The new copy waits for this process to exit, so quit right after a True; nothing may be running."""
    program, lead, directory = _own_command()
    if lead and not Path(lead[0]).is_file():
        log.warning("elevation: %s not found", lead[0])
        return False
    params = subprocess.list2cmdline([*lead, "--elevated", "--wait-pid", str(os.getpid()), *map(str, extra_args)])
    error = _shell_execute("runas", program, params, directory)
    if error == 0:
        log.info("elevation: started %s as administrator", program)
        return True
    if error == _ERROR_CANCELLED:
        log.info("elevation: the user declined the UAC prompt")
    else:
        log.warning("elevation: could not start %s as administrator (Windows error %s)", program, error)
    return False


def wait_for_exit(pid: int, timeout: float = 15) -> bool:
    """Block until process `pid` has ended: True when it has (or never existed), False after `timeout` seconds
    or when Windows won't let us wait on it."""
    if pid <= 0:
        return True
    handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER
    try:
        return _kernel32.WaitForSingleObject(handle, int(max(timeout, 0) * 1000)) == _WAIT_OBJECT_0
    finally:
        _kernel32.CloseHandle(handle)


def needs_admin(status) -> bool:
    """Will installing this status's pending update probably raise a UAC prompt? Machine-wide copies do. Per-user
    copies, Store and MSIX packages (installed per user) and updates Upstream only links to don't."""
    update, app = status.update, status.app
    if update is None or app.scope != "machine" or app.install_kind == "msix" or app.reg_key.startswith("WINGET\\"):
        return False
    if update.source == "site" and not update.download_url:
        return False
    packaged = str(update.installer_type or "").lower() in _PACKAGED_TYPES
    return not (packaged or str(update.file_name or "").lower().endswith(tuple("." + t for t in _PACKAGED_TYPES)))


def machine_installs_pending(statuses) -> int:
    """How many of the updates in `statuses` will probably ask for administrator rights. 0 once Upstream itself
    is elevated: nothing asks anymore. Pass the statuses that would be installed (not the skipped or pinned ones)."""
    if is_elevated():
        return 0
    return sum(1 for s in statuses if needs_admin(s))
