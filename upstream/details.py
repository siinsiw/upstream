"""Facts about one installed app, and its Repair / Modify actions. Owner: the app-details feature.

gather() reads what is cheap (registry values already in InstalledApp, a few file-system checks); the slow
parts are separate calls for a worker thread: folder_size() (a folder walk) and signer_of() (PowerShell).

Repair is offered for Windows Installer products only and runs `msiexec /fa`. Modify runs the vendor's
ModifyPath, or `msiexec /i` for a Windows Installer product, and only when the command line can be read
safely: the program is an existing file with a drive-letter path outside the Windows folder. Nothing is
ever run silently or without the app's own window."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import inplace, installer, inventory, trust
from .errors import InstallError
from .exitcodes import classify
from .i18n import N_
from .inventory import GUID, InstalledApp, folder_path
from .uninstaller import STORE_PREFIX, _is_exe, _split_command, _under, _usable_root

log = logging.getLogger("upstream.details")

FOLDER_SECONDS = 2.0
FOLDER_ENTRIES = 60_000
MAX_LINKS = 6
FAMILIES = {   # product names, shown as they are
    "msi": "Windows Installer (MSI)", "msix": "MSIX / Store", "inno": "Inno Setup", "nullsoft": "NSIS",
    "burn": "WiX Burn", "installshield": "InstallShield", "advancedinstaller": "Advanced Installer",
    "squirrel": "Squirrel",
}
_ATTR_REPARSE = 0x400
_LAUNCHABLE = (".exe", ".com", ".bat", ".cmd")
_OWN_UNINSTALLER = re.compile(r"unin\w*\.exe", re.I)
_MSI_STATES = (3, 4, 5)       # INSTALLSTATE_LOCAL, SOURCE, DEFAULT: installed for this user or machine (msi.h)
_SEE_NOASYNC, _SEE_NO_UI = 0x00000100, 0x00000400
_signed: dict[tuple[str, str], tuple[str, str]] = {}
# Results of repair() that are not failures: the user said no, or stopped waiting.
NEUTRAL = (N_("The repair was cancelled."), N_("Administrator permission (UAC) was declined."),
           N_("Stopped waiting for the installer. It may still be running."))


@dataclass
class AppDetails:
    name: str
    version: str
    publisher: str
    scope: str                       # machine | user
    arch: str
    install_location: str = ""       # a folder that exists and belongs to this app; '' when none is known
    install_date: str = ""           # YYYY-MM-DD, empty when unknown
    size_bytes: int = 0              # 0 = unknown
    uninstall_string: str = ""
    urls: list = field(default_factory=list)
    can_repair: bool = False
    can_modify: bool = False
    installer_kind: str = ""         # what the Uninstall key says; installer_family() also reads the uninstaller file
    size_approx: bool = False        # the folder walk was cut short: size_bytes is "at least"


# --- small helpers ----------------------------------------------------------------

def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def format_date(stamp: str) -> str:
    """'20261102' (InstalledApp.install_date) as '2026-11-02'; '' for anything else."""
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}" if re.fullmatch(r"\d{8}", stamp or "") else ""


def family_label(kind: str) -> str:
    return FAMILIES.get(kind, "")


def installer_family(app: InstalledApp) -> str:
    """The installer framework of the copy on this PC ('' when unknown). Reads the uninstaller file once: call from a thread."""
    try:
        return inplace.installed_kind(app)
    except Exception:    # a best-effort look at a file must not break the dialog
        log.warning("%s: could not tell the installer family", app.name, exc_info=True)
        return app.install_kind


def _in_cache(folder: str) -> bool:
    """Inside a Package Cache folder, or in a {GUID} folder (where Burn and Windows Installer keep their copies)."""
    parts = folder.replace("/", "\\").lower().split("\\")
    return "package cache" in parts or bool(GUID.fullmatch(parts[-1].upper()))


def install_folder(app: InstalledApp) -> str:
    """The app's own folder, when it exists and is not a folder that holds many apps: the recorded
    InstallLocation, else where its icon or its own uninstaller (unins000.exe, Uninstall.exe) sits. The
    bundle exe of a Burn installer sits in a cache folder and says nothing about where the app lives."""
    program = _split_command(app.uninstall_string)[0]
    guesses = [os.path.dirname(app.icon_path) if app.icon_path else ""]
    if program and _OWN_UNINSTALLER.fullmatch(os.path.basename(program)):
        guesses.append(os.path.dirname(program))
    guesses = [g for g in guesses if not _in_cache(g)]
    for raw in (app.install_location, *guesses):
        path = folder_path(raw)
        if path and _usable_root(path) and os.path.isdir(path):
            return path
    return ""


def folder_size(folder: str, seconds: float = FOLDER_SECONDS, max_entries: int = FOLDER_ENTRIES,
                cancel=None) -> tuple[int, bool]:
    """(bytes, complete): the sum of the file sizes under `folder`. Links and junctions are not followed. The
    walk stops after `seconds` or `max_entries` entries (or when `cancel` is set); the bytes counted so far are then a lower bound."""
    total = seen = 0
    deadline = time.monotonic() + seconds
    pending = [folder]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries or time.monotonic() > deadline or (cancel is not None and cancel.is_set):
                        return total, False
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if info.st_file_attributes & _ATTR_REPARSE:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    else:
                        total += info.st_size
        except OSError:
            continue
    return total, True


def web_links(app: InstalledApp, repo: str | None = None, page_url: str | None = None) -> list[str]:
    """http(s) addresses worth showing, without duplicates: the GitHub repository, the release page, then
    what the Uninstall key lists (which also holds e-mail addresses, file paths and plain names)."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in (f"https://github.com/{repo}" if repo else "", page_url or "", *app.urls):
        url = (raw or "").strip()
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not host or not url.isprintable() or " " in url:
            continue
        key = f"{host}{parts.path.rstrip('/')}?{parts.query}".lower()    # http and https of one page are one link
        if key not in seen:
            seen.add(key)
            found.append(url)
    return found[:MAX_LINKS]


