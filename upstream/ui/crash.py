"""Tells the user that Upstream crashed last time and shows the report that was saved. Offline: nothing is sent."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from ..i18n import tr
from .widgets import ElidedLabel

MAX_BYTES = 1_000_000   # a report is a few KB; a runaway file must not freeze the dialog
COPIED_FOR_MS = 2000


class CrashReportDialog(QDialog):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.report = self._read()
        self.setWindowTitle("Upstream")
        self.setMinimumWidth(640)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        title = QLabel(tr("Upstream closed unexpectedly last time"))
        title.setObjectName("dlgTitle")
        title.setWordWrap(True)
        intro = QLabel(tr("A report about it was saved on this PC. Nothing was sent anywhere. "
                          "To have the problem looked at, copy the report and send it to whoever maintains Upstream."))
        intro.setObjectName("hint")
        intro.setWordWrap(True)

        self.view = QPlainTextEdit()
        self.view.setObjectName("log")
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setLayoutDirection(Qt.LeftToRight)    # a traceback is code, whatever the UI language
        self.view.setMinimumHeight(240)
        self.view.setPlaceholderText(tr("The report could not be read. It may have been moved or deleted."))
        self.view.setPlainText(self.report)
        where = ElidedLabel(tr("Saved as {path}", path=str(self.path)), "meta")

        buttons = QHBoxLayout()
        self.open_btn = QPushButton(tr("Open reports folder"))
        self.open_btn.clicked.connect(self._open_folder)
        self.copy_btn = QPushButton(tr("Copy report"))
        self.copy_btn.setEnabled(bool(self.report))
        self.copy_btn.clicked.connect(self._copy)
        self.dismiss_btn = QPushButton(tr("Dismiss"))
        self.dismiss_btn.setObjectName("primary")
        self.dismiss_btn.setDefault(True)
        self.dismiss_btn.clicked.connect(self.accept)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.dismiss_btn)

        for w in (title, intro, self.view, where):
            lay.addWidget(w)
        lay.addSpacing(4)
        lay.addLayout(buttons)

        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(COPIED_FOR_MS)
        self._copied.timeout.connect(lambda: self.copy_btn.setText(tr("Copy report")))
        self.dismiss_btn.setFocus()

    def _read(self) -> str:
        try:
            with open(self.path, "rb") as fh:
                return fh.read(MAX_BYTES).decode("utf-8", "replace")
        except OSError:
            return ""

    def _open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

    def _copy(self):
        QGuiApplication.clipboard().setText(self.report)
        self.copy_btn.setText(tr("Report copied"))
        self._copied.start()
