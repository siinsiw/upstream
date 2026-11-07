"""Remove leftovers after an uninstall, with a backup and an undo. Owner: the leftover-cleanup feature.

Only the items run() is given are touched, and only after leftovers.py proposed them. Each one is checked
again right before it goes (guard.py, the other installed apps, links, a size that changed since the scan),
backed up into DATA_DIR/quarantine/<id>/ and only then removed:

    manifest.json   the app, the time and every item with its original place and where its backup is
    files/<drive>/  folders, files and shortcuts, MOVED here under their original path
                    (copied and deleted when the item lives on another drive)
    registry/*.reg  registry keys and services, exported with reg.exe

A startup entry is a single registry value, so it is written into the manifest. restore() puts everything
back and never overwrites what exists now. Nothing here raises for a single item: a refusal or failure is
returned in CleanupResult.failed with an English template (the UI translates it).

Rights: Upstream never raises its own. What needs administrator rights (HKLM, services, many Program Files
folders) fails with a clear message when Upstream is not elevated."""
from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from . import guard, leftovers
from .config import DATA_DIR
from .i18n import N_
from .installer import CREATE_NO_WINDOW, _powershell, _system_tool
from .leftovers import Leftover

log = logging.getLogger("upstream.cleanup")

QUARANTINE_DIR = DATA_DIR / "quarantine"
MANIFEST = "manifest.json"

# Reasons and failures are English templates, translated when shown.
T_CHANGED = N_("It changed after the list was made, so it was left in place")
T_LINK_INSIDE = N_("It contains a link to somewhere else, which Upstream never follows, so it was left in place")
T_IN_USE = N_("A program is using it. Close the program and try again.")
T_ADMIN = N_("It needs administrator rights. Upstream never raises its own rights: start it as administrator to remove this.")
T_DENIED = N_("Windows denied access to it. A program may be using it.")
T_NO_SPACE = N_("There is not enough free disk space for the backup, so it was left in place")
T_BACKUP_FAILED = N_("The backup failed, so nothing was removed: {error}")
T_PARTIAL = N_("Only part of it could be removed. The rest is in use and stays where it is.")
T_FAILED = N_("Couldn't remove it: {error}")
T_SERVICE_FAILED = N_("Windows refused to remove the service: {error}")
T_UNKNOWN_KIND = N_("Upstream doesn't know how to remove this kind of item")
R_EXISTS = N_("It already exists at the original place, so it was not overwritten")
R_SOME_EXIST = N_("Files that already exist at the original place were left as they are")
R_MISSING = N_("The backup of this item is missing")
R_UNREADABLE = N_("This backup can't be read")
R_SERVICE = N_("A service can't be restored from a backup. Reinstall the app to get it back.")
R_RESTORE_FAILED = N_("Couldn't restore it: {error}")
P_POINT = N_("Creating a Windows restore point…")
P_REMOVE = N_("Removing {item}")
P_RESTORE = N_("Restoring {item}")

REASONS = tuple(v for k, v in sorted(globals().items()) if k[:2] in ("T_", "R_") and isinstance(v, str))

_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
_HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
_LIVE = ("pending", "removed", "partial")    # entries that still have something to restore
_PATH_KINDS = ("folder", "file", "shortcut")
_ATTR_REPARSE = 0x400
_WIN_ACCESS_DENIED, _WIN_SHARING, _WIN_LOCK, _WIN_NOT_SAME_DEVICE = 5, 32, 33, 17
_ERROR_SERVICE_DOES_NOT_EXIST, _ERROR_SERVICE_MARKED_FOR_DELETE = 1060, 1072
_SPARE_BYTES = 64 * 1024 ** 2    # free space kept in reserve when a backup has to be copied
_RESTORE_POINT_SECONDS = 120

_settings = None
_lock = threading.RLock()

_shell32 = ctypes.WinDLL("shell32")
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_shell32.IsUserAnAdmin.restype = wintypes.BOOL
_advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.OpenSCManagerW.restype = ctypes.c_void_p
_advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.OpenServiceW.restype = ctypes.c_void_p
_advapi32.DeleteService.argtypes = [ctypes.c_void_p]
_advapi32.DeleteService.restype = wintypes.BOOL
_advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
_advapi32.CloseServiceHandle.restype = wintypes.BOOL


