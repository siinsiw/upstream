from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import net
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


def main() -> int:
    setup_logging()
    migrate_legacy()
    settings = Settings()
    _clean_settings(settings)
    net.configure(settings)

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
