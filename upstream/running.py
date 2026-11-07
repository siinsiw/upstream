"""Close the running app before its files are replaced.

find_running() lists the processes whose program file lives in a folder that belongs to the app alone (never a
folder shared with other apps: Tools, Games, a vendor or Steam library folder); close_for_update() follows the
close_apps_policy setting: ask (default), auto (close without asking) or never (leave it to the installer).
Closing is graceful first (WM_CLOSE to the top-level windows), then, only if the user agrees, forced.
Everything works on processes find_running() returned and re-checks a process's identity (image path and start
time) before touching it, so a pid that Windows has since handed to another program is never closed."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from . import guard, inventory
from .errors import Cancelled
from .inventory import InstalledApp, folder_path

log = logging.getLogger("upstream.running")

GRACE_SECONDS = 10.0     # how long the app gets to close itself after WM_CLOSE
KILL_SECONDS = 3.0       # how long TerminateProcess gets to show
INVENTORY_TTL = 30.0     # how long the list of installed apps is reused when checking for shared folders
_POLL = 0.25
_MAX_SUBFOLDERS = 200    # a folder with more subfolders than this is not looked through: treated as shared

_TH32CS_SNAPPROCESS = 0x2
_PROCESS_TERMINATE = 0x1
_QUERY_LIMITED = 0x1000
_STILL_ACTIVE = 259
_WM_CLOSE = 0x10
_GW_OWNER = 4
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PROGRAM_IN_COMMAND = re.compile(r'^\s*(?:"([^"]+)"|(.+?\.exe)(?=\s|$))', re.I)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)


def _declare(lib, name: str, restype, *argtypes):
    func = getattr(lib, name)
    func.argtypes, func.restype = list(argtypes), restype


_declare(_k32, "CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_declare(_k32, "Process32FirstW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
_declare(_k32, "Process32NextW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
_declare(_k32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_declare(_k32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_declare(_k32, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
         ctypes.POINTER(wintypes.DWORD))
_declare(_k32, "GetProcessTimes", wintypes.BOOL, wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
_declare(_k32, "GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_declare(_k32, "TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_declare(_k32, "ProcessIdToSessionId", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_declare(_u32, "EnumWindows", wintypes.BOOL, _ENUMPROC, wintypes.LPARAM)
_declare(_u32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_declare(_u32, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_declare(_u32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_declare(_u32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


@dataclass(frozen=True)
class RunningProcess:
    pid: int
    name: str
    path: str
    started: int = 0      # creation time (FILETIME ticks): tells this process from a later owner of the same pid


# --- install folders ---------------------------------------------------------------

def _program_of(command: str) -> str:
    match = _PROGRAM_IN_COMMAND.match(command or "")
    return os.path.expandvars(match.group(1) or match.group(2)) if match else ""


def _own_folders(app: InstalledApp) -> list[str]:
    """The folders a registry row names: the install location, then those of its icon and uninstallers."""
    return [app.install_location, os.path.dirname(app.icon_path or ""),
            os.path.dirname(_program_of(app.quiet_uninstall_string)),
            os.path.dirname(_program_of(app.uninstall_string))]


_installed_lock = threading.Lock()
_installed_cache: tuple[float, list[tuple[str, str, list[str]]]] | None = None


def _installed_folders() -> list[tuple[str, str, list[str]]]:
    """(registry key, ident, normalized folders) of every installed app, reused for INVENTORY_TTL seconds. A row's
    folders are its install location, or, when it names none, those of its icon and uninstallers (a host program
    such as Steam). Empty when the registry can't be read: the shape of the folder is then the only evidence."""
    global _installed_cache
    with _installed_lock:
        if _installed_cache and time.monotonic() - _installed_cache[0] < INVENTORY_TTL:
            return _installed_cache[1]
        try:
            rows = []
            for other in inventory.read_installed_apps():
                named = _own_folders(other)
                claimed = named[:1] if folder_path(named[0]) else named[1:]
                rows.append((other.reg_key, other.ident,
                             list(dict.fromkeys(guard.normalize(p) for p in map(folder_path, claimed) if p))))
        except Exception:
            log.warning("could not list the installed apps", exc_info=True)
            rows = []
        _installed_cache = (time.monotonic(), rows)
        return rows


