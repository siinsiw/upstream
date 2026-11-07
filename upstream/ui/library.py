"""The All apps tab: sorting, filters, duplicates, size and date columns, multi-select. Owner: the rows-and-library agent.

The window builds one AppRow per app through `row_factory(status, skipped, ignored) -> AppRow` (it connects the row's
signals and keeps its own row registry for job-state replay); the page only arranges them."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ..scanner import AppStatus


class LibraryPage(QWidget):
    batch_uninstall_requested = Signal(list)    # uids
    batch_export_requested = Signal(list)       # uids
    batch_ignore_requested = Signal(list)       # uids

    def __init__(self, row_factory: Callable[[AppStatus, bool, bool], object], parent=None):
        super().__init__(parent)
        self.row_factory = row_factory
        self.list = QListWidget()
        self.list.setSpacing(3)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.list)

    def set_statuses(self, statuses: list[AppStatus], skipped: set[str], ignored: set[str]) -> None:
        """Rebuild the rows. `statuses` are the non-ignored apps; skipped and ignored hold uids."""
        self.list.clear()
        for s in statuses:
            row = self.row_factory(s, s.uid in skipped, s.uid in ignored)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, s.uid)
            item.setSizeHint(QSize(0, row.sizeHint().height()))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def set_filter_text(self, text: str) -> None:
        """The window's search box text; hide rows that do not match (case-insensitive, name and publisher)."""

    def visible_count(self) -> int:
        return sum(1 for i in range(self.list.count()) if not self.list.item(i).isHidden())

    def retranslate(self) -> None:
        """Re-apply every visible text after a language switch."""
