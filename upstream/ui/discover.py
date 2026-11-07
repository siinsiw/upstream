"""Discover tab: search and install new apps. Owner: the discover feature."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget


class DiscoverPage(QWidget):
    install_requested = Signal(object)       # discover.Package

    def __init__(self, parent=None):
        super().__init__(parent)

    def retranslate(self) -> None:
        """Re-apply every visible text after a language switch."""