def signer_of(app: InstalledApp) -> tuple[str, str]:
    """(Authenticode status, signer CN) of the app's own program: its icon file, an exe in its folder named like it, its uninstaller.
    The first file with a valid signature decides. Statuses are PowerShell's (Valid, NotSigned, HashMismatch,
    NotTrusted, ...), plus Unknown when PowerShell could not answer and NoFile when there is nothing to check
    (Store apps, entries with no program). Slow: call from a thread."""
    key = (app.reg_key, app.version)
    if key in _signed:
        return _signed[key]
    folder = install_folder(app)
    candidates = (app.icon_path, trust._named_exe(folder, app.name) if folder else "",
                  _split_command(app.uninstall_string)[0])
    files = list(dict.fromkeys(p for p in candidates if trust._signable(p)))
    if not files:
        return "NoFile", ""
    first = None
    for path in files:
        try:
            status, signer = installer.signature(Path(path))
        except Exception:    # a best-effort probe of an external tool
            log.warning("%s: could not read the signature of %s", app.name, path, exc_info=True)
            return "Unknown", ""
        if status == "Valid" and signer:
            _signed[key] = (status, signer)
            return _signed[key]
        if status == "Unknown":
            return "Unknown", ""
        first = first or (status, signer)
    _signed[key] = first
    return first


# --- Repair and Modify ---------------------------------------------------------------

def _msi_product(app: InstalledApp) -> str:
    """The product code of a Windows Installer app: its {GUID} key name, or the GUID in its msiexec command; '' for any other app."""
    if app.product_code:
        return app.product_code.upper()
    for raw in (app.uninstall_string, app.modify_path):
        program, args = _split_command(raw) if raw.strip() else ("", "")
        found = GUID.search(args) if _is_exe(program, "msiexec", "msiexec.exe") else None
        if found:
            return found.group(0).upper()
    return ""


def _msi_known(code: str) -> bool:
    """Windows Installer still has the product installed (or msi.dll can't be asked: then the command line decides)."""
    state = inventory._msi_state(code)
    return state is None or state in _MSI_STATES


def repair_blocker(app: InstalledApp) -> tuple[str, dict] | None:
    """Why Repair is not offered (English template, values), or None when it is."""
    values = {"name": app.name}
    code = _msi_product(app)
    if not code:
        return N_("Only apps installed with Windows Installer (MSI) can be repaired from here."), {}
    if app.no_repair:
        return N_("The installer of {name} says it can't be repaired."), values
    if not _msi_known(code):
        return N_("Windows Installer no longer knows {name}, so it can't be repaired."), values
    return None


def _safe_program(program: str) -> bool:
    """An existing program file with a drive-letter path outside the Windows folder (no network paths, no
    cmd.exe or rundll32.exe turning a registry text into a script)."""
    if not (re.match(r"[A-Za-z]:[\\/]", program) and program.lower().endswith(_LAUNCHABLE) and os.path.isfile(program)):
        return False
    windows = os.environ.get("SystemRoot")
    return not (windows and _under(program, windows))


@dataclass(frozen=True)
class _Command:
    program: str = ""
    params: str = ""
    blocked: tuple[str, dict] | None = None    # why there is no command: (English template, values)