@dataclass
class CleanupResult:
    removed: int = 0
    failed: list = field(default_factory=list)      # [(Leftover, English template, values)]
    backup_id: str | None = None                    # None when nothing needed a backup
    restore_point: bool | None = None               # True created, False failed, None not attempted
    freed: int = 0                                  # bytes that were backed up and removed
    already_gone: int = 0                           # listed items that no longer existed
    restart_needed: bool = False                    # a service is only marked for deletion until Windows restarts
    cancelled: bool = False                         # stopped before the last item
    done: list = field(default_factory=list)        # [Leftover] that were removed


@dataclass(frozen=True)
class Backup:
    id: str
    app_name: str
    ts: float
    items: int
    size_bytes: int


@dataclass
class RestoreResult:
    restored: int = 0
    failed: list = field(default_factory=list)      # [(description, English template, values)]
    remaining: int = 0                              # entries that stay in the backup


def configure(settings) -> None:
    """The integrator calls this at start; run() reads cleanup_restore_point from it."""
    global _settings
    _settings = settings


class _Refuse(Exception):
    """This item is not removed; the template says why."""

    def __init__(self, template: str, **values):
        super().__init__(template)
        self.template, self.values = template, values


class _Gone(Exception):
    """The item no longer exists."""


class _Partial(Exception):
    """A copied folder could only be removed in part."""


class _BackupFailed(Exception):
    pass


# ----------------------------------------------------------------------- helpers

def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except OSError:
        return False


def _machine_wide(item: Leftover) -> bool:
    """Where, for a standard user, changes need administrator rights."""
    if item.kind == "service" or (item.kind in ("regkey", "regvalue") and item.location.upper().startswith("HKLM\\")):
        return True
    where = guard.locations()
    machine = [guard.normalize(where[k]) for k in ("programfiles", "programfiles_x86", "programdata", "start_common",
                                                  "desktop_public") if where.get(k)]
    return item.kind in _PATH_KINDS and any(guard.inside(guard.normalize(item.location), root) for root in machine)


def needs_admin(item: Leftover) -> bool:
    """Whether removing this probably needs administrator rights: machine-wide registry keys, services and
    the shared program folders. It is a hint for the confirmation; only HKLM and services are refused up front."""
    return _machine_wide(item)


def item_label(item: Leftover) -> str:
    """A short name for progress lines: the last part of the path or key, or the value name."""
    if item.kind == "regvalue":
        return item.location.split("|", 1)[-1]
    if item.kind in ("regkey", "service"):
        return item.location.rstrip("\\").rsplit("\\", 1)[-1]
    return os.path.basename(item.location.rstrip("\\/")) or item.location


def _stopped(cancel) -> bool:
    flag = getattr(cancel, "is_set", False)
    return bool(flag() if callable(flag) else flag)


def _long(path: str) -> str:
    """Paths past the old 260-character limit need the \\\\?\\ prefix for most file calls."""
    if len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    os.replace(tmp, path)


def _load(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("items"), list) and isinstance(data.get("ts"), (int, float))
    return data if ok and all(isinstance(e, dict) for e in data["items"]) else None


def _is_reparse(st: os.stat_result) -> bool:
    return bool(st.st_file_attributes & _ATTR_REPARSE) or stat.S_ISLNK(st.st_mode)


def _error_text(e: OSError) -> str:
    return e.strerror or str(e)


def _map_oserror(e: OSError, admin: bool, machine: bool = False) -> _Refuse:
    """Access denied means missing rights only in machine-wide places; elsewhere a program usually holds the item."""
    code = getattr(e, "winerror", None)
    if code in (_WIN_SHARING, _WIN_LOCK):
        return _Refuse(T_IN_USE)
    if isinstance(e, PermissionError) or code == _WIN_ACCESS_DENIED:
        return _Refuse(T_ADMIN if machine and not admin else T_DENIED)
    return _Refuse(T_FAILED, error=_error_text(e))


# ------------------------------------------------------------------------ backup folders

