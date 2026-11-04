"""Removes an app with its own uninstaller, then checks that Windows really
stopped listing it. plan() only reads (registry, file system); run() is the one
function that starts anything, and it refuses a blocked plan.

Messages travel as (English template, values) pairs and are translated by the UI
when shown. The exit code is a hint: many uninstallers hand over to a copy of
themselves in %TEMP% and exit early, so the verdict comes from the registry."""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from . import installer
from .i18n import english
from .inventory import GUID, InstalledApp, folder_path, still_listed

log = logging.getLogger("upstream.uninstall")

STORE_PREFIX = "WINGET\\"
POLL_SECONDS = 2
SETTLE_MAX_SECONDS = 30 * 60   # how long a live uninstaller process is waited for
GRACE_SECONDS = 20             # after the last process is gone: the key may vanish a moment later

_REBOOT = {3010, 1641}
_CANCELLED = {1602, 1223}      # MSI: user cancel, UAC declined
_BUSY = 1618                   # another installation is running
_ALREADY_GONE = {1605, 0x8A150014}   # MSI: unknown product; winget: no installed package matches
_IGNORED = {"explorer.exe", "conhost.exe", "werfault.exe", "chrome.exe", "msedge.exe", "firefox.exe",
            "brave.exe", "opera.exe", "vivaldi.exe", "iexplore.exe"}   # "why did you leave?" pages
_HELPER_NAME = re.compile(r"(\.tmp|^au_\.exe|^unins\w*\.exe)$", re.I)   # self-copies of Inno / NSIS
_SHARED = re.compile(
    r"redistributable|\bvisual c\+\+|\.net\b|\bruntimes?\b|asp\.net|\bdirectx\b|webview2"
    r"|\bjava\b|\bjre\b|\bjdk\b|\bdrivers?\b|\bopenal\b|\bphysx\b|\bxna\b", re.I)
# A device maker's software is a driver or tied to one even when its name doesn't say so.
_DEVICE_MAKER = re.compile(r"\b(?:intel|nvidia|amd|advanced micro devices|realtek|qualcomm|mediatek|broadcom)\b", re.I)
_DEVICE_PART = re.compile(r"graphics|chipset|management engine|serial io|bluetooth|wireless|wi-?fi|ethernet"
                          r"|audio|firmware|thunderbolt|storage technology|geforce|radeon|adrenalin", re.I)
_FRAMEWORKS = ("microsoft.vclibs", "microsoft.ui.xaml", "microsoft.net.native", "microsoft.windowsappruntime")
_APP_INSTALLER_IDS = ("microsoft.appinstaller", "microsoft.desktopappinstaller")
_APP_INSTALLER_NAMES = {"app installer", "desktop app installer", "windows package manager"}
_WINDOWS_IDS = re.compile(r"microsoft\.(?:edge|windowsstore|storepurchaseapp)(?:_.*)?")   # winget ids of Windows parts


@dataclass
class UninstallPlan:
    app: InstalledApp
    method: str               # msi | quiet | inno | nullsoft | winget | interactive
    file: str
    params: str
    silent: bool
    blocked: str | None = None          # English template; None when the app may be removed
    blocked_values: dict = field(default_factory=dict)
    notes: list[tuple[str, dict]] = field(default_factory=list)   # shown on the confirm page


# --- Windows API ---------------------------------------------------------------

class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_k32.Process32FirstW.restype = _k32.Process32NextW.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

_TH32CS_SNAPPROCESS = 0x2
_QUERY_LIMITED = 0x1000        # PROCESS_QUERY_LIMITED_INFORMATION
_STILL_ACTIVE = 259
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _argv(command: str) -> list[str]:
    """Split a command line the way Windows does (CommandLineToArgvW)."""
    if not command.strip():
        return []
    count = ctypes.c_int()
    ptr = _shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not ptr:
        return command.split()
    try:
        return [ptr[i] for i in range(count.value)]
    finally:
        _k32.LocalFree(ptr)


@dataclass
class _Proc:
    pid: int
    ppid: int
    name: str