def _other_roots(app: InstalledApp) -> list[str]:
    return [root for key, ident, roots in _installed_folders()
            if key != app.reg_key and ident != app.ident for root in roots]


def _umbrella_parents() -> set[str]:
    """Normalized folders whose direct children are usually one app's folder: Program Files, ProgramData and
    %LOCALAPPDATA%\\Programs (drive roots are tested separately)."""
    where = guard.locations()
    parents = {guard.normalize(where[key]) for key in ("programfiles", "programfiles_x86", "programdata",
                                                       "local_programs") if key in where}
    parents.update(c for c in guard.containers()
                   if os.path.basename(c) in ("program files", "program files (x86)", "programdata"))
    return parents


def _direct_child(path: str) -> bool:
    parent = os.path.dirname(path)
    return bool(re.fullmatch(r"[a-z]:\\", parent)) or parent in _umbrella_parents()


def _is_library(path: str) -> bool:
    """Steam's library folders hold every installed game; so does the Steam folder that has a steamapps in it."""
    parts = path.split("\\")
    return parts[-1] == "steamapps" or parts[-2:] == ["steamapps", "common"] or os.path.isdir(path + "\\steamapps")


def _has_exe(folder: str, depth: int = 2) -> bool:
    """Is there a .exe directly in this folder, or (depth 2) directly in one of its subfolders?"""
    subfolders: list[str] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.lower().endswith(".exe") and entry.is_file():
                    return True
                if depth > 1 and entry.is_dir(follow_symlinks=False):
                    subfolders.append(entry.path)
    except OSError:
        return False
    return any(_has_exe(sub, depth - 1) for sub in subfolders[:_MAX_SUBFOLDERS])


def _holds_programs(path: str) -> bool:
    """Do at least two of the folder's subfolders have programs of their own? True too when the folder can't be
    read: what can't be seen is not assumed to be one app."""
    try:
        with os.scandir(path) as entries:
            subfolders = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return True
    if len(subfolders) > _MAX_SUBFOLDERS:
        return True
    found = 0
    for sub in subfolders:
        found += _has_exe(sub)
        if found >= 2:
            return True
    return False


def _holds_own_file(app: InstalledApp, path: str) -> bool:
    """Do the app's uninstaller or icon file lie directly in this normalized folder?"""
    files = [app.icon_path, _program_of(app.quiet_uninstall_string), _program_of(app.uninstall_string)]
    return any(folder_path(os.path.dirname(f)) and guard.normalize(os.path.dirname(f)) == path and os.path.isfile(f)
               for f in files if f)


def _shared_why(app: InstalledApp, path: str, location: str) -> str:
    """Why this normalized folder is not the app's alone, or '' when it is. A folder another installed app lives
    in is shared, and so is a library or a folder that sits directly in a drive root / Program Files / ProgramData
    and holds several programs' folders, unless it is the app's own install location with its own uninstaller or
    icon in it (C:\\Program Files\\Git has subfolders full of programs and is still one app). Unreadable: shared."""
    if _is_library(path):
        return "a games library"
    if any(guard.inside(root, path) for root in _other_roots(app)):
        return "another installed app lives in it"
    if not _direct_child(path) or (path == location and _holds_own_file(app, path)):
        return ""
    return "it holds several programs' folders" if _holds_programs(path) else ""