class _Session:
    """One backup folder with its manifest, created when the first item is about to go. The manifest is
    rewritten after every change, so a crash leaves a record of what was moved."""

    def __init__(self, app_name: str):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        self.dir = QUARANTINE_DIR / self.id
        self.manifest = {"version": 1, "id": self.id, "app": app_name, "ts": time.time(), "state": "running",
                         "restore_point": None, "items": []}
        self.created = False

    def save(self) -> None:
        if not self.created:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.created = True
        _write_json(self.dir / MANIFEST, self.manifest)

    def add(self, entry: dict) -> dict:
        entry["n"] = len(self.manifest["items"]) + 1
        self.manifest["items"].append(entry)
        self.save()
        return entry

    def drop(self, entry: dict) -> None:
        self.manifest["items"].remove(entry)
        self.save()

    def finish(self) -> None:
        if not self.created:
            return
        if not self.manifest["items"]:
            _rmtree_quiet(self.dir)
            self.created = False
            return
        self.manifest["state"] = "complete"
        self.save()

    def prune(self, folder: Path) -> None:
        """Remove empty folders a failed item left behind, up to the backup folder."""
        while folder != self.dir and self.dir in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                return
            folder = folder.parent


def _backup_dest(folder: Path, original: str) -> Path:
    drive, rest = os.path.splitdrive(original)
    return folder / "files" / (drive.rstrip(":").upper() or "_") / rest.lstrip("\\/")


# --------------------------------------------------------------------------- files

def _rename(src: str, dst: str) -> None:
    os.rename(_long(src), _long(dst))     # never replaces an existing target on Windows


def _cross_volume(e: OSError) -> bool:
    return getattr(e, "winerror", None) == _WIN_NOT_SAME_DEVICE or e.errno == errno.EXDEV


def _tree_stats(path: str) -> tuple[int, int]:
    """(files, bytes) of a folder, never entering a link. A link inside raises _Refuse."""
    files = total = 0
    stack = [path]
    while stack:
        with os.scandir(_long(stack.pop())) as entries:
            for entry in entries:
                st = entry.stat(follow_symlinks=False)
                if _is_reparse(st):
                    raise _Refuse(T_LINK_INSIDE)
                if stat.S_ISDIR(st.st_mode):
                    stack.append(entry.path)
                else:
                    files += 1
                    total += st.st_size
    return files, total


def _copy_tree(src: str, dst: str) -> None:
    os.makedirs(_long(dst))
    with os.scandir(_long(src)) as entries:
        for entry in entries:
            target = os.path.join(dst, entry.name)
            if entry.is_dir(follow_symlinks=False):
                _copy_tree(entry.path, target)
            else:
                shutil.copy2(entry.path, _long(target))


def _unlink(path: str) -> None:
    try:
        os.unlink(_long(path))
    except PermissionError:
        os.chmod(_long(path), stat.S_IWRITE)    # a read-only file blocks deletion; a locked one still raises
        os.unlink(_long(path))


def _remove_tree(path: str) -> None:
    def clear(func, target, exc):
        if isinstance(exc, PermissionError) and func in (os.unlink, os.remove, os.rmdir):
            os.chmod(target, stat.S_IWRITE)
            func(target)
        else:
            raise exc

    shutil.rmtree(_long(path), onexc=clear)


def _move(src: str, dst: str, is_dir: bool) -> None:
    """Move across a volume boundary too: copy, check, then delete the original. A failure to delete a
    copied file leaves the original untouched and the copy removed; for a folder that lost only part of its
    files the copy is kept (_Partial)."""
    os.makedirs(_long(os.path.dirname(dst)), exist_ok=True)
    try:
        _rename(src, dst)
        return
    except OSError as e:
        if not _cross_volume(e):
            raise
    top = os.lstat(_long(src))
    if _is_reparse(top):    # swapped for a link since the check: copying would follow it
        raise _Refuse(leftovers.R_LINK)
    files, size = _tree_stats(src) if is_dir else (1, top.st_size)
    if size + _SPARE_BYTES > shutil.disk_usage(os.path.dirname(dst)).free:
        raise _Refuse(T_NO_SPACE)
    try:
        if is_dir:
            _copy_tree(src, dst)
        else:
            shutil.copy2(_long(src), _long(dst))
        if (_tree_stats(dst) if is_dir else (1, os.lstat(_long(dst)).st_size)) != (files, size):
            raise OSError("the copy is not the same size as the original")
    except OSError as e:
        _discard(dst, is_dir)
        raise _BackupFailed(_error_text(e)) from e
    except _Refuse:
        _discard(dst, is_dir)
        raise
    try:
        _remove_tree(src) if is_dir else _unlink(src)
    except OSError:
        if is_dir:
            try:
                unchanged = _tree_stats(src) == (files, size)
            except (OSError, _Refuse):
                unchanged = False
            if not unchanged:
                raise _Partial from None
        _discard(dst, is_dir)
        raise


