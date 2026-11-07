"""Make an update replace the installed copy instead of sitting next to it.

Reads only: the Uninstall registry entry, the uninstaller file, and the new MSI package through msi.dll.
plan() never raises. With missing or unclear evidence it returns an empty plan, so the install runs as it
did before; it refuses only when the evidence says a second copy would appear."""
from __future__ import annotations

import ctypes
import logging
import os
import re
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from . import guard, installer
from .i18n import N_
from .inventory import GUID, InstalledApp, folder_path
from .uninstaller import STORE_PREFIX, _split_command

log = logging.getLogger("upstream.inplace")

_LABELS = {
    "msi": "MSI", "msix": "MSIX", "inno": "Inno Setup", "nullsoft": "NSIS", "burn": "WiX Burn",
    "installshield": "InstallShield", "advancedinstaller": "Advanced Installer", "squirrel": "Squirrel",
}
# Setup programs that usually carry an MSI inside: next to an installed MSI they may well upgrade it.
_MSI_WRAPPERS = {"installshield", "advancedinstaller", "burn"}
_UNINSTALLER_NAME = re.compile(r"unin|remove|setup|^update\.exe$|^un[_-]", re.I)
_MAX_UNINSTALLER = 16 * 1024 * 1024
_sniffed: dict[tuple[str, int, int], str] = {}

_ERROR_MORE_DATA = 234
_ERROR_NO_MORE_ITEMS = 259
_ONLY_DETECT = 2       # Upgrade.Attributes msidbUpgradeAttributesOnlyDetect: finds the product, doesn't replace it
_MINOR_UPGRADE = "REINSTALL=ALL REINSTALLMODE=vomus"   # Windows Installer's command for a package with the same ProductCode
_msi = None


@dataclass
class InstallPlan:
    extra_args: str = ""           # appended to the silent switches (e.g. /DIR="C:\Apps\Audacity" for Inno Setup)
    warnings: list = field(default_factory=list)    # [(English template, values)] logged with the install
    refuse: tuple | None = None    # (English template, values): installing would create a second copy


# --- what is installed -------------------------------------------------------------

def _uninstaller(app: InstalledApp) -> str:
    """The uninstaller program the Uninstall key points at; '' when it isn't a file that looks like one
    (msiexec, rundll32 and the app's own exe with a --uninstall switch are not)."""
    for raw in (app.uninstall_string, app.quiet_uninstall_string):
        program, _ = _split_command(raw)
        if program and _UNINSTALLER_NAME.search(os.path.basename(program)) and os.path.isfile(program):
            return program
    return ""


def _sniff(program: str) -> str:
    """Framework of an uninstaller exe from the markers installer.detect() knows, cached until the file changes."""
    try:
        stat = os.stat(program)
        if stat.st_size > _MAX_UNINSTALLER:
            return ""
        key = (os.path.normcase(program), stat.st_mtime_ns, stat.st_size)
        if key not in _sniffed:
            kind = installer.detect(Path(program))
            _sniffed[key] = "" if kind == "exe" else kind
        return _sniffed[key]
    except (OSError, installer.InstallError):
        return ""


def installed_kind(app: InstalledApp) -> str:
    """Installer framework of the copy already on the PC (msi, inno, nullsoft, burn, ...), '' if unknown.
    The scan fills install_kind from the registry; the uninstaller file is read here, once per file (NSIS
    leaves nothing in the registry, and reading every uninstaller at scan time would be slow). When the two
    disagree the answer is '': a {GUID}_is1 key beside an NSIS Uninstall.exe (Eitaa Desktop) is a leftover
    of one framework, and guessing would refuse an update that is fine."""
    registry = "" if app.install_kind == "exe" else app.install_kind
    if not registry and app.reg_key.startswith(STORE_PREFIX):
        return "msix"
    program = _uninstaller(app)
    sniffed = _sniff(program) if program else ""
    if registry and sniffed and registry != sniffed:
        return ""
    return registry or sniffed


def _install_folder(app: InstalledApp) -> str:
    """The folder the app lives in, only when it is certain: an existing folder that belongs to one app
    (not Program Files itself, a drive root...) and holds the app's uninstaller or icon."""
    program = _uninstaller(app)
    folder = folder_path(app.install_location or (os.path.dirname(program) if program else "")).rstrip("\\/")
    if not folder or not os.path.isdir(folder) or guard.protected_path(folder):
        return ""
    if app.install_location:
        root = guard.normalize(folder)
        if not any(guard.inside(guard.normalize(p), root) for p in (program, app.icon_path) if p):
            return ""
    return folder


# --- the new package -----------------------------------------------------------------

def _lib():
    global _msi
    if _msi is None:
        lib = ctypes.WinDLL("msi")
        handle = ctypes.POINTER(wintypes.DWORD)
        lib.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, handle]
        lib.MsiDatabaseOpenViewW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, handle]
        lib.MsiViewExecute.argtypes = [wintypes.DWORD, wintypes.DWORD]
        lib.MsiViewFetch.argtypes = [wintypes.DWORD, handle]
        lib.MsiRecordGetStringW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
        lib.MsiCloseHandle.argtypes = [wintypes.DWORD]
        lib.MsiEnumRelatedProductsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR]
        _msi = lib
    return _msi


def _field(record, index: int) -> str:
    lib = _lib()
    size = wintypes.DWORD(256)
    buf = ctypes.create_unicode_buffer(size.value)
    if lib.MsiRecordGetStringW(record, index, buf, ctypes.byref(size)) == _ERROR_MORE_DATA:
        buf = ctypes.create_unicode_buffer(size.value + 1)
        size = wintypes.DWORD(len(buf))
        lib.MsiRecordGetStringW(record, index, buf, ctypes.byref(size))
    return buf.value


