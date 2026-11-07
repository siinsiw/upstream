"""History tab: what Upstream installed, updated, rolled back, removed and cleaned up, newest first.

Entries are stored as an English message template plus values (history.py) and rendered here, when they are
shown, so a language switch also applies to old entries. Colours are read from the current theme when painted."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import history
from ..history import HistoryEntry
from ..i18n import N_, SOURCE_LABELS, isolate, num, render, tr, trn
from .theme import current
from .widgets import ElidedLabel, ui_is_rtl

log = logging.getLogger("upstream.ui.history")

LOAD_LIMIT = 1000     # entries read from disk
PAGE = 50             # rows built at a time: a row is a dozen widgets and styling them takes a few ms each
SEARCH_DELAY_MS = 120
VERSION_WIDTH = 170   # columns of a row that have a fixed width
WHEN_WIDTH = 100
KIND_LABELS = {
    "install": N_("New install"), "update": N_("Update"), "rollback": N_("Rollback"),
    "uninstall": N_("Uninstall"), "cleanup": N_("Leftover cleanup"), "restore": N_("Cleanup undone"),
}
FILTERS = (   # (kind, label); "" shows everything
    ("", N_("All activity")), ("install", N_("Installs")), ("update", N_("Updates")),
    ("rollback", N_("Rollbacks")), ("uninstall", N_("Uninstalls")), ("cleanup", N_("Leftover cleanups")),
    ("restore", N_("Undone cleanups")),
)


def _moment(ts: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def full_time(ts: float) -> str:
    moment = _moment(ts)
    return isolate(num(moment.strftime("%Y-%m-%d %H:%M:%S"))) if moment else ""


def when_text(ts: float, now: float | None = None) -> str:
    """'5 minutes ago', 'Yesterday', and the date once it is a week old."""
    now = time.time() if now is None else now
    age = now - ts
    if age < 60:     # also an entry stamped a little ahead of this clock
        return tr("Just now")
    if age < 3600:
        return trn("{n} minute ago", "{n} minutes ago", int(age // 60))
    if age < 86400:
        return trn("{n} hour ago", "{n} hours ago", int(age // 3600))
    moment, today = _moment(ts), _moment(now)
    if not moment or not today:
        return full_time(ts)
    days = (today.date() - moment.date()).days
    if days <= 1:
        return tr("Yesterday")
    if days < 7:
        return trn("{n} day ago", "{n} days ago", days)
    return isolate(num(moment.strftime("%Y-%m-%d")))


def kind_label(kind: str) -> str:
    return tr(KIND_LABELS[kind]) if kind in KIND_LABELS else kind


def message_of(entry: HistoryEntry) -> str:
    """The entry's message in the current language. A template that no longer fits its values shows as it is."""
    if not entry.template:
        return ""
    try:
        return render(entry.template, entry.values)
    except Exception:
        return entry.template


def _mix(top: QColor, base: QColor, amount: float) -> QColor:
    return QColor(*(round(a * amount + b * (1 - amount)) for a, b in
                    ((top.red(), base.red()), (top.green(), base.green()), (top.blue(), base.blue()))))


def _arrow_head(path: QPainterPath, tip: QPointF, heading: float, size: float = 6.0, spread: float = 0.75):
    """Two strokes that make `tip` an arrow tip when the line arrives there going in direction `heading` (radians)."""
    for side in (-spread, spread):
        angle = heading + math.pi + side
        path.moveTo(tip)
        path.lineTo(tip.x() + size * math.cos(angle), tip.y() + size * math.sin(angle))


def _glyph_path(kind: str) -> QPainterPath:
    """Line drawings in a 36 x 36 box, one per kind of entry."""
    p = QPainterPath()
    if kind == "install":                                   # plus
        p.moveTo(18, 11), p.lineTo(18, 25), p.moveTo(11, 18), p.lineTo(25, 18)
    elif kind == "update":                                  # arrow up
        p.moveTo(18, 25), p.lineTo(18, 11), _arrow_head(p, QPointF(18, 11), -math.pi / 2)
    elif kind == "rollback":                                # arrow down
        p.moveTo(18, 11), p.lineTo(18, 25), _arrow_head(p, QPointF(18, 25), math.pi / 2)
    elif kind == "uninstall":                               # bin
        p.moveTo(11.5, 13), p.lineTo(24.5, 13)
        p.moveTo(15.5, 13), p.lineTo(15.5, 10.8), p.lineTo(20.5, 10.8), p.lineTo(20.5, 13)
        p.moveTo(13, 13), p.lineTo(14, 25), p.lineTo(22, 25), p.lineTo(23, 13)
    elif kind == "cleanup":                                 # sparkle
        p.moveTo(18, 9.5)
        for x, y in ((26.5, 18), (18, 26.5), (9.5, 18), (18, 9.5)):
            p.quadTo(18, 18, x, y)
    elif kind == "restore":                                 # arrow going round, anticlockwise
        centre, r = QPointF(18, 18.5), 7.0
        box = QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r)
        p.arcMoveTo(box, 225)
        p.arcTo(box, 225, 270)      # Qt measures anticlockwise from 3 o'clock; the arc stops at the upper left
        end = math.radians(135)
        _arrow_head(p, QPointF(centre.x() + r * math.cos(end), centre.y() - r * math.sin(end)),
                    math.atan2(-math.cos(end), -math.sin(end)), 5.0)   # heading on, y pointing down on screen
    else:                                                   # an entry from a newer version: a dot
        p.addEllipse(QPointF(18, 18), 1.6, 1.6)
    return p


