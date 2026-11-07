"""Tray icon with its menu. Owner: the background feature.

The icon is the app icon; while updates are waiting it carries a small dot in the theme's danger colour. The
tooltip and the menu are rebuilt by refresh(), which Background calls every minute and the menu calls when it
opens, so a language or theme switch shows up the next time the user looks."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..i18n import num, tr, trn
from .theme import app_icon, current

ICON_SIZES = (16, 24, 32, 48, 64)
MESSAGE_MS = 10_000


def badge_icon() -> QIcon:
    """The app icon with a dot in its top right corner."""
    base, dot = app_icon(), QColor(current()["danger"])
    icon = QIcon()
    for size in ICON_SIZES:
        pm = base.pixmap(size, size)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        d = max(8.0, size * 0.46)
        ring = QRectF(size - d, 0, d, d)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(ring)
        p.setBrush(dot)
        p.drawEllipse(ring.adjusted(d * 0.14, d * 0.14, -d * 0.14, -d * 0.14))
        p.end()
        icon.addPixmap(pm)
    return icon


class TrayIcon(QSystemTrayIcon):
    """Open, Check now, Update all (n), Quit. A click toggles the window, a double click always shows it."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.pending = 0
        self._icon_key: tuple | None = None
        self.menu = QMenu()
        self.open_action = self.menu.addAction("")
        font = self.open_action.font()
        font.setBold(True)
        self.open_action.setFont(font)
        self.check_action = self.menu.addAction("")
        self.update_action = self.menu.addAction("")
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("")
        self.open_action.triggered.connect(window.show_and_raise)
        self.check_action.triggered.connect(self._check_now)
        self.update_action.triggered.connect(self._update_all)
        self.quit_action.triggered.connect(window.quit_app)
        self.menu.aboutToShow.connect(self.refresh)
        self.setContextMenu(self.menu)
        self.activated.connect(self._on_activated)
        self.messageClicked.connect(window.show_and_raise)
        self.refresh()

    def refresh(self):
        """Re-read the pending count and re-apply the language and theme."""
        self.pending = len(self.window.pending_updates())
        busy = self.window.is_busy()
        self.open_action.setText(tr("Open Upstream"))
        self.check_action.setText(tr("Check now"))
        self.update_action.setText(tr("Update all ({n})", n=num(self.pending)) if self.pending else tr("Update all"))
        self.quit_action.setText(tr("Quit"))
        self.check_action.setEnabled(not busy)
        self.update_action.setEnabled(self.pending > 0 and not busy)
        status = trn("{n} update ready", "{n} updates ready", self.pending) if self.pending \
            else tr("Everything is up to date")
        self.setToolTip(f"Upstream\n{status}")
        key = (self.pending > 0, current()["danger"])
        if key != self._icon_key:
            self._icon_key = key
            self.setIcon(badge_icon() if self.pending else app_icon())

    def notify(self, title: str, body: str):
        """A tray message; clicking it opens the window."""
        if QSystemTrayIcon.supportsMessages():
            self.showMessage(title, body, app_icon(), MESSAGE_MS)

    def toggle_window(self):
        w = self.window
        if w.isVisible() and not w.isMinimized():
            w.hide()
        else:
            w.show_and_raise()

    def dispose(self):
        self.hide()
        self.menu.deleteLater()
        self.deleteLater()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.window.show_and_raise()

    def _check_now(self):
        self.window.show_and_raise()
        self.window.start_scan()

    def _update_all(self):
        self.window.show_and_raise()
        self.window.update_all()
