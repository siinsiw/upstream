"""Tray icon with its menu. Owner: the background feature."""
from __future__ import annotations

from PySide6.QtWidgets import QSystemTrayIcon


class TrayIcon(QSystemTrayIcon):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