class KindGlyph(QWidget):
    """A rounded square like the app avatar, with a small drawing for the kind of entry."""
    SIZE = 36

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self._path = _glyph_path(kind)
        self.setFixedSize(self.SIZE, self.SIZE)

    def colours(self) -> tuple[QColor, QColor]:
        """(background, stroke) from the current theme: green for what adds or brings back, red for what removes."""
        t = current()
        if self.kind in ("install", "update", "restore"):
            return QColor(t["accent_soft"]), QColor(t["accent"])
        if self.kind == "uninstall":
            return _mix(QColor(t["danger"]), QColor(t["surface"]), 0.16), QColor(t["danger"])
        return QColor(t["raised"]), QColor(t["text"])

    def paintEvent(self, event):
        background, stroke = self.colours()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(background)
        p.drawRoundedRect(QRectF(0, 0, self.SIZE, self.SIZE), 9, 9)
        p.setPen(QPen(stroke, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(self._path)
        p.end()


def _version_label(text: str, name: str) -> ElidedLabel:
    """A version keeps its own width when there is room and is cut with an ellipsis when there is not."""
    label = ElidedLabel(text, name)
    label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    return label


class HistoryRow(QFrame):
    """One entry: kind, app, versions, how it went and when. Same card as an app row."""

    def __init__(self, entry: HistoryEntry, message: str, now: float | None = None):
        super().__init__()
        self.setObjectName("row")
        self.entry = entry
        self._marked = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(14)
        lay.addWidget(KindGlyph(entry.kind))

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self.name = ElidedLabel(entry.name, "appName")
        text_col.addWidget(self.name)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.kind = QLabel(kind_label(entry.kind))
        self.kind.setObjectName("meta")
        meta.addWidget(self.kind)
        source = entry.source if entry.source in SOURCE_LABELS else ""
        if source:
            badge = QLabel(tr(SOURCE_LABELS[source]))
            badge.setObjectName("badge")
            badge.setProperty("source", source)
            meta.addWidget(badge)
        meta.addStretch(1)
        text_col.addLayout(meta)
        lay.addLayout(text_col, 3)

        self.versions = self._version_pair(entry)
        lay.addWidget(self.versions)

        result = QVBoxLayout()
        result.setSpacing(3)
        self.result = QLabel(tr("Succeeded") if entry.ok else tr("Failed"))
        self.result.setProperty("tone", "muted" if entry.ok else "danger")   # a failure is what to spot in a list
        self.detail = ElidedLabel(" ".join(message.split()), "meta")
        self.detail.setToolTip(message)
        self.detail.setVisible(bool(message))
        policy = self.detail.sizePolicy()
        policy.setRetainSizeWhenHidden(True)    # every row is as tall as the others
        self.detail.setSizePolicy(policy)
        result.addWidget(self.result)
        result.addWidget(self.detail)
        lay.addLayout(result, 2)

        self.when = ElidedLabel("", "meta", trailing=True)
        self.when.setFixedWidth(WHEN_WIDTH)
        self.when.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)   # Ignored would give it no room in the layout
        lay.addWidget(self.when)
        self.refresh_time(now)

        stamp = full_time(entry.ts)
        self.setToolTip("\n".join(part for part in (stamp, message) if part))
        versions = " → ".join(v for v in (entry.version_from, entry.version_to) if v)
        self.setAccessibleName(", ".join(part for part in (
            kind_label(entry.kind), entry.name, versions, self.result.text(), message, self.when.full_text) if part))

    @staticmethod
    def _version_pair(entry: HistoryEntry) -> QWidget:
        """'1.2 → 1.3', or one version when the other does not apply (an install has no old one, an uninstall no new).
        A fixed column, so the results line up; a long version string is cut, the whole of it is in the tooltip."""
        box = QWidget()
        box.setFixedWidth(VERSION_WIDTH)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        old, new = entry.version_from, entry.version_to
        if old:
            lay.addWidget(_version_label(old, "oldVer"))
        if old and new:
            arrow = QLabel("←" if ui_is_rtl() else "→")
            arrow.setObjectName("arrow")
            lay.addWidget(arrow)
        if new:   # a failed attempt did not get there: no accent
            lay.addWidget(_version_label(new, "newVer" if entry.ok else "oldVer"))
        lay.addStretch(1)
        return box

    def refresh_time(self, now: float | None = None):
        self.when.set_full_text(when_text(self.entry.ts, now))
        self.when.setToolTip(full_time(self.entry.ts))

    def set_marked(self, on: bool):
        if on != self._marked:
            self._marked = on
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._marked:    # the keyboard position in the list
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(current()["accent"]), 2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)
            p.end()


