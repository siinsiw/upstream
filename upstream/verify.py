"""Check, after an installer returns, what really happened on the PC.

snapshot() records the app family's Uninstall entries (same app name, any scope) and the pending-restart flags
before the installer runs; check() reads them again and decides:

    installed     the entry for this app, or its replacement under a new key, shows a newer version
    side_by_side  a second entry with the newer version appeared and the old one is still listed
    restart       the installer asked for one (3010 / 1641), or the version changed and a pending-restart flag is new
                  (a flag alone proves nothing: it may belong to Windows Update or another program)
    unchanged     the installer returned success but Windows still lists the old version (ok=False)
    unknown       nothing to read (a Store or winget-only row, or no entry left): the expected version is trusted"""
from __future__ import annotations

import logging
import time
import winreg
from dataclasses import dataclass, field

from .context import CancelToken
from .exitcodes import Outcome
from .inventory import InstalledApp, read_installed_apps
from .versioning import compare, parse_version, same_version

log = logging.getLogger("upstream.verify")

STORE_PREFIX = "WINGET\\"
SETTLE_SECONDS = 6      # an installer that hands over to a helper may update the registry just after it returns
_ACCESS = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
_REBOOT_KEYS = {        # flag name -> key that exists while Windows waits for a restart
    "servicing": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
    "windows update": r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
}
_SESSION_MANAGER = r"SYSTEM\CurrentControlSet\Control\Session Manager"


@dataclass
class Snapshot:
    """The PC's state just before the installer runs."""
    entries: list[InstalledApp] = field(default_factory=list)
    pending_reboot: bool = False                       # Windows was already waiting for a restart
    pending: dict[str, int] = field(default_factory=dict)   # restart flag -> how much it holds


@dataclass(frozen=True)
class Verdict:
    kind: str                      # installed | restart | side_by_side | unchanged | unknown
    version: str                   # the version Windows reports now (or the expected one when unknown)
    template: str                  # English message shown on the row
    values: dict = field(default_factory=dict)
    ok: bool = True


def _pending_flags() -> dict[str, int]:
    """Restart-pending markers that are set right now: the servicing and Windows Update keys, and the number of
    files queued to be replaced or deleted at the next start."""
    found: dict[str, int] = {}
    for name, path in _REBOOT_KEYS.items():
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, _ACCESS))
            found[name] = 1
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SESSION_MANAGER, 0, _ACCESS) as key:
            queued = winreg.QueryValueEx(key, "PendingFileRenameOperations")[0]
        count = len([item for item in queued if item]) if isinstance(queued, list) else 0
        if count:
            found["file operations"] = count
    except OSError:
        pass
    return found


def _family(app: InstalledApp) -> list[InstalledApp]:
    return [entry for entry in read_installed_apps() if entry.ident == app.ident]


def snapshot(app: InstalledApp) -> Snapshot:
    """Never raises: a snapshot that could not be taken just leaves check() with less to compare."""
    pending = _pending_flags()
    entries: list[InstalledApp] = []
    if not app.reg_key.startswith(STORE_PREFIX):
        try:
            entries = _family(app)
        except Exception:
            log.exception("%s: could not read the installed apps", app.name)
        if all(entry.reg_key != app.reg_key for entry in entries):
            entries.append(app)
    return Snapshot(entries, bool(pending), pending)


def _moved_on(new: str, old: str) -> bool:
    """Whether the registry now shows a later version than before. Strings that cannot be put in order
    (another numbering scheme, no number at all) only count as later when they differ and carry a number."""
    order = compare(new, old)
    if order is not None:
        return order > 0
    if not new:
        return False
    if parse_version(new) is None:
        return parse_version(old) is None and new != old
    return True


def _newest(entries: list[InstalledApp]) -> InstalledApp | None:
    best = None
    for entry in entries:
        if best is None or compare(entry.version, best.version) == 1:
            best = entry
    return best


def _inspect(app: InstalledApp, before: Snapshot) -> tuple[str, InstalledApp | None, InstalledApp | None]:
    """How the app list changed: (kind, the entry carrying the version to report, the old entry if still listed)."""
    family = _family(app)
    known = {entry.reg_key for entry in before.entries} | {app.reg_key}
    old = next((entry for entry in family if entry.reg_key == app.reg_key), None)
    fresh = _newest([entry for entry in family if entry.reg_key not in known and _moved_on(entry.version, app.version)])
    if old and _moved_on(old.version, app.version):
        return "installed", old, old
    if fresh and old is None:       # an upgrade that registers the new version under a new key
        return "installed", fresh, None
    if fresh:
        return "side_by_side", fresh, old
    if old:
        return "unchanged", old, old
    return "unknown", None, None


def _inspect_for(app: InstalledApp, before: Snapshot, downgrade_to: str):
    """`_inspect`, except that for a rollback the registry showing exactly the version asked for counts as installed
    (the version went down, so nothing is newer than before)."""
    kind, entry, old = _inspect(app, before)
    if (downgrade_to and kind == "unchanged" and entry is not None and same_version(entry.version, downgrade_to)
            and not same_version(app.version, downgrade_to)):
        return "installed", entry, old
    return kind, entry, old


def _restart_flag_is_new(before: Snapshot) -> bool:
    return any(count > before.pending.get(name, 0) for name, count in _pending_flags().items())


def check(app: InstalledApp, before: Snapshot, outcome: Outcome, expected_version: str,
          cancel: CancelToken | None = None, downgrade_to: str = "") -> Verdict:
    """Cancelling ends the settle wait at once; the verdict then rests on what the registry shows now.
    `downgrade_to` is the version a rollback was asked to install."""
    expected = expected_version or app.version
    kind, entry = "unknown", None
    if not app.reg_key.startswith(STORE_PREFIX):
        try:
            kind, entry, _ = _inspect_for(app, before, downgrade_to)
            # Not for an installer that only said the app is already there: it started no hand-over to wait for.
            settle = not outcome.restart and not outcome.present
            deadline = time.monotonic() + SETTLE_SECONDS
            while settle and kind == "unchanged" and time.monotonic() < deadline:
                if cancel is None:
                    time.sleep(1)
                elif cancel.wait(1):
                    break
                kind, entry, _ = _inspect_for(app, before, downgrade_to)
        except Exception:
            log.exception("%s: could not read the installed apps", app.name)
    version = entry.version if entry and kind in ("installed", "side_by_side") else expected
    log.info("%s: registry %s (%s -> %s)", app.name, kind, app.version or "?", entry.version if entry else "?")

    if kind == "side_by_side":
        return Verdict(kind, version, "Installed {version}, but the old copy of {name} is still there",
                       {"version": version, "name": app.name})
    # 3010 / 1641 are enough on their own. A new flag is only believed when the version really changed, and not
    # when the installer merely reported the app as already there: otherwise a failed install would pass as "restart".
    if outcome.restart or (kind != "unchanged" and not outcome.present and _restart_flag_is_new(before)):
        return Verdict("restart", version, "Restart Windows to finish installing {version}", {"version": version})
    if kind == "unchanged":
        return Verdict(kind, entry.version, "Installer finished, but Windows still reports {version}",
                       {"version": entry.version}, ok=False)
    if kind == "unknown":
        return Verdict(kind, expected, "Installed {version} (couldn't be verified)", {"version": expected})
    return Verdict("installed", version, "Installed {version}", {"version": version})
