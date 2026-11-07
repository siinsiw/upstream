"""Download an installer, check it, and run it silently when its framework is known."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
from ctypes import wintypes
from pathlib import Path

from .errors import Cancelled, InstallError  # noqa: F401 - re-exported

log = logging.getLogger("upstream.installer")

USER_AGENT = "Upstream-Updater/1.0"
CREATE_NO_WINDOW = 0x08000000

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


def signature(path: Path) -> tuple[str, str]:
    """(status, signer) from Authenticode. Status is Valid, NotSigned, HashMismatch, ..."""
    literal = str(path).replace("'", "''")
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; "
              "\"$($s.Status)|$($s.SignerCertificate.Subject)\"")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        status, _, signer = r.stdout.strip().partition("|")
        cn = re.search(r"CN=\"?([^,\"]+)", signer)
        return status or "Unknown", cn.group(1) if cn else signer
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
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _shell_run(file: str, params: str, show: int = 1, cancel=None) -> int:
    """ShellExecuteEx + wait. Unlike CreateProcess it shows the UAC prompt for
    installers whose manifest asks for admin rights."""
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
    try:
        _kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        _kernel32.CloseHandle(info.hProcess)


def install(path: Path, kind: str, scope: str, plan=None, cancel=None) -> int:
    """Run the installer and return its exit code. `plan` (inplace.InstallPlan) adds switches that
    keep the update in the app's existing folder; `cancel` (context.CancelToken) lets the user stop
    waiting."""
    extra = f" {plan.extra_args}" if plan is not None and plan.extra_args else ""
    if kind == "msi":
        # /passive: progress bar only, and unlike /qn it can raise UAC itself.
        return _shell_run("msiexec.exe", f'/i "{path}" /passive /norestart{extra}', cancel=cancel)
    if kind == "msix":
        literal = str(path).replace("'", "''")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Add-AppxPackage -Path '{literal}' -ForceApplicationShutdown"],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if r.returncode:
            detail = r.stderr.strip().splitlines()[0] if r.stderr.strip() else ""
            if detail:
                raise InstallError("Couldn't install the MSIX package: {error}", error=detail)
            raise InstallError("Couldn't install the MSIX package.")
        return 0
    params = SILENT_ARGS.get(kind, "")
    if kind == "inno" and scope == "user":
        params += " /CURRENTUSER"
    return _shell_run(str(path), (params + extra).strip(), cancel=cancel)


def succeeded(code: int) -> bool:
    return code in _SUCCESS