def install_roots(app: InstalledApp) -> list[str]:
    """Normalized folders that belong to this app alone: its install location and the folders of its icon and
    uninstaller. A folder guard.py would refuse to delete (a drive root, Program Files itself, a user profile,
    Windows, Upstream's own Python...) is too broad to mean 'this app', and so is an umbrella folder (C:\\Tools,
    Program Files\\JetBrains, a Steam library, the folder of the host program a Steam-style row points its icon at):
    its processes would be other apps', which an update must not close."""
    location = folder_path(app.install_location)
    location = guard.normalize(location) if location else ""
    roots: list[str] = []
    for raw in _own_folders(app):
        path = folder_path(raw)
        if not path:
            continue
        norm = guard.normalize(path)
        if norm in roots:
            continue
        reason = guard.protected_path(path) or _shared_why(app, norm, location)
        if reason:
            log.debug("%s: %s is not an install folder (%s)", app.name, path, reason)
            continue
        roots.append(norm)
    return roots


# --- processes ---------------------------------------------------------------------

def _snapshot() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) of every process, from one Toolhelp snapshot."""
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap in (None, _INVALID_HANDLE):
        return []
    rows = []
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return rows


def _session(pid: int) -> int | None:
    out = wintypes.DWORD()
    return out.value if _k32.ProcessIdToSessionId(pid, ctypes.byref(out)) else None


def _describe(handle) -> tuple[str, int]:
    """(program path, creation time) of an open process; ('', 0) when Windows won't tell."""
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    if not _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return "", 0
    created, *spare = (wintypes.FILETIME() for _ in range(4))
    if not _k32.GetProcessTimes(handle, ctypes.byref(created), *map(ctypes.byref, spare)):
        return buf.value, 0
    return buf.value, created.dwHighDateTime << 32 | created.dwLowDateTime


def _own_chain(rows: list[tuple[int, int, str]]) -> set[int]:
    """Upstream's own pid and the pids of everything that started it."""
    parent = {pid: ppid for pid, ppid, _ in rows}
    chain, pid = set(), os.getpid()
    while pid and pid not in chain:
        chain.add(pid)
        pid = parent.get(pid, 0)
    return chain


def find_running(app: InstalledApp) -> list[RunningProcess]:
    """Processes of this Windows session whose program file lies inside the app's install folder. Upstream and
    its parents, services, anything under Windows and processes Windows won't describe (elevated ones) are left out."""
    roots = install_roots(app)
    if not roots:
        return []
    rows = _snapshot()
    skip = _own_chain(rows)
    mine = _session(os.getpid())
    found = []
    for pid, _, _ in rows:
        session = _session(pid)
        if pid in skip or pid <= 4 or session == 0 or (mine is not None and session != mine):
            continue
        handle = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            continue
        try:
            path, started = _describe(handle)
        finally:
            _k32.CloseHandle(handle)
        if not path or not any(guard.inside(guard.normalize(path), root) for root in roots):
            continue
        if guard.protected_path(path) in (guard.R_WINDOWS, guard.R_UPSTREAM):
            continue
        found.append(RunningProcess(pid, os.path.basename(path), path, started))
    return sorted(found, key=lambda p: (p.name.lower(), p.pid))


def _open_same(proc: RunningProcess, access: int = 0):
    """A handle on the very process find_running() saw, or None once it has exited or the pid belongs to another
    program. Holding the handle keeps Windows from reusing the pid meanwhile."""
    handle = _k32.OpenProcess(access | _QUERY_LIMITED, False, proc.pid)
    if not handle:
        return None
    path, started = _describe(handle)
    if path.lower() != proc.path.lower() or (proc.started and started != proc.started):
        _k32.CloseHandle(handle)
        return None
    return handle


def _alive(proc: RunningProcess) -> bool:
    handle = _open_same(proc)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


# --- closing -----------------------------------------------------------------------