def _rmtree_quiet(path: Path) -> None:
    """Delete one of Upstream's own backup folders, read-only files included."""
    try:
        _remove_tree(str(path))
    except OSError as e:
        log.warning("Could not delete %s: %s", path, e)


def _discard(path: str, is_dir: bool) -> None:
    try:
        _remove_tree(path) if is_dir else _unlink(path)
    except OSError:
        log.warning("Could not remove the unfinished backup %s", path)


def _check_path(item: Leftover, path: str, others: list[tuple[str, str]]) -> os.stat_result:
    why = guard.protected_path(path)
    if why:
        raise _Refuse(why)
    norm = guard.normalize(path)
    for root, name in others:
        if guard.inside(norm, root) or guard.inside(root, norm):
            raise _Refuse(leftovers.R_INSIDE_OTHER, other=name)
    try:
        st = os.lstat(_long(path))
    except FileNotFoundError:
        raise _Gone from None
    if _is_reparse(st) or guard.normalize(os.path.realpath(path)) != norm:
        raise _Refuse(leftovers.R_LINK)
    if stat.S_ISDIR(st.st_mode) != (item.kind == "folder"):
        raise _Refuse(T_CHANGED)
    if item.kind != "folder" and item.size is not None and st.st_size != item.size:
        raise _Refuse(T_CHANGED)
    return st


def _remove_path(item: Leftover, session: _Session, others, admin: bool) -> int:
    path = os.path.normpath(item.location)
    _check_path(item, path, others)
    is_dir = item.kind == "folder"
    dest = _backup_dest(session.dir, path)
    if os.path.lexists(dest):
        raise _Refuse(T_BACKUP_FAILED, error="the backup place is taken")
    entry = session.add({"kind": item.kind, "location": path, "backup": dest.relative_to(session.dir).as_posix(),
                         "reason": item.reason, "reason_values": item.reason_values, "status": "pending", "bytes": 0})
    try:
        _move(path, str(dest), is_dir)
    except _Partial:
        entry["status"] = "partial"
        session.save()
        raise _Refuse(T_PARTIAL) from None
    except _Refuse:
        session.drop(entry)
        session.prune(dest.parent)
        raise
    except _BackupFailed as e:
        session.drop(entry)
        session.prune(dest.parent)
        raise _Refuse(T_BACKUP_FAILED, error=str(e)) from None
    except OSError as e:
        session.drop(entry)
        session.prune(dest.parent)
        raise _map_oserror(e, admin, _machine_wide(item)) from None
    size = item.size if item.size is not None else (leftovers._measure(str(dest))[0] or 0)
    entry["status"], entry["bytes"] = "removed", size
    session.save()
    return size


# ------------------------------------------------------------------------- registry

def _split_location(location: str) -> tuple[int, str]:
    head, _, sub = location.partition("\\")
    return _HIVES[head.upper()], sub


def _open(hive: int, sub: str, access: int):
    return winreg.OpenKey(hive, sub, 0, access | winreg.KEY_WOW64_64KEY)


def key_exists(location: str) -> bool:
    try:
        hive, sub = _split_location(location)
        with _open(hive, sub, winreg.KEY_READ):
            return True
    except (OSError, KeyError):
        return False


def _proc_error(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout or b"").decode("mbcs", "replace").strip().splitlines()
    return text[0] if text else f"exit code {r.returncode}"


