"""The All apps tab: sorting, filters, duplicates, size and date columns, multi-select.

The window builds one AppRow per app through `row_factory(status, skipped, ignored) -> AppRow` (it connects the row's
signals and keeps its own row registry for job-state replay); the page only arranges them. The rows are placed by
hand inside a scroll area and only the ones near the viewport are shown, which builds their widgets (see AppRow):
sorting, filtering and scrolling never create or destroy a row, and 150 apps cost what a screenful costs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from ..details import format_date, format_size
from ..i18n import N_, isolate, num, tr, trn
from ..scanner import AppStatus
from ..versioning import parse_version
from .widgets import AppRow

ROW_GAP = 3
BUFFER = 240        # px above and below the viewport whose rows are already built and shown

SORTS = (
    ("name", N_("Name")), ("size", N_("Size, largest first")), ("date", N_("Install date, newest first")),
    ("publisher", N_("Publisher")), ("updates", N_("Updates first")),
)
FILTERS = (
    ("all", N_("All")), ("updates", N_("Updates available")), ("machine", N_("Machine-wide")),
    ("user", N_("Current user")), ("duplicates", N_("Possible duplicates")), ("flagged", N_("Flagged")),
    ("pinned", N_("Pinned")),
)

# Copies of these are meant to sit side by side: runtimes and SDKs that apps pick by version, and JDKs.
_SIDE_BY_SIDE = re.compile(
    r"visual c\+\+|\.net\b|asp\.net|windows desktop runtime|windows app runtime|windows app sdk|"
    r"windows (?:software development kit|sdk)|\bj(?:dk|re)\b|\bjava\b|temurin|corretto|openjdk", re.I)
_PYTHON = re.compile(r"python\b", re.I)
MAX_OTHERS = 4


def find_duplicates(statuses: list[AppStatus]) -> dict[str, list[AppStatus]]:
    """uid -> the other copies, for apps listed more than once under the same name with different versions or for
    different scopes. A 32 and a 64-bit build of the same version are one product, not a duplicate; the side-by-side
    families above are never flagged, and Python is compared per minor version (3.11 and 3.12 belong together)."""
    groups: dict[tuple, list[AppStatus]] = {}
    for s in statuses:
        app = s.app
        if not app.ident or app.reg_key.startswith("WINGET\\") or _SIDE_BY_SIDE.search(app.name):
            continue
        key: tuple = (app.ident, re.sub(r"\W+", "", app.publisher.casefold()))
        if _PYTHON.match(app.name):
            key += (((parse_version(app.version) or ()) + (0, 0))[:2],)
        groups.setdefault(key, []).append(s)
    found: dict[str, list[AppStatus]] = {}
    for members in groups.values():
        if len(members) > 1 and (len({m.app.version for m in members}) > 1
                                 or len({m.app.scope for m in members}) > 1):
            for m in members:
                found[m.uid] = [o for o in members if o is not m]
    return found


def _scope_label(scope: str) -> str:
    return tr("All users") if scope == "machine" else tr("Current user only")


@dataclass
class _Entry:
    status: AppStatus
    row: AppRow
    skipped: bool
    others: list[AppStatus]         # the other copies of a possible duplicate

    @property
    def has_update(self) -> bool:
        return self.status.update is not None and not self.skipped


def _name_key(e: _Entry):
    return e.status.app.name.casefold(), e.status.uid


def _date_number(e: _Entry) -> int:
    stamp = e.status.app.install_date
    return int(stamp) if format_date(stamp) else 0


# Each sort lists known values first: an app that reports no size, date or publisher goes last, never first.
SORT_KEYS: dict[str, Callable[[_Entry], tuple]] = {
    "name": _name_key,
    "size": lambda e: (e.status.app.size_kb <= 0, -e.status.app.size_kb, _name_key(e)),
    "date": lambda e: (_date_number(e) == 0, -_date_number(e), _name_key(e)),
    "publisher": lambda e: (not e.status.app.publisher.strip(), e.status.app.publisher.casefold(), _name_key(e)),
    "updates": lambda e: (not e.has_update, _name_key(e)),
}

FILTER_TESTS: dict[str, Callable[[_Entry], bool]] = {
    "all": lambda e: True,
    "updates": lambda e: e.has_update,
    "machine": lambda e: e.status.app.scope == "machine",
    "user": lambda e: e.status.app.scope == "user",
    "duplicates": lambda e: bool(e.others),
    "flagged": lambda e: bool(e.status.badges),
    "pinned": lambda e: bool(e.status.pin),
}


class _Scroll(QScrollArea):
    resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class LibraryPage(QWidget):
    batch_uninstall_requested = Signal(list)    # uids
    batch_export_requested = Signal(list)       # uids
    batch_ignore_requested = Signal(list)       # uids

    def __init__(self, row_factory: Callable[[AppStatus, bool, bool], AppRow], parent=None):
        super().__init__(parent)
        self.row_factory = row_factory
        self._entries: dict[str, _Entry] = {}       # every app handed to set_statuses, in that order
        self._order: list[str] = []                 # uids of the rows that pass the filter, in display order
        self._selected: set[str] = set()
        self._select_mode = False
        self._text = ""
        self._row_h = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._show_label, self._sort_label = QLabel(), QLabel()
        self.filter_box, self.sort_box = QComboBox(), QComboBox()
        for label, box in ((self._show_label, self.filter_box), (self._sort_label, self.sort_box)):
            label.setProperty("tone", "muted")
            label.setBuddy(box)
        for key, _ in FILTERS:
            self.filter_box.addItem("", key)
        for key, _ in SORTS:
            self.sort_box.addItem("", key)
        self.filter_box.currentIndexChanged.connect(self._view_changed)
        self.sort_box.currentIndexChanged.connect(self._view_changed)
        self.summary = QLabel()
        self.summary.setObjectName("hint")
        self.select_btn = QPushButton()
        self.select_btn.setCheckable(True)
        self.select_btn.setCursor(Qt.PointingHandCursor)
        self.select_btn.toggled.connect(self.set_select_mode)
        tools.addWidget(self._show_label)
        tools.addWidget(self.filter_box)
        tools.addSpacing(10)
        tools.addWidget(self._sort_label)
        tools.addWidget(self.sort_box)
        tools.addStretch(1)
        tools.addWidget(self.summary)
        tools.addSpacing(6)
        tools.addWidget(self.select_btn)
        lay.addLayout(tools)

        self._canvas = QWidget()
        self._scroll = _Scroll()
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.verticalScrollBar().setSingleStep(20)
        self._scroll.verticalScrollBar().valueChanged.connect(self._place)
        self._scroll.resized.connect(self._place)
        self._empty = QWidget()
        el = QVBoxLayout(self._empty)
        el.addStretch(2)
        self._empty_title, self._empty_text = QLabel(), QLabel()
        self._empty_title.setObjectName("emptyTitle")
        self._empty_text.setObjectName("emptyText")
        self._empty_text.setWordWrap(True)
        for w in (self._empty_title, self._empty_text):
            w.setAlignment(Qt.AlignCenter)
            el.addWidget(w)
        el.addStretch(3)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._empty)
        lay.addWidget(self._stack, 1)

        self._bar = QFrame()
        self._bar.setObjectName("notice")
        bl = QHBoxLayout(self._bar)
        bl.setContentsMargins(14, 8, 10, 8)
        bl.setSpacing(8)
        self._count = QLabel()
        self._count.setProperty("tone", "accent")
        self._all_btn, self._clear_btn = QPushButton(), QPushButton()
        for b in (self._all_btn, self._clear_btn):
            b.setObjectName("link")
            b.setCursor(Qt.PointingHandCursor)
        self._all_btn.clicked.connect(self.select_all)
        self._clear_btn.clicked.connect(self.clear_selection)
        self._uninstall_btn, self._export_btn, self._ignore_btn = QPushButton(), QPushButton(), QPushButton()
        for b, signal in ((self._uninstall_btn, self.batch_uninstall_requested),
                          (self._export_btn, self.batch_export_requested),
                          (self._ignore_btn, self.batch_ignore_requested)):
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, s=signal: s.emit(self.selected_uids()))
        bl.addWidget(self._count)
        bl.addWidget(self._all_btn)
        bl.addStretch(1)
        for b in (self._uninstall_btn, self._export_btn, self._ignore_btn, self._clear_btn):
            bl.addWidget(b)
        self._bar.hide()
        lay.addWidget(self._bar)

        # Ctrl+A ticks the shown rows, Esc leaves select mode; a text field that has the focus keeps its own Ctrl+A.
        self._select_all_key = QShortcut(QKeySequence.SelectAll, self)
        self._select_all_key.activated.connect(self._ctrl_a)
        self._leave_key = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._leave_key.activated.connect(lambda: self.set_select_mode(False))
        self._leave_key.setEnabled(False)

        self.retranslate()

    # -- content ---------------------------------------------------------------------------------------
    def set_statuses(self, statuses: list[AppStatus], skipped: set[str], ignored: set[str]) -> None:
        """Rebuild the rows. `statuses` are the non-ignored apps; skipped and ignored hold uids. The sort, the
        filter, the select mode and the ticks of apps that are still there stay as they were."""
        for e in self._entries.values():
            e.row.setParent(None)
            e.row.deleteLater()
        self._entries = {}
        found = find_duplicates(statuses)
        for s in statuses:
            row = self.row_factory(s, s.uid in skipped, s.uid in ignored)
            row.setParent(self._canvas)
            row.hide()      # shown (and built) only when it is near the viewport
            row.selection_changed.connect(self._ticked)
            self._entries[s.uid] = _Entry(s, row, s.uid in skipped, found.get(s.uid, []))
        self._selected.intersection_update(self._entries)
        for e in self._entries.values():
            self._decorate(e)
        first = next(iter(self._entries.values()), None)
        self._row_h = first.row.sizeHint().height() if first else 0
        self._refresh_view()

    def _decorate(self, e: _Entry):
        """What the library adds to a row: size and date, the duplicate marker, the select state."""
        app, row = e.status.app, e.row
        size = num(format_size(app.size_kb * 1024)) if app.size_kb > 0 else ""
        row.set_meta(size, num(format_date(app.install_date)))
        if e.others:
            listed = ", ".join(f"{o.app.version or '?'} ({_scope_label(o.app.scope)})" for o in e.others[:MAX_OTHERS])
            more = " …" if len(e.others) > MAX_OTHERS else ""
            row.set_duplicate(tr("Also installed: {others}", others=listed + more))
        else:
            row.set_duplicate("")
        row.set_selectable(self._select_mode)
        row.set_selected(e.status.uid in self._selected)

    def set_filter_text(self, text: str) -> None:
        """The window's search box text; hide rows that do not match (case-insensitive, name and publisher)."""
        self._text = (text or "").strip().casefold()
        self._refresh_view()

    def visible_count(self) -> int:
        """Rows that pass the filter and the search text."""
        return len(self._order)

    def count(self) -> int:
        """Every row on the page, whatever the filter says."""
        return len(self._entries)

    def row_for(self, uid: str) -> AppRow | None:
        e = self._entries.get(uid)
        return e.row if e else None

    # -- view: filter, sort, placement -------------------------------------------------------------------
    def _passes(self, e: _Entry) -> bool:
        if not FILTER_TESTS[self.filter_box.currentData()](e):
            return False
        app = e.status.app
        return not self._text or self._text in f"{app.name} {app.publisher}".casefold()

    def _view_changed(self):
        self._scroll.verticalScrollBar().setValue(0)
        self._refresh_view()

    def _refresh_view(self):
        passing = (e for e in self._entries.values() if self._passes(e))
        self._order = [e.status.uid for e in sorted(passing, key=SORT_KEYS[self.sort_box.currentData()])]
        keep = set(self._order)
        for uid, e in self._entries.items():
            if uid not in keep and not e.row.isHidden():
                e.row.hide()
        # A tick never outlives its row's visibility: what the bar counts is what its buttons act on.
        for uid in self._selected - keep:
            self._entries[uid].row.set_selected(False)
        self._selected.intersection_update(keep)
        self._stack.setCurrentWidget(self._scroll if self._order else self._empty)
        if not self._order:
            self._empty_title.setText(tr("Nothing matches") if self._entries else tr("No apps found"))
            self._empty_text.setText(tr("Try another filter, or clear the search.") if self._entries else "")
        self._place_rows()
        self._chain_tab_order()
        self._update_summary()
        self._update_bar()

    def _place(self, *_):
        """The viewport moved or was resized: show the rows that came near it."""
        if self._place_rows():
            self._chain_tab_order()

    def _place_rows(self) -> bool:
        """Position the rows and show only those near the viewport (building them on the way). True when a row
        was newly shown."""
        step = self._row_h + ROW_GAP
        width = self._scroll.viewport().width()
        total = len(self._order) * step - ROW_GAP if self._order else 0
        self._canvas.resize(width, max(total, 0))
        top = self._scroll.verticalScrollBar().value()
        low, high = top - BUFFER, top + self._scroll.viewport().height() + BUFFER
        changed = False
        for i, uid in enumerate(self._order):
            row = self._entries[uid].row
            y = i * step
            if y + self._row_h < low or y > high:
                if not row.isHidden():
                    row.hide()
                continue
            row.setGeometry(0, y, width, self._row_h)
            if row.isHidden():
                row.build()
                row.show()
                changed = True
        return changed

    def _chain_tab_order(self):
        """Tab visits the rows in the order they are shown, whatever order they were created in."""
        previous = self.select_btn
        for uid in self._order:
            row = self._entries[uid].row
            if row.isHidden():
                continue
            for w in row.focus_widgets():
                QWidget.setTabOrder(previous, w)
                previous = w
        if self._bar.isVisibleTo(self):
            QWidget.setTabOrder(previous, self._all_btn)

    # -- summary and selection bar -----------------------------------------------------------------------
    def _update_summary(self):
        total, shown = len(self._entries), len(self._order)
        parts = [trn("{n} app", "{n} apps", total) if shown == total
                 else trn("{shown} of {n} app", "{shown} of {n} apps", total, shown=num(shown))]
        size_kb = sum(self._entries[uid].status.app.size_kb for uid in self._order)
        if size_kb > 0:
            parts.append(isolate(num(format_size(size_kb * 1024))))
        self.summary.setText(" · ".join(parts))
        self.summary.setToolTip(tr("Sizes are what each app reports to Windows. Some report none, "
                                   "so the total can be lower than the real figure."))

    def _update_bar(self):
        n = len(self._selected)
        self._count.setText(tr("{n} selected", n=num(n)))
        for b in (self._uninstall_btn, self._export_btn, self._ignore_btn, self._clear_btn):
            b.setEnabled(n > 0)
        self._all_btn.setEnabled(len(self._order) > n)

    def _ticked(self, uid: str, on: bool):
        (self._selected.add if on else self._selected.discard)(uid)
        self._update_bar()

    # -- select mode -------------------------------------------------------------------------------------------
    @property
    def select_mode(self) -> bool:
        return self._select_mode

    def set_select_mode(self, on: bool) -> None:
        on = bool(on)
        if self.select_btn.isChecked() != on:
            self.select_btn.setChecked(on)      # comes back through the toggled signal
            return
        self._select_mode = on
        if not on:
            self._selected.clear()
        for e in self._entries.values():
            e.row.set_selectable(on)
            e.row.set_selected(e.status.uid in self._selected)
        self._bar.setVisible(on)
        self._leave_key.setEnabled(on)
        self.select_btn.setText(tr("Done") if on else tr("Select"))
        self._update_bar()
        self._chain_tab_order()

    def selected_uids(self) -> list[str]:
        """The ticked rows, in the order they are shown."""
        return [uid for uid in self._order if uid in self._selected]

    def select_all(self) -> None:
        """Tick every row that passes the filter and the search."""
        self._selected = set(self._order)
        for uid in self._order:
            self._entries[uid].row.set_selected(True)
        self._update_bar()

    def clear_selection(self) -> None:
        for uid in self._selected:
            self._entries[uid].row.set_selected(False)
        self._selected.clear()
        self._update_bar()

    def _ctrl_a(self):
        self.set_select_mode(True)
        self.select_all()

    # -- language --------------------------------------------------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply every visible text after a language switch. The window rebuilds the rows itself (set_statuses
        again); this also re-translates what the rows of the current list show of the library."""
        self._show_label.setText(tr("Show"))
        self._sort_label.setText(tr("Sort by"))
        for box, table in ((self.filter_box, FILTERS), (self.sort_box, SORTS)):
            for i, (_, label) in enumerate(table):
                box.setItemText(i, tr(label))
        self.filter_box.setAccessibleName(tr("Show"))
        self.sort_box.setAccessibleName(tr("Sort by"))
        self.select_btn.setText(tr("Done") if self._select_mode else tr("Select"))
        self.select_btn.setToolTip(tr("Tick several apps to uninstall, export or ignore them together. "
                                      "Ctrl+A ticks every app shown."))
        self._all_btn.setText(tr("Select all"))
        self._clear_btn.setText(tr("Clear selection"))
        self._uninstall_btn.setText(tr("Uninstall…"))
        self._export_btn.setText(tr("Export selected…"))
        self._ignore_btn.setText(tr("Ignore selected"))
        for e in self._entries.values():
            self._decorate(e)
            e.row.retranslate()
        self._update_summary()
        self._update_bar()
        if not self._order:
            self._empty_title.setText(tr("Nothing matches") if self._entries else tr("No apps found"))
            self._empty_text.setText(tr("Try another filter, or clear the search.") if self._entries else "")