def _snapshot() -> list[_Proc]:
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap in (None, _INVALID_HANDLE):
        return []
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    procs = []
    try:
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs.append(_Proc(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return procs


def _inspect(pid: int) -> tuple[str, float] | None:
    """(image path, start time as Unix seconds) of a running process; ("", 0) once it has
    exited; None when Windows refuses access (SYSTEM services, elevated processes)."""
    handle = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        status = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != _STILL_ACTIVE:
            return "", 0.0
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return "", 0.0
        started = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 1e7 - 11644473600
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        path = buf.value if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
        return path, started
    finally:
        _k32.CloseHandle(handle)


# --- paths -----------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _under(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\") + "\\")


@functools.cache
def _containers() -> frozenset[str]:
    """Folders that hold many apps: never an app's own root."""
    names = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "ALLUSERSPROFILE", "APPDATA",
             "LOCALAPPDATA", "USERPROFILE", "PUBLIC", "SystemRoot", "windir", "TEMP", "TMP")
    found = {os.environ[n] for n in names if os.environ.get(n)}
    if os.environ.get("LOCALAPPDATA"):
        found.add(os.path.join(os.environ["LOCALAPPDATA"], "Programs"))
    return frozenset(_norm(p) for p in found)


def _usable_root(path: str) -> bool:
    """A folder that belongs to one app: not a drive root, a container, or part of Windows."""
    if not path:
        return False
    norm = _norm(path)
    if os.path.splitdrive(norm)[1] in ("", "\\") or norm in _containers():
        return False
    windows = os.environ.get("SystemRoot")
    return not (windows and _under(norm, windows))


# --- command lines ---------------------------------------------------------------

def _split_command(raw: str) -> tuple[str, str]:
    """(program, argument text) of a registry command. Unquoted program paths that
    contain spaces are common (C:\\Program Files\\X\\uninst.exe /S): take the
    shortest prefix that is a file."""
    text = os.path.expandvars(raw).strip()
    if not text:
        return "", ""
    first = _argv(text)[0]
    if text.startswith('"'):
        return first, text[len(first) + 2:].strip()
    for stop in [m.start() for m in re.finditer(r"\s", text)] + [len(text)]:
        if os.path.isfile(text[:stop]):
            return text[:stop], text[stop:].strip()
    named = re.match(r"(.+?\.(?:exe|bat|cmd|com))(?=\s|$)", text, re.I)
    if named:
        return named.group(1), text[named.end():].strip()
    return first, text[len(first):].strip()


def _missing(program: str) -> bool:
    """A path that is not there. Bare names (msiexec, rundll32) are left to Windows to find."""
    return bool(re.search(r"[\\/]", program)) and not os.path.isfile(program)


def _is_exe(program: str, *names: str) -> bool:
    return os.path.basename(program).lower() in names


def _tokens(args: str) -> list[str]:
    return _argv("x " + args)[1:] if args.strip() else []


def _has(args: str, switch: str, case: bool = False) -> bool:
    tokens = _tokens(args)
    return switch in tokens if case else switch.lower() in {t.lower() for t in tokens}


def _with(args: str, *switches: str, case: bool = False) -> str:
    return " ".join([args.strip(), *(s for s in switches if not _has(args, s, case))]).strip()


def _kind(program: str, subkey: str) -> str:
    """Installer framework of an uninstaller exe: file name, then embedded markers, then the
    key name (Inno writes <AppId>_is1, but a vendor can ship a different uninstaller under it)."""
    if re.fullmatch(r"unins\d{3}\.exe", os.path.basename(program), re.I):
        return "inno"
    kind = "exe"
    if os.path.isfile(program):
        try:
            kind = installer.detect(Path(program))
        except (installer.InstallError, OSError):
            pass
    return "inno" if kind == "exe" and subkey.lower().endswith("_is1") else kind


def _harden(program: str, args: str, subkey: str) -> str:
    """A vendor-declared quiet command runs as written, except that nothing may restart Windows."""
    if _is_exe(program, "msiexec", "msiexec.exe"):
        return _with(args, "/norestart")
    kind = _kind(program, subkey)
    if kind == "inno":
        return _with(args, "/NORESTART")
    if kind == "burn":
        return _with(args, "/norestart")
    return args


# --- planning --------------------------------------------------------------------

def _is_store(app: InstalledApp) -> bool:
    return app.reg_key.startswith(STORE_PREFIX)


def _store_id(app: InstalledApp) -> str | None:
    return app.reg_key[len(STORE_PREFIX):] if _is_store(app) else None


def _commands(app: InstalledApp) -> tuple[tuple[str, str], tuple[str, str]]:
    return _split_command(app.quiet_uninstall_string), _split_command(app.uninstall_string)


def _is_running_python(app: InstalledApp, winget_id: str | None) -> bool:
    """The Python Upstream runs on, by the name Windows lists it under or, for a Store/winget row
    (no install folder, the publisher is only the id's first word), by its winget id."""
    version = rf"{sys.version_info.major}\.{sys.version_info.minor}"
    if "python software foundation" in app.publisher.lower() and re.match(rf"python {version}\b", app.name.lower()):
        return True
    bare = (winget_id or "").lower().rpartition("\\")[2]
    return bool(re.match(rf"(?:python|pythonsoftwarefoundation)\.python\.{version}(?!\d)", bare))


def _hosts_upstream(app: InstalledApp, programs: list[str], winget_id: str | None = None) -> str | None:
    """'self' when the app is Upstream, 'python' when it holds the interpreter Upstream runs on."""
    if app.ident == "upstream":
        return "self"
    frozen = bool(getattr(sys, "frozen", False))
    run_dir = os.path.dirname(sys.executable)
    roots = [r for r in (folder_path(app.install_location), *(os.path.dirname(p) for p in programs)) if _usable_root(r)]
    if any(_under(mine, root) for root in roots for mine in (sys.executable, sys.base_prefix)):
        return "self" if frozen else "python"
    if _usable_root(run_dir) and any(_under(p, run_dir) for p in programs):
        return "self"
    if not frozen and _is_running_python(app, winget_id):
        return "python"
    return None


def _static_block(app: InstalledApp, winget_id: str | None, programs: list[str]) -> tuple[str, dict] | None:
    values = {"name": app.name}
    host = _hosts_upstream(app, programs, winget_id)
    if host == "self":
        return "Upstream can't remove itself.", {}
    if host == "python":
        return "Upstream runs on this Python, so removing it would break Upstream.", {}
    wid = (winget_id or "").lower()
    name = app.name.lower()
    if wid.startswith(_APP_INSTALLER_IDS) or app.ident in _APP_INSTALLER_NAMES:
        return "{name} installs and removes Store apps, so it can't be removed here.", values
    if wid.startswith(_FRAMEWORKS) or name.startswith(_FRAMEWORKS):
        return "{name} is a Windows component that other apps need, so it can't be removed here.", values
    if app.system_component or _WINDOWS_IDS.fullmatch(wid.rpartition("\\")[2]):
        return "{name} is part of Windows and can't be removed here.", values
    if app.no_remove:
        return "{name} is marked as not removable.", values
    return None


def _blocked(app: InstalledApp, template: str, values: dict) -> UninstallPlan:
    method = "winget" if _is_store(app) else "msi" if app.product_code else "interactive"
    return UninstallPlan(app, method, "", "", False, template, values)


def needs_ack(plan: UninstallPlan) -> bool:
    """Shared runtimes and drivers are removed only after a second, explicit yes."""
    return bool(plan.notes)


def _notes(app: InstalledApp) -> list[tuple[str, dict]]:
    if _SHARED.search(app.name) or (_DEVICE_MAKER.search(app.publisher) and _DEVICE_PART.search(app.name)):
        return [("Other apps may need this. Removing it can stop them from working.", {})]
    return []


def plan(app: InstalledApp, winget_id: str | None = None, prefer_silent: bool = True) -> UninstallPlan:
    """How to remove the app, or why it must not be removed. prefer_silent=False runs the
    registered command as written, so the vendor's own window appears."""
    wid = winget_id or _store_id(app)
    quiet, regular = _commands(app)
    block = _static_block(app, wid, [p for p, _ in (quiet, regular) if p])
    if block:
        return _blocked(app, *block)
    notes = _notes(app)
    no_winget = ("winget isn't available, so {name} can't be removed from here.", {"name": app.name})

    if _is_store(app):
        if not shutil.which("winget"):
            return _blocked(app, *no_winget)
        args = subprocess.list2cmdline(["uninstall", "--id", wid, "--exact", "--source", "winget", "--silent",
                                        "--accept-source-agreements", "--disable-interactivity"])
        return UninstallPlan(app, "winget", "winget", args, True, notes=notes)

    program, args = regular
    code = app.product_code
    if not code and _is_exe(program, "msiexec", "msiexec.exe"):
        found = GUID.search(args)
        code = found.group(0) if found else None
    if code:
        # The registered string is often /I (repair), so it is never used for MSI.
        return UninstallPlan(app, "msi", "msiexec.exe",
                             f"/x {code} /passive /norestart" if prefer_silent else f"/x {code} /norestart",
                             prefer_silent, notes=notes)

    if _is_exe(program, "winget", "winget.exe"):   # portable packages register "winget uninstall --product-code ..."
        if not shutil.which("winget"):
            return _blocked(app, *no_winget)
        return UninstallPlan(app, "winget", "winget", _with(args, "--silent", "--disable-interactivity"),
                             True, notes=notes)

    subkey = app.reg_key.rsplit("\\", 1)[-1]
    q_program, q_args = quiet
    if q_program and not _missing(q_program) and (prefer_silent or not program):
        return UninstallPlan(app, "quiet", q_program, _harden(q_program, q_args, subkey), True, notes=notes)
    if not program:
        if q_program:
            return _blocked(app, "The uninstaller for {name} is missing: {path}", {"name": app.name, "path": q_program})
        return _blocked(app, "{name} has no uninstaller registered with Windows.", {"name": app.name})
    if _missing(program):
        return _blocked(app, "The uninstaller for {name} is missing: {path}", {"name": app.name, "path": program})

    if prefer_silent:
        kind = _kind(program, subkey)
        if kind == "inno":
            return UninstallPlan(app, "inno", program, _with(args, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"),
                                 True, notes=notes)
        if kind == "nullsoft":
            # _?= keeps NSIS from relaunching out of %TEMP%, which would hide its real exit code.
            # It must be last and unquoted, even with spaces in the folder.
            params = _with(args, "/S", case=True)
            folder = os.path.dirname(program)
            if folder and not any(t.startswith("_?=") for t in _tokens(args)):
                params = f"{params} _?={folder}".strip()
            return UninstallPlan(app, "nullsoft", program, params, True, notes=notes)
    return UninstallPlan(app, "interactive", program, args, False, notes=notes)


# --- verifying -------------------------------------------------------------------

def _winget_gone(winget_id: str) -> bool:
    try:
        r = subprocess.run(["winget", "list", "--id", winget_id, "--exact", "--source", "winget", "--disable-interactivity"],
                           capture_output=True, timeout=120, creationflags=installer.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    # Only "no installed package matches" counts; any other failure proves nothing.
    return r.returncode & 0xFFFFFFFF == 0x8A150014


def is_gone(app: InstalledApp, winget_id: str | None = None) -> bool:
    """Windows no longer lists the app (registry for desktop apps, winget for Store rows)."""
    if _is_store(app):
        return _winget_gone(winget_id or _store_id(app))
    return not still_listed(app)


# --- waiting for the uninstaller ---------------------------------------------------

def _watch_roots(plan: UninstallPlan) -> list[str]:
    """Where an uninstaller's helper processes run from: %TEMP% (self-copies), the app's
    own folder and the uninstaller's folder (Package Cache for Burn bundles)."""
    roots = [os.environ[v] for v in ("TEMP", "TMP") if os.environ.get(v)]
    roots += [r for r in (folder_path(plan.app.install_location), os.path.dirname(plan.file)) if _usable_root(r)]
    return list({_norm(os.path.realpath(r)) for r in roots})


def _active(since: float, roots: list[str], baseline: set[int], family: set[int], plan: UninstallPlan) -> list[_Proc]:
    """Processes started after the launch that still run from one of the roots. When Windows
    refuses to describe a process (an elevated child), fall back on the snapshot: a new process
    that is a child of one already followed, or that is named like an uninstaller's helper."""
    own = {os.path.basename(plan.file).lower()} - {"msiexec.exe"}   # the Installer service lingers for minutes
    found = []
    for proc in _snapshot():
        name = proc.name.lower()
        if proc.pid in (0, 4, os.getpid()) or name in _IGNORED:
            continue
        info = _inspect(proc.pid)
        if info is None:
            hit = proc.pid not in baseline and (proc.ppid in family or name in own or bool(_HELPER_NAME.search(name)))
        else:
            path, started = info
            hit = started >= since and bool(path) and any(_under(os.path.realpath(path), r) for r in roots)
        if hit:
            found.append(proc)
    family.update(p.pid for p in found)
    return found


def _settle(plan: UninstallPlan, since: float, baseline: set[int], progress, patient: bool) -> bool:
    """Wait until the app is gone: while helper processes still run (at most SETTLE_MAX_SECONDS),
    then a short grace period when the uninstaller reported success."""
    app, wid = plan.app, _store_id(plan.app)
    roots, family = _watch_roots(plan), set()
    progress("Checking that {name} is gone", name=app.name)
    if is_gone(app, wid):
        return True
    progress("Waiting for the uninstaller to finish")
    deadline = time.monotonic() + SETTLE_MAX_SECONDS
    seen: set[int] = set()
    while time.monotonic() < deadline:
        live = _active(since, roots, baseline, family, plan)
        if not live:
            break
        for proc in live:
            if proc.pid not in seen:
                seen.add(proc.pid)
                log.info("%s: waiting for %s (pid %d)", app.name, proc.name, proc.pid)
        time.sleep(POLL_SECONDS)
        if is_gone(app, wid):
            return True
    else:
        log.warning("%s: still waiting after %d minutes, giving up", app.name, SETTLE_MAX_SECONDS // 60)
    progress("Checking that {name} is gone", name=app.name)
    end = time.monotonic() + (GRACE_SECONDS if patient else 0)
    while not is_gone(app, wid):
        if time.monotonic() >= end:
            return False
        time.sleep(1)
    return True


def _run_winget(plan: UninstallPlan) -> int:
    program = shutil.which(plan.file)
    if not program:
        raise FileNotFoundError(plan.file)
    proc = subprocess.Popen([program, *_argv("x " + plan.params)[1:]], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=installer.CREATE_NO_WINDOW)
    out, _ = proc.communicate()
    for line in re.split(rb"[\r\n]+", out):
        text = line.decode("utf-8", "replace").strip()
        if re.search(r"[A-Za-z]{3,}", text):
            log.info("winget: %s", text)
    return proc.returncode & 0xFFFFFFFF


def _code_text(code: int) -> str:
    return f"0x{code:08X}" if code > 0xFFFF else str(code)


def _failure(plan: UninstallPlan, code: int) -> tuple[str, dict]:
    if plan.method == "winget":
        return "winget exited with {code}", {"code": f"0x{code:08X}"}
    return "The uninstaller exited with code {code}", {"code": _code_text(code)}


def run(plan: UninstallPlan, progress) -> tuple[str, str, dict]:
    """Uninstall and verify. progress(template, **values). Returns (outcome, template, values),
    outcome being removed | still_listed | cancelled | busy | reboot_required | failed."""
    app = plan.app
    name = {"name": app.name}
    log.info("%s: uninstall via %s: %s %s", app.name, plan.method, plan.file, plan.params)
    if plan.blocked:
        log.warning("%s: refused: %s", app.name, english(plan.blocked, plan.blocked_values))
        return "failed", plan.blocked, plan.blocked_values
    block = _static_block(app, _store_id(app), [p for p, _ in _commands(app) if p])
    if block or not plan.file:
        template, values = block or ("{name} has no uninstaller registered with Windows.", name)
        log.warning("%s: refused: %s", app.name, english(template, values))
        return "failed", template, values

    baseline = {p.pid for p in _snapshot()}
    since = time.time() - 0.1
    progress("Uninstalling (winget)" if plan.method == "winget" else "Uninstalling (Windows may ask for permission)")
    try:
        code = _run_winget(plan) if plan.method == "winget" else installer._shell_run(plan.file, plan.params)
    except installer.Cancelled as e:
        log.info("%s: administrator permission was declined", app.name)
        return "cancelled", e.template, e.values
    except installer.InstallError as e:
        log.warning("%s: could not start the uninstaller: %s", app.name, e)
        return "failed", "Couldn't start the uninstaller (Windows error {code}).", {"code": e.values.get("code", 0)}
    except FileNotFoundError:
        log.warning("%s: winget was not found", app.name)
        return "failed", "winget isn't available, so {name} can't be removed from here.", name
    except Exception as e:
        log.exception("%s: uninstall crashed", app.name)
        return "failed", "{error}", {"error": str(e)}
    log.info("%s: the uninstaller exited with %s", app.name, _code_text(code))

    if code in _CANCELLED or (plan.method == "nullsoft" and code == 1):
        return "cancelled", "The uninstall was cancelled", {}
    if code == _BUSY:
        return "busy", "Another installation is running. Try again when it has finished.", {}
    if code in _ALREADY_GONE:
        if is_gone(app, _store_id(app)):
            log.info("%s: the system had already removed it", app.name)
            return "removed", "{name} was removed", name
        return "failed", *_failure(plan, code)

    gone = _settle(plan, since, baseline, progress, patient=code == 0 or code in _REBOOT)
    log.info("%s: %s", app.name, "no longer listed" if gone else "still listed")
    if code in _REBOOT:
        return "reboot_required", "Restart Windows to finish removing {name}", name
    if gone:
        if code:
            return "removed", "{name} was removed (the uninstaller reported {code})", {**name, "code": _code_text(code)}
        return "removed", "{name} was removed", name
    if code == 0:
        return "still_listed", "Windows still lists {name}", name
    return "failed", *_failure(plan, code)
