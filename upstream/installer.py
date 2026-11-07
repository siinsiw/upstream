"""Download an installer, check it, and run it silently when its framework is known."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .errors import Cancelled, InstallError  # noqa: F401 - re-exported

log = logging.getLogger("upstream.installer")

USER_AGENT = "Upstream-Updater/1.0"
CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT_MIN = 30
_SLICE = 0.25          # seconds between looks at the cancel token while an installer runs
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0, 0x102
_settings = None

# Silent switches per installer framework. Unknown .exe files get none and run
# with their normal wizard, so nothing is guessed blindly.
SILENT_ARGS = {
    "inno": "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CLOSEAPPLICATIONS",
    "nullsoft": "/S",
    "burn": "/quiet /norestart",
    "installshield": '/s /v"/qn /norestart"',
    "advancedinstaller": "/exenoui /qn /norestart",
    "squirrel": "--silent",
}
_SIGNATURES = [
    ("inno", (b"Inno Setup", b"JR.Inno.Setup")),
    ("nullsoft", (b"Nullsoft.NSIS", b"NullsoftInst")),
    ("burn", (b".wixburn",)),
    ("installshield", (b"InstallShield",)),
    ("advancedinstaller", (b"Advanced Installer",)),
    ("squirrel", (b"Squirrel",)),
]
_WINGET_TYPES = {"inno": "inno", "nullsoft": "nullsoft", "burn": "burn", "wix": "msi", "msi": "msi",
                 "msix": "msix", "appx": "msix"}
_SUCCESS = {0, 3010, 1641}  # 3010/1641: done, restart needed


def common_name(subject: str) -> str:
    """CN of an X.500 subject. A quoted CN may contain commas ("Dropbox, Inc.") and doubled quotes."""
    m = re.search(r'CN=(?:"((?:[^"]|"")*)"|([^,]+))', subject)
    if not m:
        return subject
    return m.group(1).replace('""', '"') if m.group(1) is not None else m.group(2).strip()


def signature(path: Path) -> tuple[str, str]:
    """(status, signer) from Authenticode. Status is Valid, NotSigned, HashMismatch, ..."""
    literal = str(path).replace("'", "''")
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; "
              "\"$($s.Status)|$($s.SignerCertificate.Subject)\"")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, creationflags=CREATE_NO_WINDOW)
        status, _, signer = (r.stdout or "").strip().partition("|")
        return status or "Unknown", common_name(signer) if signer else ""
    except (OSError, subprocess.SubprocessError):
        return "Unknown", ""


def detect(path: Path, hint: str | None = None) -> str:
    name = path.name.lower()
    with open(path, "rb") as fh:
        head = fh.read(4 * 1024 * 1024)
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or name.endswith(".msi"):
        return "msi"
    if name.endswith((".msix", ".msixbundle", ".appx", ".appxbundle")):
        return "msix"
    if not head.startswith(b"MZ"):
        raise InstallError("The downloaded file is not a Windows installer.")
    for kind, markers in _SIGNATURES:
        if any(m in head for m in markers):
            return kind
    return _WINGET_TYPES.get(hint or "", "exe")


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def configure(settings) -> None:
    """Keep the settings object: install_timeout_min is read on every install, so a change made in the
    settings dialog applies to the next one without a restart."""
    global _settings
    _settings = settings


def timeout_minutes() -> int:
    """How long to wait for one installer before moving on; 0 means as long as it takes."""
    value = _settings.get("install_timeout_min", DEFAULT_TIMEOUT_MIN) if _settings is not None else DEFAULT_TIMEOUT_MIN
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MIN


def _wait_for(finished: Callable[[float], bool], cancel=None, timeout_min: float | None = None) -> None:
    """Call finished(seconds) until it reports the installer is done. Raises Cancelled when the user stops
    waiting or the time limit passes. The installer is never killed: it may be in the middle of writing files."""
    deadline = time.monotonic() + timeout_min * 60 if timeout_min else None
    while not finished(_SLICE):
        if cancel is not None and cancel.is_set:
            raise Cancelled("Stopped waiting for the installer. It may still be running.")
        if deadline is not None and time.monotonic() >= deadline:
            raise Cancelled("The installer was still running after {minutes} minutes, so Upstream stopped "
                            "waiting for it. It may still be running.", minutes=f"{timeout_min:g}")


def _shell_run(file: str, params: str, show: int = 1, cancel=None, timeout_min: float | None = None) -> int:
    """ShellExecuteEx + wait. Unlike CreateProcess it shows the UAC prompt for
    installers whose manifest asks for admin rights. With `cancel` or `timeout_min` the wait can end
    early (Cancelled) while the process carries on."""
    if cancel is not None:
        cancel.check()
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # NOCLOSEPROCESS | NOASYNC
    info.lpVerb = "open"
    info.lpFile = file
    info.lpParameters = params
    info.nShow = show
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:
            raise Cancelled("Administrator permission (UAC) was declined.")
        raise InstallError("Couldn't start the installer (Windows error {code}).", code=err)
    if not info.hProcess:
        return 0

    def finished(seconds: float) -> bool:
        state = _kernel32.WaitForSingleObject(info.hProcess, int(seconds * 1000))
        if state not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
            raise InstallError("Lost track of the installer (Windows error {code}).", code=ctypes.get_last_error())
        return state == _WAIT_OBJECT_0

    try:
        _wait_for(finished, cancel, timeout_min)
        code = wintypes.DWORD()
        _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        _kernel32.CloseHandle(info.hProcess)


def _exited(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _add_appx(path: Path, cancel, timeout_min: float | None) -> int:
    if cancel is not None:
        cancel.check()
    literal = str(path).replace("'", "''")
    proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             f"Add-AppxPackage -Path '{literal}' -ForceApplicationShutdown"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            creationflags=CREATE_NO_WINDOW)
    _wait_for(lambda seconds: _exited(proc, seconds), cancel, timeout_min)
    if proc.returncode:
        detail = proc.stderr.read().strip().splitlines()[:1]
        if detail:
            raise InstallError("Couldn't install the MSIX package: {error}", error=detail[0])
        raise InstallError("Couldn't install the MSIX package.")
    return 0


def install(path: Path, kind: str, scope: str, plan=None, cancel=None) -> int:
    """Run the installer and return its exit code. `plan` (inplace.InstallPlan) adds switches that
    keep the update in the app's existing folder; `cancel` (context.CancelToken) lets the user stop
    waiting, and install_timeout_min does the same after a while: either way Cancelled is raised and the
    installer is left running."""
    extra = f" {plan.extra_args}" if plan is not None and plan.extra_args else ""
    limit = timeout_minutes()
    if kind == "msi":
        # /passive: progress bar only, and unlike /qn it can raise UAC itself.
        return _shell_run("msiexec.exe", f'/i "{path}" /passive /norestart{extra}', cancel=cancel, timeout_min=limit)
    if kind == "msix":
        return _add_appx(path, cancel, limit)
    params = SILENT_ARGS.get(kind, "")
    if kind == "inno" and scope == "user":
        params += " /CURRENTUSER"
    return _shell_run(str(path), (params + extra).strip(), cancel=cancel, timeout_min=limit)


def succeeded(code: int) -> bool:
    return code in _SUCCESS
