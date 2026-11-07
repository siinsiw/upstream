"""Tray icon, scheduled checks, notifications, per-app automatic updates. Owner: the background feature.

The main window offers this API to Background (the integrator implements it on MainWindow):
    window.statuses: dict[str, AppStatus]       window.settings
    window.start_scan()                          window.is_busy() -> bool   (scanning, updating or uninstalling)
    window.show_and_raise()                      window.pending_updates() -> list[str]   (uids with an update)
    window.request_update(uid, bulk=False, auto=False)   window.quit_app()   window.update_all()
Background calls these only on the UI thread.

Settings read: tray_enabled, close_to_tray, check_interval_hours, notify_updates, auto_update.
Settings written: last_check_ts (time of the last finished check), notified (what was announced) and auto_tried
(what was installed automatically), the last two as short lists of "ident@version".

A tray icon is also shown when only the schedule and notifications are on: without one there is no place for a
message to come from. The schedule compares the wall clock with last_check_ts once a minute, so a PC that slept
or a restart does not lose a check; it waits a moment after waking up, because the network is often not back yet."""
from __future__ import annotations

import logging
import math
import sys
import time
import winreg
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import running
from .i18n import isolate, num, tr, trn
from .ui.tray import TrayIcon

log = logging.getLogger("upstream.background")

TICK_MS = 60_000
STARTUP_GRACE = 120.0           # no scheduled check this soon after start: the start-up check has just run
RETRY_AFTER = 15 * 60.0         # a scheduled check that never finished (no network, ...) is retried after this long
RESUME_GAP = 180.0              # the one-minute tick arriving later than this means the PC slept
RESUME_DELAY = 90.0             # ... and the check waits this long after waking up
MAX_INTERVAL_HOURS = 24 * 366
MAX_REMEMBERED = 400
LISTED_NAMES = 3                # app names in a notification before "and N more"

AUTOSTART_NAME = "Upstream"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Task Manager's Startup tab keeps its on/off switch here: first byte odd = turned off by the user.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _tray_available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