def _rows(db, query: str, columns: int) -> list[tuple[str, ...]]:
    """All rows of a read-only query; [] when the table doesn't exist."""
    lib = _lib()
    view = wintypes.DWORD()
    if lib.MsiDatabaseOpenViewW(db, query, ctypes.byref(view)):
        return []
    rows = []
    try:
        lib.MsiViewExecute(view, 0)
        while True:
            record = wintypes.DWORD()
            if lib.MsiViewFetch(view, ctypes.byref(record)):
                break
            try:
                rows.append(tuple(_field(record, i) for i in range(1, columns + 1)))
            finally:
                lib.MsiCloseHandle(record)
    finally:
        lib.MsiCloseHandle(view)
    return rows


def _read_package(path: Path) -> dict | None:
    """ProductCode, UpgradeCode, ALLUSERS and the upgrade codes the package replaces, read from the .msi
    without installing anything. None when it can't be opened or has no ProductCode."""
    try:
        lib = _lib()
        db = wintypes.DWORD()
        if lib.MsiOpenDatabaseW(str(path), None, ctypes.byref(db)):
            return None
        try:
            props = dict(_rows(db, "SELECT `Property`, `Value` FROM `Property`", 2))
            replaces = [code for code, attrs in _rows(db, "SELECT `UpgradeCode`, `Attributes` FROM `Upgrade`", 2)
                        if code and not int(attrs or 0) & _ONLY_DETECT]
        finally:
            lib.MsiCloseHandle(db)
    except (OSError, AttributeError, ValueError):
        return None
    if not props.get("ProductCode"):
        return None
    return {"product": props["ProductCode"].upper(), "upgrade": props.get("UpgradeCode", ""),
            "allusers": props.get("ALLUSERS", ""), "replaces": replaces}


def _related_products(upgrade_code: str) -> set[str] | None:
    """Product codes Windows Installer has installed under this upgrade code; None when it can't be asked."""
    try:
        lib = _lib()
    except OSError:
        return None
    found, index = set(), 0
    buf = ctypes.create_unicode_buffer(39)
    while True:
        code = lib.MsiEnumRelatedProductsW(upgrade_code, 0, index, buf)
        if code == _ERROR_NO_MORE_ITEMS:
            return found
        if code:
            return None
        found.add(buf.value.upper())
        index += 1


def _product_code(app: InstalledApp) -> str:
    if app.product_code:
        return app.product_code.upper()
    for text in (app.reg_key.rsplit("\\", 1)[-1], app.uninstall_string if "msiexec" in app.uninstall_string.lower() else ""):
        found = GUID.search(text)
        if found:
            return found.group(0).upper()
    return ""


# --- planning --------------------------------------------------------------------------

def _plan_msi(app: InstalledApp, path: Path) -> InstallPlan:
    code, package = _product_code(app), _read_package(path)
    if not code or not package:
        return InstallPlan()
    plan = InstallPlan()
    values = {"name": app.name}
    if app.scope == "machine" and not package["allusers"]:
        plan.warnings.append((N_("The installed copy of {name} is for all users, but this installer sets the app up for the "
                                 "current user only. The update may end up as a second copy."), values))
    elif app.scope == "user" and package["allusers"] == "1":
        plan.warnings.append((N_("The installed copy of {name} is for the current user only, but this installer "
                                 "sets up the app for all users. The update may end up as a second copy."), values))
    if package["product"] == code:
        plan.extra_args = _MINOR_UPGRADE
        return plan
    undecided = False
    for upgrade_code in {c for c in (package["upgrade"], *package["replaces"]) if c}:
        related = _related_products(upgrade_code)
        if related is None:
            undecided = True
        elif code in related:
            return plan
    if not undecided:
        plan.refuse = (N_("This installer is not an upgrade of the {name} on this PC. Running it would add a "
                          "second copy next to the old one."), values)
    return plan


def _plan_folder(app: InstalledApp, kind: str) -> InstallPlan:
    folder = _install_folder(app)
    if not folder:
        return InstallPlan()
    log.info("%s: keeping the update in %s", app.name, folder)
    # Inno takes a quoted value. NSIS wants /D= last and unquoted, spaces or not; install() appends extra_args last.
    return InstallPlan(f'/DIR="{folder}"' if kind == "inno" else f"/D={folder}")


def _plan_mismatch(app: InstalledApp, old: str, new: str) -> InstallPlan:
    values = {"name": app.name, "installed": _LABELS.get(old, old), "new": _LABELS.get(new, new)}
    if old == "msi" and new in _MSI_WRAPPERS:
        return InstallPlan(warnings=[(N_("{name} was installed as an MSI package, but this update is packaged with "
                                         "{new}. It may not replace the installed copy."), values)])
    return InstallPlan(refuse=(N_("{name} was installed with {installed}, but this update is packaged with {new}. "
                                  "Running it would add a second copy next to the old one."), values))


def plan(app: InstalledApp, new_kind: str, installer_path: Path) -> InstallPlan:
    """How to run the downloaded installer so it replaces the installed copy. `new_kind` is what
    installer.detect() said about the file."""
    try:
        old = installed_kind(app)
        if not old or old == "exe" or not new_kind or new_kind == "exe":
            return InstallPlan()    # one side is unknown: nothing to compare, run it as before
        if old != new_kind:
            return _plan_mismatch(app, old, new_kind)
        if new_kind == "msi":
            return _plan_msi(app, Path(installer_path))
        if new_kind in ("inno", "nullsoft"):
            return _plan_folder(app, new_kind)
    except Exception:
        log.exception("%s: could not check whether the update replaces the installed copy", app.name)
    return InstallPlan()