def _reg_export(location: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([_system_tool("reg.exe"), "export", location, str(dest), "/y", "/reg:64"],
                       capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
    if r.returncode or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise _BackupFailed(_proc_error(r))


def _reg_import(file: Path) -> None:
    r = subprocess.run([_system_tool("reg.exe"), "import", str(file), "/reg:64"],
                       capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
    if r.returncode:
        raise OSError(_proc_error(r))


def _reg_file_inside(file: Path, location: str) -> bool:
    """Every key a .reg file would write lies at or below `location`, so importing it cannot touch anything else."""
    names = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}
    head, _, sub = location.partition("\\")
    top = (names[head.upper()] + "\\" + sub).lower()
    try:
        text = file.read_text("utf-16")
    except (OSError, UnicodeError):
        return False
    keys = [line.strip()[1:-1].lower() for line in text.splitlines() if line.startswith("[") and line.rstrip().endswith("]")]
    return bool(keys) and all(k == top or k.startswith(top + "\\") for k in keys)


def _delete_key_tree(hive: int, sub: str, depth: int = 0) -> None:
    if depth > 64:
        raise OSError("the registry key is nested too deeply")
    names = []
    with _open(hive, sub, winreg.KEY_ENUMERATE_SUB_KEYS) as key:
        while True:
            try:
                names.append(winreg.EnumKey(key, len(names)))
            except OSError:
                break
    for name in names:
        _delete_key_tree(hive, f"{sub}\\{name}", depth + 1)
    winreg.DeleteKeyEx(hive, sub, winreg.KEY_WOW64_64KEY)


def _check_key(item: Leftover) -> None:
    location = item.location
    why = guard.protected_key(location) if isinstance(location, str) else guard.R_KEY_UNKNOWN
    if not why and any(ord(c) < 32 or c == '"' for c in location):
        why = guard.R_KEY_UNKNOWN
    parsed = guard.split_key(location) if not why else None
    if not why and parsed and parsed[0] == "hklm" and parsed[1][:1] == ["system"] and item.kind != "service":
        why = guard.R_KEY_SYSTEM
    if not why and item.kind == "service" and not (parsed and parsed[0] == "hklm" and parsed[1][:1] == ["system"]):
        why = guard.R_KEY_UNKNOWN
    if why:
        raise _Refuse(why)


def _read_value(hive: int, sub: str, name: str) -> dict:
    with _open(hive, sub, winreg.KEY_QUERY_VALUE) as key:
        data, kind = winreg.QueryValueEx(key, name)
    return _encode_value(name, kind, data)


def _encode_value(name: str, kind: int, data) -> dict:
    if isinstance(data, bytes):
        return {"name": name, "type": kind, "bytes": data.hex()}
    return {"name": name, "type": kind, "data": data}


def _decode_value(value: dict):
    return bytes.fromhex(value["bytes"]) if "bytes" in value else value.get("data")


def _remove_key(item: Leftover, session: _Session, admin: bool) -> int:
    _check_key(item)
    if not admin and _machine_wide(item):
        raise _Refuse(T_ADMIN)
    hive, sub = _split_location(item.location)
    if not key_exists(item.location):
        raise _Gone
    entry = session.add({"kind": item.kind, "location": item.location, "reason": item.reason,
                         "reason_values": item.reason_values, "status": "pending", "bytes": 0})
    dest = session.dir / "registry" / f"{entry['n']}.reg"
    entry["reg"] = dest.relative_to(session.dir).as_posix()
    session.save()
    try:
        _reg_export(item.location, dest)
    except (_BackupFailed, OSError, subprocess.SubprocessError) as e:
        session.drop(entry)
        raise _Refuse(T_BACKUP_FAILED, error=str(e)) from None
    try:
        if item.kind == "service":
            _remove_service(item)
        else:
            _delete_key_tree(hive, sub)
    except _Refuse:
        session.drop(entry)
        dest.unlink(missing_ok=True)
        raise
    except OSError as e:
        session.drop(entry)
        dest.unlink(missing_ok=True)
        raise _map_oserror(e, admin) from None
    entry["status"], entry["bytes"] = "removed", dest.stat().st_size
    session.save()
    return 0


def _delete_service(name: str) -> int:
    """0 when Windows accepted the deletion, else the Windows error code."""
    scm = _advapi32.OpenSCManagerW(None, None, 0x0001)              # SC_MANAGER_CONNECT
    if not scm:
        return ctypes.get_last_error()
    try:
        service = _advapi32.OpenServiceW(scm, name, 0x00010000)     # DELETE
        if not service:
            return ctypes.get_last_error()
        try:
            return 0 if _advapi32.DeleteService(service) else ctypes.get_last_error()
        finally:
            _advapi32.CloseServiceHandle(service)
    finally:
        _advapi32.CloseServiceHandle(scm)


def _remove_service(item: Leftover) -> None:
    """Delete through the service manager: it is the only supported way, and a running service is only
    marked for deletion until it stops (the registry key stays until then)."""
    name = item.location.rstrip("\\").rsplit("\\", 1)[-1]
    code = _delete_service(name)
    if code in (0, _ERROR_SERVICE_DOES_NOT_EXIST, _ERROR_SERVICE_MARKED_FOR_DELETE):
        return
    if code == _WIN_ACCESS_DENIED:
        raise _Refuse(T_ADMIN)
    raise _Refuse(T_SERVICE_FAILED, error=f"Windows error {code}")


def _remove_value(item: Leftover, session: _Session, admin: bool) -> int:
    key, _, name = item.location.partition("|")
    why = guard.protected_value(key, name)
    if why:
        raise _Refuse(why)
    if not admin and _machine_wide(item):
        raise _Refuse(T_ADMIN)
    hive, sub = _split_location(key)
    try:
        value = _read_value(hive, sub, name)
    except FileNotFoundError:
        raise _Gone from None
    entry = session.add({"kind": "regvalue", "location": item.location, "reason": item.reason,
                         "reason_values": item.reason_values, "value": value, "status": "pending", "bytes": 0})
    try:
        with _open(hive, sub, winreg.KEY_SET_VALUE) as handle:
            winreg.DeleteValue(handle, name)
    except OSError as e:
        session.drop(entry)
        raise _map_oserror(e, admin) from None
    entry["status"] = "removed"
    session.save()
    return 0


# ------------------------------------------------------------------------ restore point

def _create_restore_point(app_name: str, cancel) -> bool:
    """True only when Windows really made a point: it needs administrator rights and System Protection, and
    refuses a second one within 24 hours."""
    if not is_admin():
        return False
    script = ("try { Checkpoint-Computer -Description $env:UPSTREAM_DESC -RestorePointType MODIFY_SETTINGS "
              "-WarningAction Stop -ErrorAction Stop; exit 0 } catch { exit 1 }")
    command, env = _powershell(script, Path("."))
    env["UPSTREAM_DESC"] = re.sub(r"[\x00-\x1f]", " ", f"Upstream: removed leftovers of {app_name}")[:100]
    try:
        proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
    except OSError:
        return False
    deadline = time.monotonic() + _RESTORE_POINT_SECONDS
    while True:
        try:
            return proc.wait(1) == 0
        except subprocess.TimeoutExpired:
            if _stopped(cancel) or time.monotonic() > deadline:
                proc.kill()     # Upstream's own PowerShell, nobody else's process
                return False


# ------------------------------------------------------------------------------ run

def _installed_apps() -> list:
    try:
        from .inventory import read_installed_apps
        return read_installed_apps()
    except Exception:
        log.exception("could not read the installed apps for the ownership check")
        return []


def _other_roots(apps) -> list[tuple[str, str]]:
    """(normalized folder, name) of the installed apps with a folder of their own."""
    roots = []
    for app in apps:
        folder = leftovers._clean(app.install_location)
        if folder and not guard.protected_path(folder):
            roots.append((guard.normalize(folder), app.name))
    return roots


def _order(item: Leftover) -> tuple[int, int]:
    """Parents before children, so a file inside a folder that is moved anyway is simply already gone."""
    if item.kind in _PATH_KINDS:
        return 0, len(guard.normalize(item.location))
    return {"regvalue": 1, "regkey": 2, "service": 3}.get(item.kind, 4), len(item.location)


def _identity(item: Leftover) -> tuple[str, str]:
    return item.kind, guard.normalize(item.location) if item.kind in _PATH_KINDS else item.location.lower()


def _remove_one(item: Leftover, session: _Session, others, admin: bool) -> int:
    if item.confidence == leftovers.PROTECTED:
        raise _Refuse(item.reason, **item.reason_values)
    if item.kind in _PATH_KINDS:
        return _remove_path(item, session, others, admin)
    if item.kind in ("regkey", "service"):
        return _remove_key(item, session, admin)
    if item.kind == "regvalue":
        return _remove_value(item, session, admin)
    raise _Refuse(T_UNKNOWN_KIND)


def run(items: list[Leftover], app_name: str, progress, cancel=None, *, apps: list | None = None,
        restore_point: bool | None = None) -> CleanupResult:
    """Back up then remove the ticked leftovers. progress(template, percent, **values), percent -1 when unknown.
    `cancel` stops before the next item. `apps` are the installed apps whose folders are off limits (read from
    the registry when None); `restore_point` overrides the cleanup_restore_point setting."""
    result = CleanupResult()
    todo, seen = [], set()
    for item in sorted(items, key=_order):
        if _identity(item) not in seen:
            seen.add(_identity(item))
            todo.append(item)
    if not todo:
        return result
    with _lock:
        others = _other_roots(_installed_apps() if apps is None else apps)
        admin = is_admin()
        session = _Session(app_name)
        wanted = restore_point
        if wanted is None:
            wanted = bool(_settings.get("cleanup_restore_point", False)) if _settings is not None else False
        try:
            if wanted and any(i.confidence != leftovers.PROTECTED for i in todo):
                progress(P_POINT, -1)
                result.restore_point = _create_restore_point(app_name, cancel)
                session.manifest["restore_point"] = result.restore_point
            for index, item in enumerate(todo):
                if _stopped(cancel):
                    result.cancelled = True
                    break
                progress(P_REMOVE, index * 100 // len(todo), item=item_label(item))
                try:
                    freed = _remove_one(item, session, others, admin)
                except _Gone:
                    result.already_gone += 1
                except _Refuse as e:
                    log.info("%s: left %s: %s", app_name, item.location, e.template.format_map(e.values))
                    result.failed.append((item, e.template, e.values))
                except Exception as e:
                    log.exception("%s: removing %s failed", app_name, item.location)
                    result.failed.append((item, T_FAILED, {"error": str(e)}))
                else:
                    result.removed += 1
                    result.freed += freed
                    result.done.append(item)
                    if item.kind == "service" and key_exists(item.location):
                        result.restart_needed = True
                    log.info("%s: removed %s", app_name, item.location)
        finally:
            session.finish()
        result.backup_id = session.id if session.created else None
    return result


# ------------------------------------------------------------------------ backups list

def backups() -> list[Backup]:
    """Backups that still hold something to restore, newest first."""
    found = []
    try:
        folders = [p for p in QUARANTINE_DIR.iterdir() if p.is_dir() and _ID.match(p.name)]
    except OSError:
        return []
    for folder in folders:
        data = _load(folder)
        live = [e for e in data["items"] if e.get("status") in _LIVE] if data else []
        if live:
            found.append(Backup(folder.name, str(data.get("app", "")), float(data["ts"]), len(live),
                                sum(int(e.get("bytes") or 0) for e in live)))
    return sorted(found, key=lambda b: b.ts, reverse=True)


def _backup_folder(backup_id: str) -> Path | None:
    return QUARANTINE_DIR / backup_id if _ID.match(backup_id or "") else None


def delete_backup(backup_id: str) -> None:
    """Delete one backup folder (only one of Upstream's own: the id is checked)."""
    folder = _backup_folder(backup_id)
    if folder is not None and folder.is_dir():
        _rmtree_quiet(folder)
        log.info("Deleted backup %s", backup_id)


def purge_old(days: int) -> int:
    """Delete backups older than `days`; returns how many were removed. 0 or less keeps everything."""
    if days <= 0:
        return 0
    limit, count = time.time() - days * 86400, 0
    try:
        folders = [p for p in QUARANTINE_DIR.iterdir() if p.is_dir() and _ID.match(p.name)]
    except OSError:
        return 0
    for folder in folders:
        data = _load(folder)
        try:
            when = float(data["ts"]) if data else folder.stat().st_mtime
        except OSError:
            continue
        if when < limit:
            delete_backup(folder.name)
            count += not folder.exists()
    return count


# ---------------------------------------------------------------------------- restore

def _restore_path(entry: dict, folder: Path) -> str | None:
    """Put one file, folder or shortcut back. Returns a template for what was left, None when all is back."""
    original = entry["location"]
    why = guard.protected_path(original)
    if why:
        raise _Refuse(why)
    source = folder / entry["backup"]
    if not os.path.lexists(_long(str(source))):
        if entry.get("status") == "pending":    # the move never happened
            return None
        raise _Refuse(R_MISSING)
    is_dir = source.is_dir() and not source.is_symlink()
    if not os.path.lexists(_long(original)):
        _move(str(source), original, is_dir)
        return None
    if not is_dir or not os.path.isdir(_long(original)):
        return R_EXISTS
    return R_SOME_EXIST if _merge(str(source), original) else None


def _merge(src: str, dst: str) -> int:
    """Move what is missing from the backup folder into the existing one; returns how many entries stayed."""
    left = 0
    for entry in os.scandir(_long(src)):
        target = os.path.join(dst, entry.name)
        is_dir = entry.is_dir(follow_symlinks=False)
        if not os.path.lexists(_long(target)):
            _move(entry.path, target, is_dir)
        elif is_dir and os.path.isdir(_long(target)) and not _is_reparse(os.lstat(_long(target))):
            left += _merge(entry.path, target)
        else:
            left += 1
    try:
        os.rmdir(_long(src))
    except OSError:
        pass
    return left


def _restore_key(entry: dict, folder: Path) -> str | None:
    location = entry["location"]
    why = guard.protected_key(location)
    if why:
        raise _Refuse(why)
    if key_exists(location):
        return None if entry.get("status") == "pending" else R_EXISTS
    reg = folder / entry["reg"]
    if not reg.is_file():
        raise _Refuse(R_MISSING)
    if not _reg_file_inside(reg, location):
        raise _Refuse(R_UNREADABLE)
    _reg_import(reg)
    return None


def _restore_value(entry: dict) -> str | None:
    key, _, name = entry["location"].partition("|")
    why = guard.protected_value(key, name)
    if why:
        raise _Refuse(why)
    hive, sub = _split_location(key)
    value = entry["value"]
    try:
        _read_value(hive, sub, name)
        return None if entry.get("status") == "pending" else R_EXISTS
    except FileNotFoundError:
        pass
    with winreg.CreateKeyEx(hive, sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as handle:
        winreg.SetValueEx(handle, name, 0, int(value["type"]), _decode_value(value))
    return None


def restore(backup_id: str, progress=None) -> RestoreResult:
    """Put everything in a backup back: files and folders where they were, registry keys and startup entries
    again. Whatever exists now is never overwritten; it is skipped and reported."""
    result = RestoreResult()
    folder = _backup_folder(backup_id)
    data = _load(folder) if folder is not None else None
    if data is None:
        result.failed.append((backup_id, R_UNREADABLE, {}))
        return result
    progress = progress or (lambda *a, **k: None)
    with _lock:
        todo = [e for e in reversed(data["items"]) if e.get("status") in _LIVE]
        for index, entry in enumerate(todo):
            where = str(entry.get("location", ""))
            progress(P_RESTORE, index * 100 // len(todo),
                     item=where.split("|", 1)[-1] if entry.get("kind") == "regvalue"
                     else os.path.basename(where.rstrip("\\/")) or where)
            try:
                if entry["kind"] == "service":
                    entry["status"] = "manual"
                    result.failed.append((where, R_SERVICE, {}))
                    continue
                if entry["kind"] in _PATH_KINDS:
                    left = _restore_path(entry, folder)
                elif entry["kind"] == "regkey":
                    left = _restore_key(entry, folder)
                else:
                    left = _restore_value(entry)
            except _Refuse as e:
                result.failed.append((where, e.template, e.values))
                continue
            except _Partial:
                result.failed.append((where, R_RESTORE_FAILED, {"error": "part of it is in use"}))
                continue
            except (OSError, _BackupFailed, subprocess.SubprocessError, KeyError, ValueError) as e:
                log.warning("Could not restore %s: %s", where, e)
                result.failed.append((where, R_RESTORE_FAILED, {"error": _error_text(e) if isinstance(e, OSError) else str(e)}))
                continue
            if left:
                result.failed.append((where, left, {}))
                continue
            entry["status"] = "restored"
            result.restored += 1
        result.remaining = sum(1 for e in data["items"] if e.get("status") in _LIVE)
        if result.remaining:
            data["state"] = "partial"
            _write_json(folder / MANIFEST, data)
        else:
            _rmtree_quiet(folder)
        log.info("Restored backup %s: %d restored, %d left", backup_id, result.restored, len(result.failed))
    return result
