from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import installer, net
from .config import Cache, Settings, migrate_legacy, setup_logging
from .i18n import LANGUAGES
from .ui.locale import apply_language
from .ui.theme import THEMES, app_icon, apply_theme
from .ui.window import MainWindow


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


def main() -> int:
    setup_logging()
    migrate_legacy()
    settings = Settings()
    _clean_settings(settings)
    net.configure(settings)
    installer.configure(settings)

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    try:
        # Own taskbar group and icon instead of python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Upstream.Updater")
    except (AttributeError, OSError):
        pass

    app = QApplication(sys.argv)
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
    window.show()
    QTimer.singleShot(150, window.start_scan)
    return app.exec()
