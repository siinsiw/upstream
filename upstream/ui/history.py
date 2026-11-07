"""History tab. Owner: the history feature."""
from __future__ import annotations

from PySide6.QtWidgets import QWidget


class HistoryPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

    def refresh(self) -> None:
        """Reload from history.load()."""

    def retranslate(self) -> None:
        """Re-apply every visible text after a language switch."""