def _top_windows(pids: set[int]) -> dict[int, list[tuple[int, bool]]]:
    """(window, visible) of the unowned top-level windows per pid. Owned windows are dialogs: a WM_CLOSE sent
    to a 'Save changes?' box would answer it for the user."""
    found: dict[int, list[tuple[int, bool]]] = {}

    def visit(hwnd, _):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and not _u32.GetWindow(hwnd, _GW_OWNER):
            found.setdefault(pid.value, []).append((hwnd, bool(_u32.IsWindowVisible(hwnd))))
        return True

    _u32.EnumWindows(_ENUMPROC(visit), 0)
    return found


def _post_close(procs: list[RunningProcess]) -> int:
    """Ask each process to close by posting WM_CLOSE to its main windows (to its hidden ones only when it has
    no visible window: tray apps). Returns how many windows were asked."""
    handles = {p.pid: h for p in procs if (h := _open_same(p))}
    try:
        asked = 0
        for wins in _top_windows(set(handles)).values():
            for hwnd in [w for w, shown in wins if shown] or [w for w, _ in wins]:
                asked += bool(_u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))
        return asked
    finally:
        for handle in handles.values():
            _k32.CloseHandle(handle)


def _terminate(procs: list[RunningProcess]):
    for proc in procs:
        handle = _open_same(proc, _PROCESS_TERMINATE)
        if not handle:
            continue
        try:
            if _k32.TerminateProcess(handle, 1):
                log.info("terminated %s (pid %s)", proc.name, proc.pid)
            else:
                log.warning("could not terminate %s (pid %s): error %s", proc.name, proc.pid, ctypes.get_last_error())
        finally:
            _k32.CloseHandle(handle)


def _wait_gone(ctx, procs: list[RunningProcess], seconds: float) -> list[RunningProcess]:
    """The processes still alive after up to `seconds`. Raises Cancelled when the user cancels meanwhile."""
    deadline = time.monotonic() + seconds
    alive = [p for p in procs if _alive(p)]
    while alive and time.monotonic() < deadline:
        ctx.cancel.check()
        ctx.cancel.wait(_POLL)
        alive = [p for p in alive if _alive(p)]
    return alive


def _names(procs: list[RunningProcess]) -> str:
    names = sorted({p.name for p in procs}, key=str.lower)
    return ", ".join(names[:8]) + (", …" if len(names) > 8 else "")


def close_for_update(ctx, app: InstalledApp, progress) -> None:
    """Ask / close the app's processes if any are running. `ctx` is a context.UpdateContext.
    Raises errors.Cancelled when the user declines or the update was cancelled; a cancelled update never closes
    anything. `progress(template, percent, **values)`."""
    ctx.cancel.check()
    policy = ctx.setting("close_apps_policy", "ask")
    if policy == "never":
        return
    procs = find_running(app)
    if not procs:
        return
    log.info("%s is running: %s", app.name, _names(procs))
    if policy != "auto" and not ctx.ask(
            "{name} is running. Close it to update?", yes="Close it", no="Cancel update",
            detail="These will be closed: {processes}. Unsaved changes in them may be lost.",
            name=app.name, processes=_names(procs)):
        raise Cancelled("Update cancelled: {name} is still running.", name=app.name)

    progress("Closing {name}…", -1, name=app.name)
    ctx.cancel.check()
    asked = _post_close(procs)
    left = _wait_gone(ctx, procs, GRACE_SECONDS) if asked else [p for p in procs if _alive(p)]
    if not left:
        return
    log.info("%s did not close by itself: %s", app.name, _names(left))
    if not ctx.ask("{name} did not close. Force it to close?", yes="Force close", no="Cancel update", danger=True,
                   detail="Still open: {processes}. Anything you haven't saved will be lost.",
                   name=app.name, processes=_names(left)):
        raise Cancelled("Update cancelled: {name} is still running.", name=app.name)
    ctx.cancel.check()
    _terminate(left)
    if _wait_gone(ctx, left, KILL_SECONDS):
        raise Cancelled("Update cancelled: {name} is still running.", name=app.name)
