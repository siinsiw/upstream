from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import __version__, installer, net, stability
from .config import Cache, Settings, migrate_legacy, setup_logging
from .i18n import LANGUAGES
from .ui.crash import CrashReportDialog
from .ui.locale import apply_language
from .ui.theme import THEMES, app_icon, apply_theme
from .ui.window import MainWindow

log = logging.getLogger("upstream.app")

CRASH_REPORT_DELAY_MS = 400
HOUSEKEEPING_DELAY_MS = 5000


@dataclass(frozen=True)
class Launch:
    """The command-line arguments Upstream itself understands."""
    minimized: bool = False     # --minimized: no window at start (the Windows start-up entry)
    elevated: bool = False      # --elevated: relaunched with administrator rights (elevation.relaunch_elevated)
    wait_pid: int = 0           # --wait-pid <pid>: let that process exit before taking over


def parse_launch(argv: list[str]) -> Launch:
    """Anything else (Qt's own options) is left to Qt. Never raises: a bad value is ignored."""
    minimized = elevated = False
    wait_pid = 0
    args = iter(argv)
    for arg in args:
        if arg == "--minimized":
            minimized = True
        elif arg == "--elevated":
            elevated = True
        elif arg == "--wait-pid" or arg.startswith("--wait-pid="):
            value = arg.partition("=")[2] if "=" in arg else next(args, "")
            wait_pid = int(value) if value.isascii() and value.isdigit() else 0
    return Launch(minimized, elevated, wait_pid)


def _clean_settings(settings: Settings):
    """A hand-edited or older settings file must not break startup or leave
    the settings dialog with nothing selected."""
    theme = settings.get("theme")
    if not (isinstance(theme, str) and (theme == "system" or theme in THEMES)):
        settings.set("theme", "system")
    if settings.get("language") not in LANGUAGES:
        settings.set("language", "en")
    for key, allowed in (("publisher_policy", ("ask", "block", "off")),
                         ("close_apps_policy", ("ask", "auto", "never")),
                         ("metered_policy", ("ask", "skip", "ignore")),
                         ("battery_policy", ("ask", "skip", "ignore"))):
        if settings.get(key) not in allowed:
            settings.set(key, "ask")
    proxy = settings.get("proxy")
    if not (isinstance(proxy, dict) and proxy.get("mode") in ("system", "none", "custom")
            and isinstance(proxy.get("url", ""), str)):
        settings.set("proxy", {"mode": "system", "url": ""})
    timeout = settings.get("install_timeout_min")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        settings.set("install_timeout_min", 30)
    for key in ("signers", "signers_approved"):
        if not isinstance(settings.get(key), dict):
            settings.set(key, {})


def _show_window(window: MainWindow, settings: Settings, launch: Launch) -> None:
    if not launch.minimized:
        window.show()
    elif not settings.get("tray_enabled"):
        window.showMinimized()      # with no tray icon nothing else would show that Upstream is running


def _show_crash_report(window: MainWindow) -> None:
    """The report of an earlier crash, once the window is up. A hidden window (a start-up launch that lives
    in the tray) leaves it for a start the user sees."""
    try:
        path = stability.pending_crash_report()
        if path is None or not window.isVisible():
            return
        CrashReportDialog(path, window).exec()
        stability.mark_reported(path)
    except Exception:
        log.exception("Could not show the crash report")


def _start_housekeeping() -> None:
    threading.Thread(target=stability.housekeeping, name="housekeeping", daemon=True).start()


def main() -> int:
    launch = parse_launch(sys.argv[1:])
    setup_logging()
    log.info("Upstream %s starting (pid %d%s%s)", __version__, os.getpid(),
             ", minimized" if launch.minimized else "", ", elevated" if launch.elevated else "")

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    try:
        # Own taskbar group and icon instead of python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Upstream.Updater")
    except (AttributeError, OSError):
        pass

    app = QApplication(sys.argv)
    window: MainWindow | None = None

    def activate():
        if window is not None:
            window.show_and_raise()

    # An elevated relaunch waits for the instance that started it instead of handing over to it.
    if launch.wait_pid and not stability.wait_for_exit(launch.wait_pid):
        log.warning("Process %d is still running; starting without the single-instance lock", launch.wait_pid)
    elif not stability.acquire_single_instance(activate, activate=not launch.minimized):
        return 0
    app.aboutToQuit.connect(stability.release_single_instance)

    migrate_legacy()
    settings = Settings()
    _clean_settings(settings)
    net.configure(settings)
    installer.configure(settings)

    app.setApplicationName("Upstream")
    app.setApplicationDisplayName("Upstream")
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    apply_language(app, settings.get("language"))
    apply_theme(app, settings.get("theme"))
    try:
        app.styleHints().colorSchemeChanged.connect(
            lambda *_: settings.get("theme") == "system" and apply_theme(app, "system"))
    except AttributeError:
        pass

    window = MainWindow(settings, Cache())
    # Background (tray, schedule, automatic updates) is created here once it is wired in. If its tray icon
    # cannot be made, a --minimized start must show the window.
    _show_window(window, settings, launch)
    QTimer.singleShot(150, window.start_scan)
    QTimer.singleShot(CRASH_REPORT_DELAY_MS, lambda: _show_crash_report(window))
    QTimer.singleShot(HOUSEKEEPING_DELAY_MS, _start_housekeeping)
    return app.exec()
