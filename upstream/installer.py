"""Download an installer, check it, and run it silently when its framework is known."""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import subprocess
import urllib.request
from ctypes import wintypes
from pathlib import Path

from .config import DOWNLOAD_DIR

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


class InstallError(Exception):
    """Carries an English template and its values so the UI can show it in
    whichever language is active when the message is displayed."""

    def __init__(self, template: str, **values):
        super().__init__(template.format(**values) if values else template)
        self.template, self.values = template, values


class Cancelled(InstallError):
    pass


def download(url: str, file_name: str | None, on_progress, expected_sha256: str | None = None) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if not file_name:
            disposition = resp.headers.get("Content-Disposition") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
            file_name = m.group(1) if m else resp.url.rsplit("/", 1)[-1].split("?", 1)[0]
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", file_name).strip(". ") or "installer.exe"
        dest = DOWNLOAD_DIR / safe
        part = dest.with_name(dest.name + ".part")
        total = int(resp.headers.get("Content-Length") or 0)
        digest = hashlib.sha256()
        done = 0
        with open(part, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                on_progress(done, total)
    if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
        part.unlink(missing_ok=True)
        raise InstallError("The download's SHA-256 doesn't match the hash GitHub published; nothing was installed.")
    os.replace(part, dest)
    return dest


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


def _shell_run(file: str, params: str, show: int = 1) -> int:
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


def install(path: Path, kind: str, scope: str) -> int:
    if kind == "msi":
        # /passive: progress bar only, and unlike /qn it can raise UAC itself.
        return _shell_run("msiexec.exe", f'/i "{path}" /passive /norestart')
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
    return _shell_run(str(path), params)


def succeeded(code: int) -> bool:
    return code in _SUCCESS