def _modify_command(app: InstalledApp) -> _Command:
    values = {"name": app.name}
    none = _Command(blocked=(N_("{name} doesn't offer a Modify option."), values))
    if app.no_modify or app.reg_key.startswith(STORE_PREFIX):
        return none
    raw = app.modify_path.strip()
    program, args = _split_command(raw) if raw else ("", "")
    if program and not _is_exe(program, "msiexec", "msiexec.exe"):
        if raw.isprintable() and _safe_program(program):
            return _Command(program, args)
        return _Command(blocked=(N_("Upstream won't run the Modify command of {name}: its program is missing or "
                                    "not safe to start."), values))
    # msiexec's own switches in ModifyPath are never reused: only its product code, with /i (maintenance).
    code = _msi_product(app)
    if code and _msi_known(code):
        return _Command(installer._system_tool("msiexec.exe"), f"/i {code}")
    return none


def modify_blocker(app: InstalledApp) -> tuple[str, dict] | None:
    """Why Modify is not offered (English template, values), or None when it is."""
    return _modify_command(app).blocked


def can_repair(app: InstalledApp) -> bool:
    return repair_blocker(app) is None


def can_modify(app: InstalledApp) -> bool:
    return modify_blocker(app) is None


def gather(app: InstalledApp, measure: bool = False) -> AppDetails:
    """The facts, fast: registry values already read by the scan plus a few file-system checks. With `measure`
    an app that reports no size gets its folder measured (capped, see folder_size); the dialog does that on a thread instead."""
    folder = install_folder(app)
    size, approx = app.size_kb * 1024, False
    if not size and measure and folder:
        size, complete = folder_size(folder)
        approx = not complete
    return AppDetails(
        name=app.name, version=app.version, publisher=app.publisher, scope=app.scope, arch=app.arch,
        install_location=folder, install_date=format_date(app.install_date), size_bytes=size,
        uninstall_string=app.uninstall_string or app.quiet_uninstall_string, urls=list(app.urls),
        can_repair=can_repair(app), can_modify=can_modify(app), installer_kind=app.install_kind, size_approx=approx)


def repair(app: InstalledApp, progress, cancel=None) -> tuple[bool, str, dict]:
    """Repair a Windows Installer app: msiexec /fa, with its progress window and, for a machine-wide app, Windows' own permission
    prompt. Waits for it to finish. progress(template, **values). Returns (ok, English template, values)."""
    blocker = repair_blocker(app)
    if blocker:
        return False, *blocker
    name = {"name": app.name}
    params = f"/fa {_msi_product(app)} /passive /norestart"
    log.info("%s: repair: msiexec %s", app.name, params)
    progress("Repairing {name}…", **name)
    try:
        code = installer._shell_run(installer._system_tool("msiexec.exe"), params, cancel=cancel,
                                    timeout_min=installer.timeout_minutes())
    except InstallError as e:    # includes Cancelled: permission declined, or the user stopped waiting
        log.info("%s: repair: %s", app.name, e)
        return False, e.template, e.values
    except Exception as e:
        log.exception("%s: repair crashed", app.name)
        return False, "{error}", {"error": str(e)}
    outcome = classify(code, "msi")
    log.info("%s: repair: msiexec exited with %s (%s)", app.name, code, outcome.kind)
    if outcome.restart:
        return True, N_("{name} was repaired. Restart Windows to finish."), name
    if outcome.ok:
        return True, N_("{name} was repaired."), name
    if outcome.cancelled:
        return False, NEUTRAL[0], {}
    return False, outcome.template, outcome.values


def _open(file: str, params: str, folder: str = "") -> int:
    """Start a program through the shell and return at once (so Windows asks for permission when the
    program needs it). 0 when it started, else the Windows error code."""
    info = installer._ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_NOASYNC | _SEE_NO_UI
    info.lpVerb = "open"
    info.lpFile = file
    info.lpParameters = params
    info.lpDirectory = folder or None
    info.nShow = 1
    if installer._shell32.ShellExecuteExW(ctypes.byref(info)):
        return 0
    return ctypes.get_last_error() or 1


def modify(app: InstalledApp) -> tuple[bool, str, dict]:
    """Open the Modify / Change window of the app and return without waiting. Returns (started, English template, values)."""
    command = _modify_command(app)
    if command.blocked:
        return False, *command.blocked
    name = {"name": app.name}
    log.info("%s: modify: %s %s", app.name, command.program, command.params)
    error = _open(command.program, command.params, os.path.dirname(command.program))
    if error == 1223:
        return False, N_("Administrator permission (UAC) was declined."), {}
    if error:
        log.warning("%s: modify: Windows error %d", app.name, error)
        return False, N_("Couldn't open the Modify window (Windows error {code})."), {"code": error}
    return True, N_("Opened the Modify window of {name}. Follow the steps there."), name