class _List(QListWidget):
    """The rows. It takes the keyboard (arrow keys, Page Up/Down, Home/End) and marks the current row."""

    def __init__(self):
        super().__init__()
        self.setSpacing(3)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(20)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.currentItemChanged.connect(self.mark_current)

    def mark_current(self, *_):
        current_item, focused = self.currentItem(), self.hasFocus()
        for i in range(self.count()):
            item = self.item(i)
            row = self.itemWidget(item)
            if isinstance(row, HistoryRow):
                row.set_marked(focused and item is current_item)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.mark_current()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.mark_current()


class HistoryPage(QWidget):
    cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[HistoryEntry] = []
        self._index: list[tuple[HistoryEntry, str, str]] = []   # entry, text to search in, message
        self._recording = True
        self._matches: list[tuple[HistoryEntry, str]] = []      # what the filter and search let through: entry, message
        self._shown = PAGE
        self._row_height: int | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(250)
        self.kind_filter = QComboBox()
        for key, label in FILTERS:
            self.kind_filter.addItem(tr(label), key)
        self.clear_btn = QPushButton()
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        bar.addWidget(self.search)
        bar.addWidget(self.kind_filter)
        bar.addStretch(1)
        bar.addWidget(self.clear_btn)
        v.addLayout(bar)
        v.addSpacing(12)

        self.notice = QFrame()
        self.notice.setObjectName("notice")
        nl = QHBoxLayout(self.notice)
        nl.setContentsMargins(14, 10, 14, 10)
        self.notice_text = QLabel()
        self.notice_text.setWordWrap(True)
        self.notice_text.setProperty("tone", "muted")
        nl.addWidget(self.notice_text)
        self.notice.hide()
        v.addWidget(self.notice)
        v.addSpacing(10)

        self.stack = QStackedWidget()
        self.list = _List()
        self.stack.addWidget(self.list)
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.addStretch(2)
        self.empty_title = QLabel()
        self.empty_title.setObjectName("emptyTitle")
        self.empty_text = QLabel()
        self.empty_text.setObjectName("emptyText")
        self.empty_text.setWordWrap(True)
        for w in (self.empty_title, self.empty_text):
            w.setAlignment(Qt.AlignCenter)
            el.addWidget(w)
        el.addStretch(3)
        self.stack.addWidget(self.empty)
        v.addWidget(self.stack, 1)

        self.more_btn = QPushButton()
        self.more_btn.setObjectName("link")
        self.more_btn.setCursor(Qt.PointingHandCursor)
        self.more_btn.hide()
        v.addWidget(self.more_btn, 0, Qt.AlignHCenter)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DELAY_MS)
        self._debounce.timeout.connect(self._filter_changed)
        self._tick = QTimer(self)   # while the tab stays open: pick up new entries, and 'x minutes ago' ages
        self._tick.setInterval(60_000)
        self._tick.timeout.connect(self.refresh)

        self.search.textChanged.connect(lambda _: self._debounce.start())
        self.kind_filter.currentIndexChanged.connect(lambda _: self._filter_changed())
        self.clear_btn.clicked.connect(self.clear_history)
        self.more_btn.clicked.connect(self._show_more)
        find = QShortcut(QKeySequence.Find, self)
        find.setContext(Qt.WidgetWithChildrenShortcut)
        find.activated.connect(self._focus_search)
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self.search)
        escape.setContext(Qt.WidgetShortcut)
        escape.activated.connect(self.search.clear)
        self.setTabOrder(self.search, self.kind_filter)
        self.setTabOrder(self.kind_filter, self.clear_btn)
        self.setTabOrder(self.clear_btn, self.list)
        self.setTabOrder(self.list, self.more_btn)

        self.retranslate()
        self.refresh()

    # -- data ----------------------------------------------------------
    def refresh(self) -> None:
        """Reload from history.load(). The rows stay as they are when nothing new was written."""
        entries = history.load(LOAD_LIMIT)
        recording = history.is_enabled()
        if [e.id for e in entries] == [e.id for e in self._entries] and recording == self._recording:
            self._age_rows()
            return
        self._entries, self._recording = entries, recording
        self._shown = PAGE
        self._reindex()
        self._rebuild()

    def _reindex(self):
        self._index = []
        for entry in self._entries:
            message = message_of(entry)
            source = tr(SOURCE_LABELS[entry.source]) if entry.source in SOURCE_LABELS else entry.source
            hay = " ".join((entry.name, entry.version_from, entry.version_to, message, kind_label(entry.kind), source))
            self._index.append((entry, hay.casefold(), message))

    # -- rows ----------------------------------------------------------
    def _rebuild(self, keep_scroll: bool = False):
        kind = self.kind_filter.currentData()
        needle = self.search.text().strip().casefold()
        self._matches = [(e, m) for e, hay, m in self._index if (not kind or e.kind == kind) and needle in hay]
        scroll = self.list.verticalScrollBar().value() if keep_scroll else 0
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        self._row_height = None
        self._add_rows(self._matches[:self._shown])
        self.list.setUpdatesEnabled(True)
        self.list.verticalScrollBar().setValue(scroll)
        self._sync()

    def _add_rows(self, matches: list[tuple[HistoryEntry, str]]):
        now = time.time()
        for entry, message in matches:
            row = HistoryRow(entry, message, now)
            if self._row_height is None:    # every row is as tall as the first; asking each one is slow
                self._row_height = row.sizeHint().height()
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, self._row_height))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def _sync(self):
        """Everything that depends on what is listed: the empty state, the notice, the buttons."""
        self.clear_btn.setEnabled(bool(self._entries))
        self.notice.setVisible(not self._recording and bool(self._entries))
        left = len(self._matches) - self.list.count()
        self.more_btn.setVisible(left > 0)
        self.more_btn.setText(trn("Show {n} older entry", "Show {n} older entries", left) if left > 0 else "")
        if self.list.count():
            self.stack.setCurrentWidget(self.list)
            return
        if not self._entries and not self._recording:
            title = tr("History is turned off")
            text = tr("Turn it on in Settings to keep a record of installs, updates and uninstalls.")
        elif not self._entries:
            title = tr("No history yet")
            text = tr("Installs, updates and uninstalls will be listed here.")
        else:
            title, text = tr("Nothing matches"), ""
        self.empty_title.setText(title)
        self.empty_text.setText(text)
        self.stack.setCurrentWidget(self.empty)

    def _filter_changed(self):
        self._shown = PAGE
        self._rebuild()

    def _show_more(self):
        built = self.list.count()
        self._shown += PAGE
        self._add_rows(self._matches[built:self._shown])
        self._sync()

    def _age_rows(self):
        now = time.time()
        for i in range(self.list.count()):
            row = self.list.itemWidget(self.list.item(i))
            if isinstance(row, HistoryRow):
                row.refresh_time(now)

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    # -- actions -------------------------------------------------------
    def clear_history(self) -> None:
        if not self._confirm_clear():
            return
        history.clear()
        self.refresh()
        self.cleared.emit()

    def _confirm_clear(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Upstream")
        box.setText(tr("Clear the whole history?"))
        box.setInformativeText(tr("This deletes the record of what Upstream installed, updated and removed. "
                                  "Your apps and files are not touched."))
        yes = box.addButton(tr("Clear history"), QMessageBox.DestructiveRole)
        no = box.addButton(tr("Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        return box.clickedButton() is yes

    # -- language ------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply every visible text after a language switch."""
        self.search.setPlaceholderText(tr("Search history"))
        self.search.setAccessibleName(tr("Search history"))
        self.kind_filter.setAccessibleName(tr("Show only"))
        for i, (_, label) in enumerate(FILTERS):
            self.kind_filter.setItemText(i, tr(label))
        self.list.setAccessibleName(tr("History entries"))
        self.clear_btn.setText(tr("Clear history…"))
        self.notice_text.setText(tr("History is turned off, so new activity is not being recorded. "
                                    "You can turn it on in Settings."))
        self._reindex()
        self._rebuild(keep_scroll=True)

    # -- visibility ----------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._tick.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._tick.stop()
