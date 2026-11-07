"""Tray icon, scheduled checks, notifications, per-app automatic updates. Owner: the background feature.

The main window offers this API to Background (the integrator implements it on MainWindow):
    window.statuses: dict[str, AppStatus]       window.settings
    window.start_scan()                          window.is_busy() -> bool   (scanning, updating or uninstalling)
    window.show_and_raise()                      window.pending_updates() -> list[str]   (uids with an update)
    window.request_update(uid, bulk=False)       window.quit_app()
Background calls these only on the UI thread."""
from __future__ import annotations

from PySide6.QtCore import QObject


class Background(QObject):
    def __init__(self, window, settings):
        super().__init__(window)
        self.window, self.settings = window, settings

    def start(self) -> None:
        """Create the tray icon (when enabled) and start the schedule timer."""

    def stop(self) -> None:
        """Remove the tray icon and stop the timer."""

    def settings_changed(self) -> None:
        """Re-read tray_enabled, check_interval_hours, ... after the settings dialog changed them."""

    def scan_finished(self) -> None:
        """Called by the window after every scan: notify, then install the auto_update apps."""

    def should_hide_on_close(self) -> bool:
        """True when closing the window should only hide it (close_to_tray and a tray icon exists)."""
        return False


def set_autostart(enabled: bool) -> bool:
    """Add or remove the HKCU Run entry that starts Upstream minimised with Windows. True on success."""
    return True


def is_autostart() -> bool:
    return False