class Background(QObject):
    def __init__(self, window, settings, clock: Callable[[], float] = time.time):
        super().__init__(window)
        self.window, self.settings, self.clock = window, settings, clock
        self.tray: TrayIcon | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)
        self._started = self._last_tick = clock()
        self._quiet_until = 0.0
        self._last_attempt = -math.inf

    # -- life cycle -----------------------------------------------------
    def start(self, minimized: bool = False) -> None:
        """Create the tray icon (when enabled) and start the schedule timer. `minimized` is True when Upstream was
        started with --minimized: without a tray icon the window then opens minimised in the taskbar, so the
        app is never running with no way to reach it."""
        self._started = self._last_tick = self.clock()
        self._sync()
        if minimized and self.tray is None and not self.window.isVisible():
            self.window.showMinimized()

    def stop(self) -> None:
        """Remove the tray icon and stop the timer."""
        self.timer.stop()
        self._remove_tray(reveal=False)

    def settings_changed(self) -> None:
        """Re-read tray_enabled, check_interval_hours, ... after the settings dialog changed them."""
        self._sync()

    @property
    def has_tray(self) -> bool:
        return self.tray is not None

    def should_hide_on_close(self) -> bool:
        """True when closing the window should only hide it (close_to_tray and a tray icon exists)."""
        return bool(self.settings.get("close_to_tray", False)) and self.tray is not None and self.tray.isVisible()

    def notify(self, title: str, body: str) -> bool:
        """A tray message (clicking it opens the window). False when there is no tray icon to show it."""
        if self.tray is None:
            log.info("No tray icon, notification not shown: %s", title)
            return False
        self.tray.notify(title, body)
        return True

    # -- tray and timer -------------------------------------------------
    def _tray_wanted(self) -> bool:
        return bool(self.settings.get("tray_enabled", False)) or (
            self._interval() > 0 and bool(self.settings.get("notify_updates", True)))

    def _sync(self):
        if self._tray_wanted() and _tray_available():
            if self.tray is None:
                self._add_tray()
            else:
                self.tray.refresh()
        elif self.tray is not None:
            self._remove_tray(reveal=True)
        if self.tray is None and self._interval() <= 0:
            self.timer.stop()
        elif not self.timer.isActive():
            self.timer.start()

    def _add_tray(self):
        self.tray = TrayIcon(self.window)
        self.tray.show()
        # With the window hidden in the tray, closing a dialog must not count as "the last window closed".
        # A real close of the window quits through _after_close instead.
        QApplication.setQuitOnLastWindowClosed(False)
        self.window.installEventFilter(self)
        log.info("Tray icon shown")

    def _remove_tray(self, reveal: bool):
        if self.tray is None:
            return
        self.window.removeEventFilter(self)
        QApplication.setQuitOnLastWindowClosed(True)
        self.tray.dispose()
        self.tray = None
        if reveal and not self.window.isVisible():
            self.window.show_and_raise()    # a hidden window with no tray icon could not be reached again
        log.info("Tray icon removed")

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close:
            QTimer.singleShot(0, self._after_close)
        return False

    def _after_close(self):
        if self.tray is not None and not self.window.isVisible() and not self.should_hide_on_close():
            self.window.quit_app()

    # -- schedule -------------------------------------------------------
    def _interval(self) -> float:
        """Seconds between scheduled checks; 0 when off or when the stored value is nonsense."""
        hours = self.settings.get("check_interval_hours", 0)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= MAX_INTERVAL_HOURS:
            return 0.0
        return hours * 3600.0

    def _last_check(self, now: float) -> float:
        value = self.settings.get("last_check_ts", 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value > now:
            return -math.inf    # unreadable, or stamped in the future (the clock was set back): treat as never
        return float(value)

    def _skip_reason(self, now: float) -> str | None:
        interval = self._interval()
        if not interval:
            return "scheduled checks are off"
        if now - self._started < STARTUP_GRACE:
            return "Upstream has just started"
        if now < self._quiet_until:
            return "the PC has just woken up"
        if now - self._last_check(now) < interval:
            return "not due yet"
        if now - self._last_attempt < RETRY_AFTER:
            return "the last attempt has not finished"
        if self.window.is_busy():
            return "Upstream is busy"
        return None

    def _tick(self):
        now = self.clock()
        if now - self._last_tick > RESUME_GAP:
            self._quiet_until = now + RESUME_DELAY
        self._last_tick = now
        try:
            if self.tray is not None:
                self.tray.refresh()
            reason = self._skip_reason(now)
            if reason is None:
                self._last_attempt = now
                log.info("Scheduled check starts")
                self.window.start_scan()
            else:
                log.debug("Scheduled check skipped: %s", reason)
        except Exception:
            log.exception("Scheduled check failed")

    # -- after a scan ---------------------------------------------------
    def scan_finished(self) -> None:
        """Called by the window after every scan: notify, then install the auto_update apps."""
        self._save("last_check_ts", self.clock())
        try:
            pending = [s for s in (self.window.statuses.get(uid) for uid in self.window.pending_updates())
                       if s and s.update]
            auto = self._auto_candidates(pending)
            queued = {s.uid for s in auto}
            self._announce([s for s in pending if s.uid not in queued])
            self._install(auto)
        except Exception:
            log.exception("Handling the finished check failed")
        if self.tray is not None:
            self.tray.refresh()

    def _announce(self, statuses):
        """One message for the updates nobody has been told about yet, unless the user is looking at the window."""
        if not self.settings.get("notify_updates", True):
            return
        told = self._remembered("notified")
        fresh = [s for s in statuses if _key(s) not in told]
        if not fresh:
            return
        self._remember("notified", [_key(s) for s in fresh])
        if self._window_active():
            log.info("%d new updates, not announced: the window is open", len(fresh))
            return
        fresh.sort(key=lambda s: s.app.name.casefold())
        self.notify(*_message(fresh))

    def _window_active(self) -> bool:
        return bool(self.window.isActiveWindow())

    def _auto_candidates(self, statuses) -> list:
        idents = self.settings.get("auto_update", [])
        if not isinstance(idents, list) or not idents:
            return []
        tried = self._remembered("auto_tried")
        picked = []
        for s in statuses:
            if s.app.ident not in idents:
                continue
            why = self._auto_skip(s, tried)
            if why:
                log.info("Automatic update of %s %s skipped: %s", s.app.name, s.update.version, why)
            else:
                picked.append(s)
        return picked

    def _auto_skip(self, s, tried: list[str]) -> str | None:
        """Why this update must not install by itself, or None. The cheap checks come first:
        looking for running processes walks the process list."""
        if s.pin:
            return "pinned"
        if _key(s) in tried:
            return "already tried for this version (a failed or declined install is not repeated unattended)"
        if s.update.source == "site" and not s.update.download_url and not s.fallbacks:
            return "needs a manual download"
        try:
            procs = running.find_running(s.app)
        except Exception as e:
            return f"could not tell whether it is running ({e})"
        if procs:
            return "running: " + ", ".join(sorted({p.name for p in procs}))
        return None

    def _install(self, statuses):
        if not statuses:
            return
        self._remember("auto_tried", [_key(s) for s in statuses])
        for s in statuses:
            log.info("Automatic update of %s to %s", s.app.name, s.update.version)
            try:
                self.window.request_update(s.uid, bulk=True, auto=True)
            except Exception:
                log.exception("Automatic update of %s could not be queued", s.app.name)

    # -- remembered keys ------------------------------------------------
    def _remembered(self, name: str) -> list[str]:
        value = self.settings.get(name, [])
        return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []

    def _remember(self, name: str, keys: list[str]):
        kept = [k for k in self._remembered(name) if k not in keys] + keys
        self._save(name, kept[-MAX_REMEMBERED:])

    def _save(self, name: str, value):
        self.settings.set(name, value)
        try:
            self.settings.save()
        except OSError as e:
            log.warning("Could not save %s: %s", name, e)


def _key(status) -> str:
    return f"{status.app.ident}@{status.update.version}"


def _message(statuses) -> tuple[str, str]:
    """Title and body for a list of apps with updates (sorted by name)."""
    n = len(statuses)
    title = trn("{n} update ready", "{n} updates ready", n)
    if n == 1:
        s = statuses[0]
        return title, tr("{name} {version} is ready to install", name=s.app.name, version=s.update.version)
    names = tr(", ").join(isolate(s.app.name) for s in statuses[:LISTED_NAMES])
    if n > LISTED_NAMES:
        names = tr("{names} and {n} more", names=names, n=num(n - LISTED_NAMES))
    return title, names


# -- start with Windows ---------------------------------------------------
def autostart_command() -> str:
    """The Run value: this program, minimised. From source that is pythonw (no console window) and main.py."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    gui = exe.with_name("pythonw.exe")
    return f'"{gui if gui.is_file() else exe}" "{Path(__file__).resolve().parent.parent / "main.py"}" --minimized'


def set_autostart(enabled: bool, name: str = AUTOSTART_NAME) -> bool:
    """Add or remove the HKCU Run entry that starts Upstream minimised with Windows. True on success."""
    try:
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, autostart_command())
            _clear_task_manager_switch(name)    # switching it on here overrides an earlier "Disable" in Task Manager
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        return True
    except OSError as e:
        log.warning("Could not %s the Windows start-up entry: %s", "add" if enabled else "remove", e)
        return False


def is_autostart(name: str = AUTOSTART_NAME) -> bool:
    """The Run entry exists and Task Manager has not switched it off."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return False
    return isinstance(value, str) and bool(value.strip()) and not _task_manager_off(name)


def _task_manager_off(name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_READ) as key:
            data, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return False
    return isinstance(data, bytes) and bool(data) and bool(data[0] & 1)


def _clear_task_manager_switch(name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass
