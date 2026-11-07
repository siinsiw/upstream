"""Details dialog for one app. Owner: the app-details feature."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog


class DetailsDialog(QDialog):
    def __init__(self, status, parent=None):
        super().__init__(parent)
        self.status = status
