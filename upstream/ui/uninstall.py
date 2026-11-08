"""The uninstall dialogs: confirm, run, result, a review of what was left behind, and removal of the ticked
leftovers with a backup and an undo.

uninstaller.run() starts the app's own uninstaller on a worker thread; leftovers.scan() only lists. Nothing from the
review is touched until the user ticks it and confirms, and cleanup.run() backs every item up first. The pages after
the uninstall are shared by UninstallDialog (one app) and BatchUninstallDialog (many apps, one combined review).
Messages from workers are (English template, values) pairs, translated when shown, and reach the UI through signals."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QStackedWidget, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import cleanup, guard, leftovers, uninstaller
from ..details import format_size
from ..i18n import english, isolate, num, render, tr, trn
from ..inventory import InstalledApp
from ..scanner import AppStatus
from ..versioning import parse_version
from . import theme
from .theme import current, letter_avatar
from .widgets import ElidedLabel, ui_is_rtl

log = logging.getLogger("upstream.ui.uninstall")

CONFIRM, RUNNING, RESULT, REVIEW, CLEAN_CONFIRM, CLEANING, SUMMARY = range(7)   # the pages of UninstallDialog
BATCH_CONFIRM, BATCH_RUNNING = 0, 1   # the first pages of BatchUninstallDialog; the shared pages follow
REVIEW_HEIGHT = 700
_LRI, _PDI = "⁦", "⁩"   # left-to-right isolate: a path stays in one piece inside an RTL window
_HIVES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}
_PATH_KINDS = ("folder", "file", "shortcut")
_RUNNING: set[str] = set()   # apps whose uninstaller runs, and cleanups under way, even if their dialog was closed
_ROW_ROLE = Qt.UserRole + 1   # "group" | "protected": the delegate colours these from the current theme
_PROBLEM_ROWS = 100           # failures listed on a result page; the rest is counted
_REVIEW_KEYS = ["location", "kind", "size", "owner", None]     # what each column of the leftover review sorts on
_APP_KEYS = ["name", "version", "size", None, None]            # the batch confirmation: app, version, size, how, heads-up
_STATE_KEYS = ["name", "version", "size", None]                # the batch result rows: app, version, size, outcome


def N_(text: str) -> str:
    """Marks a template for translation where a table defines it; tr() is applied when it is shown."""
    return text


KIND_LABELS = {"folder": N_("Folder"), "file": N_("File"), "shortcut": N_("Shortcut"),
               "regkey": N_("Registry key"), "regvalue": N_("Registry value"), "service": N_("Service")}
KIND_COUNTS = {"folder": (N_("{n} folder"), N_("{n} folders")), "file": (N_("{n} file"), N_("{n} files")),
               "shortcut": (N_("{n} shortcut"), N_("{n} shortcuts")),
               "regkey": (N_("{n} registry key"), N_("{n} registry keys")),
               "regvalue": (N_("{n} startup entry"), N_("{n} startup entries")),
               "service": (N_("{n} service"), N_("{n} services"))}
GROUPS = (
    (leftovers.CERTAIN, N_("Certain"), N_("Inside the app's folder, or pointing into it")),
    (leftovers.LIKELY, N_("Likely"), N_("Named exactly like the app")),
    (leftovers.POSSIBLE, N_("Possible"), N_("Weaker matches: check them yourself")),
    (leftovers.PROTECTED, N_("Protected"), N_("Found, but Upstream will never offer to remove them")),
)
_TICKABLE_GROUPS = (leftovers.CERTAIN, leftovers.LIKELY)   # these get a box that ticks the whole group
RESULT_TITLES = {  # outcome -> (headline, tone)
    "removed": (N_("Removed"), "accent"), "reboot_required": (N_("Restart needed"), "accent"),
    "still_listed": (N_("Not removed yet"), "danger"), "cancelled": (N_("Cancelled"), ""),
    "busy": (N_("Windows is busy"), ""), "failed": (N_("Couldn't remove it"), "danger"),
    "waiting": (N_("Waiting to start"), ""), "running": (N_("Being removed…"), "accent"),
    "skipped": (N_("Left alone"), ""),
}


def running() -> int:
    """How many uninstallers and cleanups are running right now (the main window warns before closing)."""
    return len(_RUNNING)


def _ltr(text: str) -> str:
    return f"{_LRI}{text}{_PDI}" if ui_is_rtl() else text


def size_text(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def app_size_text(app: InstalledApp) -> str:
    """The size Windows reports for an installed app ('45.2 MB', Persian digits in Persian); '' when it reports none."""
    return _ltr(num(format_size(app.size_kb * 1024))) if app.size_kb > 0 else ""


class _Sort:
    """Which column a list is sorted by, and which way. Nothing until the user clicks a header, so the list keeps the
    order it was given; the same header again reverses it. Rows whose value is unknown (None) always go last."""
    FIRST_DESCENDING = ("size",)    # a first click on these shows the biggest first; the others start at A

    def __init__(self):
        self.key: str | None = None
        self.descending = False

    def click(self, key: str):
        if key == self.key:
            self.descending = not self.descending
        else:
            self.set(key)

    def set(self, key: str, reverse: bool = False):
        """Sort on `key` in its usual direction, or the opposite one when `reverse`."""
        self.key, self.descending = key, (key in self.FIRST_DESCENDING) != reverse

    @property
    def reverse(self) -> bool:
        """Whether the list runs against the usual direction of its key: what a second click on a header does."""
        return self.key is not None and self.descending != (self.key in self.FIRST_DESCENDING)

    def apply(self, rows, values: dict[str, Callable]) -> list:
        """`rows` in order; values[key](row) is what the column sorts on."""
        rows = list(rows)
        value = values.get(self.key)
        if value is None:
            return rows
        known = [r for r in rows if value(r) is not None]
        known.sort(key=value, reverse=self.descending)   # equal rows keep the order they came in
        return known + [r for r in rows if value(r) is None]


def _sortable(tree: QTreeWidget, keys: list[str | None], clicked: Callable[[str], None]):
    """Clicking the header of a column with a key calls clicked(key). `keys` has one entry per column."""
    header = tree.header()
    header.setSectionsClickable(True)
    header.sectionClicked.connect(lambda column: keys[column] and clicked(keys[column]))
    for column, key in enumerate(keys):
        if key:
            tree.headerItem().setToolTip(column, tr("Click to sort. Click again to reverse the order."))


def _show_sort(tree: QTreeWidget, keys: list[str | None], sort: _Sort):
    header = tree.header()
    shown = sort.key is not None and sort.key in keys
    header.setSortIndicatorShown(shown)
    if shown:
        header.setSortIndicator(keys.index(sort.key), Qt.DescendingOrder if sort.descending else Qt.AscendingOrder)


class _SortBar(QWidget):
    """'Sort by' and a reverse box: what a click on a column header does, for the keyboard. It shows the sort the
    list is in, whichever of the two set it, and sorts nothing until a key is picked."""
    changed = Signal(str, bool)    # key, reverse

    def __init__(self, keys: list[tuple[str, str]]):
        """`keys` are (sort key, text shown)."""
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.label = QLabel(tr("Sort by"))
        self.combo = QComboBox()
        self.combo.setAccessibleName(tr("Sort by"))
        self.combo.setPlaceholderText("—")
        for key, text in keys:
            self.combo.addItem(text, key)
        self.combo.setCurrentIndex(-1)
        self.label.setBuddy(self.combo)
        self.reverse = QCheckBox(tr("Reverse order"))
        self.reverse.setEnabled(False)
        row.addWidget(self.label)
        row.addWidget(self.combo)
        row.addWidget(self.reverse)
        row.addStretch(1)
        self.combo.currentIndexChanged.connect(self._emit)
        self.reverse.toggled.connect(self._emit)

    def _emit(self, *_):
        key = self.combo.currentData()
        if key:
            self.reverse.setEnabled(True)
            self.changed.emit(key, self.reverse.isChecked())

    def show_sort(self, sort: _Sort):
        """Follow the list's sort without sorting again."""
        for widget in (self.combo, self.reverse):
            widget.blockSignals(True)
        self.combo.setCurrentIndex(self.combo.findData(sort.key) if sort.key else -1)
        self.reverse.setChecked(sort.reverse)
        self.reverse.setEnabled(self.combo.currentIndex() >= 0)
        for widget in (self.combo, self.reverse):
            widget.blockSignals(False)


def registry_key(location: str) -> str:
    """'HKCU\\Software\\X|Name' -> 'HKEY_CURRENT_USER\\Software\\X', the form Registry Editor's address bar takes."""
    hive, _, rest = location.split("|", 1)[0].partition("\\")
    return f"{_HIVES.get(hive, hive)}\\{rest}" if rest else _HIVES.get(hive, hive)


def reveal(path: str) -> bool:
    """Explorer with the item selected. It takes /select,"path" as one argument and exits with 1
    even when it worked, so the exit code is not checked."""
    try:
        subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"')
    except OSError:
        log.warning("Could not start Explorer for %s", path)
        return False
    return True


def when_text(ts: float) -> str:
    return _ltr(num(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")))


def counts_text(items) -> str:
    """'3 folders · 2 registry keys', in the order of KIND_COUNTS."""
    parts = []
    for kind, (one, many) in KIND_COUNTS.items():
        n = sum(1 for it in items if it.kind == kind)
        if n:
            parts.append(trn(one, many, n))
    return " · ".join(parts)


def _notice(tone: str = "") -> QFrame:
    frame = QFrame()
    frame.setObjectName("notice")
    frame.setProperty("tone", tone)
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(12, 9, 12, 9)
    frame.label = QLabel()
    frame.label.setWordWrap(True)
    lay.addWidget(frame.label)
    return frame


def _button(text: str, name: str = "", slot=None) -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


def _page() -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    return page, lay


def _bar() -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 0)
    bar.setFixedHeight(4)
    bar.setTextVisible(False)
    return bar


def _dash_glyph(colour: str) -> str:
    """The dash of a partly ticked group, drawn like the theme's check mark (stylesheets take images by path only)."""
    path = theme.DATA_DIR / "theme" / f"dash-{colour.lstrip('#')}.png"
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            pm = QPixmap(32, 32)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(colour), 3.4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(9, 16, 23, 16)
            p.end()
            pm.save(str(path))
    except OSError:
        return ""
    return path.as_posix()


def _indicator_style() -> str:
    """Check boxes of a tree look like the app's other check boxes; the app stylesheet only covers QCheckBox."""
    t = current()
    check, dash = theme._glyphs(t)["check"], _dash_glyph(t["on_accent"])
    return (f"QTreeWidget::indicator {{ width: 16px; height: 16px; border: 1px solid {t['muted']}; border-radius: 4px;"
            f" background: {t['surface']}; }}"
            f"QTreeWidget::indicator:hover {{ border-color: {t['accent']}; }}"
            f"QTreeWidget::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url(\"{check}\"); }}"
            f"QTreeWidget::indicator:indeterminate {{ background: {t['accent']}; border-color: {t['accent']}; "
            f"image: url(\"{dash}\"); }}")


def _problem_tree() -> QTreeWidget:
    """What could not be removed or restored, and why."""
    tree = QTreeWidget()
    tree.setColumnCount(2)
    tree.setHeaderLabels([tr("Item"), tr("Why")])
    tree.setRootIsDecorated(False)
    tree.setIndentation(0)
    tree.setUniformRowHeights(True)
    tree.setSelectionMode(QTreeWidget.NoSelection)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    tree.setItemDelegate(_Delegate(tree))
    tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
    tree.header().setStretchLastSection(True)
    tree.setColumnWidth(0, 250)
    tree.setFixedHeight(150)
    return tree


def _fill_problems(tree: QTreeWidget, rows: list[tuple[str, str, dict]]):
    tree.clear()
    for location, template, values in rows[:_PROBLEM_ROWS]:
        row = QTreeWidgetItem(tree)
        row.setText(0, _ltr(location))
        row.setText(1, render(template, values))
        row.setToolTip(0, _ltr(location))
        row.setToolTip(1, row.text(1))
    if len(rows) > _PROBLEM_ROWS:
        more = QTreeWidgetItem(tree)
        more.setText(0, tr("and {n} more", n=num(len(rows) - _PROBLEM_ROWS)))
        more.setData(0, _ROW_ROLE, "protected")


class _Bus(QObject):
    """Worker-thread results; Qt delivers them on the UI thread. It outlives the dialog on purpose:
    a late result then reaches nobody instead of a deleted widget."""
    progress = Signal(str, object)                # template, values
    finished = Signal(str, str, object, bool)     # outcome, template, values, Windows no longer lists the app
    checked = Signal(bool)
    scan_done = Signal(object, object, object)    # items, the app each belongs to (None for a single app), cleanup.snapshot(items)
    scan_failed = Signal(str)
    clean_progress = Signal(str, int, object)     # template, percent (-1 unknown), values
    clean_done = Signal(object)                   # cleanup.CleanupResult
    undo_progress = Signal(str, int, object)
    undo_done = Signal(object)                    # cleanup.RestoreResult
    app_state = Signal(int, str, str, object)     # batch: row, outcome, template, values
    app_removed = Signal(int)                     # batch: row of an app Windows no longer lists
    batch_done = Signal()


class _Delegate(QStyledItemDelegate):
    """Paths are cut in the middle (the end says what they are), sentences at the end. Group and
    protected rows get their colours here, when they are painted, so a theme change reaches rows
    that are already on screen."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideMiddle if index.column() == 0 else Qt.ElideRight
        role = index.data(_ROW_ROLE)
        t = current()
        if role == "group":
            option.backgroundBrush = QBrush(QColor(t["bg"]))   # muted text keeps 4.5:1 here in every theme
            option.palette.setBrush(QPalette.Text, QColor(t["text"] if index.column() == 0 else t["muted"]))
        elif role == "protected":
            option.palette.setBrush(QPalette.Text, QColor(t["muted"]))


def _restore_work(bus: _Bus, backup_id: str):
    def progress(template, percent=-1, **values):
        bus.undo_progress.emit(template, percent, values)

    try:
        result = cleanup.restore(backup_id, progress)
    except Exception as e:
        log.exception("restore of backup %s failed", backup_id)
        result = cleanup.RestoreResult(failed=[(backup_id, cleanup.R_RESTORE_FAILED, {"error": str(e)})])
    bus.undo_done.emit(result)


def _restore_summary(result: cleanup.RestoreResult) -> str:
    lines = [trn("Restored {n} item", "Restored {n} items", result.restored)]
    if result.failed:
        lines.append(trn("{n} item was left as it is", "{n} items were left as they are", len(result.failed)))
    return "\n".join(lines)


class _FlowDialog(QDialog):
    """What the single and the batch dialog share: pages, the review of leftovers, the confirmation, progress and
    summary of removing them, undo, and the link to the backups."""
    cleanup_finished = Signal(str, object)   # app name (or the batch's label), cleanup.CleanupResult
    restored = Signal(str, object)           # app name, cleanup.RestoreResult after an undo

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings
        self.items: list[leftovers.Leftover] = []
        self.owners: list[str] = []       # the app each item belongs to (batch); '' for a single app
        self.result: cleanup.CleanupResult | None = None
        self.baseline: dict = {}          # cleanup.snapshot() of the review: what its registry items held when it was made
        self._outcome: tuple[str, str, dict] = ("", "", {})   # (outcome, English template, values) of the uninstall
        self.bus = _Bus()
        self._widths: list[int] = []
        self._review_sort = _Sort()
        self._released = False
        self._cleaning = False            # a cleanup or an undo runs on a worker thread
        self._cleanup_token = ""
        self._stop = threading.Event()
        self._backup_id: str | None = None
        self.p_review = self.p_clean_confirm = self.p_cleaning = self.p_summary = -1

        self.setWindowTitle("Upstream")
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        bus = self.bus
        bus.scan_done.connect(self._on_scan_done)
        bus.scan_failed.connect(self._on_scan_failed)
        bus.clean_progress.connect(self._on_clean_progress)
        bus.clean_done.connect(self._on_clean_done)
        bus.undo_progress.connect(self._on_undo_progress)
        bus.undo_done.connect(self._on_undo_done)

    # -- building ------------------------------------------------------
    _page = staticmethod(_page)
    _notice = staticmethod(_notice)
    _button = staticmethod(_button)

    @property
    def cleanup_name(self) -> str:
        """The label the backup and the signals carry."""
        raise NotImplementedError

    @property
    def outcome(self) -> tuple[str, str, dict]:
        """How the uninstall ended: (outcome, English template, values); ('', '', {}) while it has not ended."""
        return self._outcome

    def _add_page(self, page: QWidget, width: int) -> int:
        self._widths.append(width)
        return self.stack.addWidget(page)

    def _add_cleanup_pages(self):
        self.p_review = self._add_page(self._build_review(), 980 if self.batch else 900)
        self.p_clean_confirm = self._add_page(self._build_clean_confirm(), 580)
        self.p_cleaning = self._add_page(self._build_cleaning(), 580)
        self.p_summary = self._add_page(self._build_summary(), 640)

    @property
    def batch(self) -> bool:
        return False

    def _build_review(self) -> QWidget:
        page, lay = self._page()
        self.review_title = QLabel(tr("Leftovers"))
        self.review_title.setObjectName("dlgTitle")
        self.review_sub = QLabel()
        self.review_sub.setWordWrap(True)
        self.review_sub.setProperty("tone", "accent")
        lay.addWidget(self.review_title)
        lay.addWidget(self.review_sub)

        self.review_stack = QStackedWidget()
        lay.addWidget(self.review_stack, 1)
        scanning, sl = self._page()
        sl.addStretch(1)
        sl.addWidget(QLabel(tr("Looking for leftovers…")), 0, Qt.AlignHCenter)
        sl.addWidget(_bar())
        sl.addStretch(2)
        message, ml = self._page()
        ml.addStretch(1)
        self.review_message = QLabel()
        self.review_message.setAlignment(Qt.AlignCenter)
        self.review_message.setWordWrap(True)
        ml.addWidget(self.review_message)
        ml.addStretch(2)
        self.review_stack.addWidget(scanning)
        self.review_stack.addWidget(self._build_results())
        self.review_stack.addWidget(message)

        self.review_note = QLabel(tr("Nothing is removed until you press Remove selected. Everything removed is "
                                     "backed up first, and you can undo it."))
        self.review_note.setObjectName("hint")
        self.review_note.setWordWrap(True)
        lay.addWidget(self.review_note)
        buttons = QHBoxLayout()
        self.open_btn = self._button(tr("Open location"), "", self._open_location)
        self.export_btn = self._button(tr("Export list…"), "", self._export)
        self.backups_btn = self._button(tr("Backups…"), "link", self._open_backups)
        self.remove_btn = self._button(tr("Remove selected…"), "danger", self._ask_remove)
        self.remove_btn.setAutoDefault(False)
        done = self._button(tr("Done"), "", self.accept)
        done.setAutoDefault(False)
        for b in (self.open_btn, self.export_btn, self.backups_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.remove_btn)
        buttons.addWidget(done)
        lay.addLayout(buttons)
        return page

    def _build_results(self) -> QWidget:
        page, lay = self._page()
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setObjectName("hint")
        self.pick_line = QLabel()
        self.pick_line.setProperty("tone", "accent")
        names = {"location": tr("Item"), "kind": tr("Type"), "size": tr("Space used"), "owner": tr("From app")}
        self.sort_bar = _SortBar([(k, names[k]) for k in _REVIEW_KEYS if k and (k != "owner" or self.batch)])
        self.sort_bar.changed.connect(self._sort_review_by)
        top.addWidget(self.summary)
        top.addStretch(1)
        top.addWidget(self.sort_bar)
        top.addStretch(1)
        top.addWidget(self.pick_line)
        lay.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([tr("Item"), tr("Type"), tr("Space used"), tr("From app"), tr("Why")])
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollPerPixel)
        self.tree.setItemDelegate(_Delegate(self.tree))
        self.tree.setStyleSheet(_indicator_style())
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(3, 130)
        self.tree.setColumnHidden(3, not self.batch)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.itemChanged.connect(self._on_tick)
        _sortable(self.tree, _REVIEW_KEYS, self._sort_review)
        lay.addWidget(self.tree, 1)

        self.detail_path = QLineEdit()
        self.detail_path.setReadOnly(True)
        self.detail_path.setLayoutDirection(Qt.LeftToRight)
        self.detail_text = QLabel()
        self.detail_text.setWordWrap(True)
        self.detail_text.setAlignment(Qt.AlignTop)
        self.detail_text.setMinimumHeight(self.detail_text.fontMetrics().height() * 3 + 6)
        self.detail_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_line = QLabel()
        self.status_line.setProperty("tone", "accent")
        self.status_line.setWordWrap(True)
        for w in (self.detail_path, self.detail_text, self.status_line):
            lay.addWidget(w)
        return page

    def _build_clean_confirm(self) -> QWidget:
        page, lay = self._page()
        self.cc_title = QLabel()
        self.cc_title.setObjectName("dlgTitle")
        self.cc_title.setWordWrap(True)
        self.cc_counts = QLabel()
        self.cc_counts.setWordWrap(True)
        self.cc_size = QLabel()
        self.cc_size.setObjectName("hint")
        self.cc_size.setWordWrap(True)
        self.cc_backup_note = self._notice()
        self.cc_admin_note = self._notice("danger")
        self.cc_service_note = self._notice("danger")
        for w in (self.cc_title, self.cc_counts, self.cc_size, self.cc_backup_note, self.cc_service_note, self.cc_admin_note):
            lay.addWidget(w)
        lay.addStretch(1)
        lay.addSpacing(8)
        buttons = QHBoxLayout()
        self.cc_go = self._button(tr("Remove and back up"), "danger", self._start_cleanup)
        self.cc_go.setAutoDefault(False)   # Enter must not start a removal
        self.cc_back = self._button(tr("Back to the list"), "", lambda: self._show(self.p_review))
        buttons.addWidget(self.cc_go)
        buttons.addStretch(1)
        buttons.addWidget(self.cc_back)
        lay.addLayout(buttons)
        return page

    def _build_cleaning(self) -> QWidget:
        page, lay = self._page()
        title = QLabel(tr("Removing leftovers"))
        title.setObjectName("dlgTitle")
        self.cl_step = QLabel(tr("Starting…"))
        self.cl_step.setWordWrap(True)
        self.cl_bar = QProgressBar()
        self.cl_bar.setRange(0, 100)
        self.cl_bar.setFixedHeight(4)
        self.cl_bar.setTextVisible(False)
        hint = QLabel(tr("Each item is backed up before it is removed."))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        for w in (title, self.cl_step, self.cl_bar, hint):
            lay.addWidget(w)
        lay.addStretch(1)
        buttons = QHBoxLayout()
        self.cl_stop = self._button(tr("Stop after this item"), "", self._stop_cleanup)
        buttons.addStretch(1)
        buttons.addWidget(self.cl_stop)
        lay.addLayout(buttons)
        return page

    def _build_summary(self) -> QWidget:
        page, lay = self._page()
        self.sum_title = QLabel()
        self.sum_title.setObjectName("dlgTitle")
        self.sum_text = QLabel()
        self.sum_text.setWordWrap(True)
        self.sum_extra = QLabel()
        self.sum_extra.setObjectName("hint")
        self.sum_extra.setWordWrap(True)
        self.sum_problems_title = QLabel()
        self.sum_problems_title.setObjectName("section")
        self.sum_problems = _problem_tree()
        self.sum_note = QLabel()
        self.sum_note.setObjectName("hint")
        self.sum_note.setWordWrap(True)
        self.sum_bar = _bar()
        for w in (self.sum_title, self.sum_text, self.sum_extra, self.sum_problems_title, self.sum_problems, self.sum_note,
                  self.sum_bar):
            lay.addWidget(w)
        lay.addStretch(1)
        lay.addSpacing(8)
        buttons = QHBoxLayout()
        self.undo_btn = self._button(tr("Undo cleanup"), "", self._undo)
        self.sum_backups_btn = self._button(tr("Backups…"), "link", self._open_backups)
        self.sum_done = self._button(tr("Done"), "primary", self.accept)
        buttons.addWidget(self.undo_btn)
        buttons.addWidget(self.sum_backups_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.sum_done)
        lay.addLayout(buttons)
        return page

    # -- pages ---------------------------------------------------------
    def _show(self, index: int):
        for i in range(self.stack.count()):
            policy = QSizePolicy.Preferred if i == index else QSizePolicy.Ignored   # only the shown page sizes the dialog
            self.stack.widget(i).setSizePolicy(policy, policy)
        self.stack.setCurrentIndex(index)
        self.setMinimumSize(0, 0)
        self.setMinimumWidth(self._widths[index])
        if index == self.p_clean_confirm:
            self.cc_back.setDefault(True)   # Enter must not start a removal; this only works once the button has a dialog
        self._on_page(index)
        self._fit()

    def _on_page(self, index: int):
        """For subclasses: what to start or stop when a page appears."""

    def _fit(self):
        """Size the dialog to the shown page (the stack would otherwise keep the tallest page's height)."""
        index = self.stack.currentIndex()
        width = self._widths[index]
        if index == self.p_review:
            self.resize(max(self.width(), width), max(self.height(), REVIEW_HEIGHT))
            return
        margins = self.layout().contentsMargins()
        page = self.stack.currentWidget().layout()
        inner = width - margins.left() - margins.right()
        height = page.totalHeightForWidth(inner) if page.hasHeightForWidth() else page.sizeHint().height()
        self.resize(width, height + margins.top() + margins.bottom())

    # -- review --------------------------------------------------------
    def _scan_job(self) -> Callable[[], tuple[list, list | None]]:
        """A function for the worker thread: the leftovers, and the app each belongs to (None: one app)."""
        raise NotImplementedError

    def _begin_scan(self, removed_text: str):
        self.review_sub.setText(removed_text)
        self.review_stack.setCurrentIndex(0)
        self.open_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self._show(self.p_review)

        def work(bus: _Bus, job):
            try:
                items, owners = job()
                baseline = cleanup.snapshot(items)
            except Exception as e:
                log.exception("leftover scan failed")
                bus.scan_failed.emit(str(e))
                return
            bus.scan_done.emit(items, owners, baseline)

        threading.Thread(target=work, args=(self.bus, self._scan_job()), daemon=True).start()

    def _on_scan_failed(self, error: str):
        self.review_message.setText(tr("Looking for leftovers failed: {error}", error=error))
        self.review_stack.setCurrentIndex(2)

    def _on_scan_done(self, items: list, owners: list | None = None, baseline: dict | None = None):
        self.items = items
        self.owners = list(owners) if owners else [""] * len(items)
        self.baseline = dict(baseline or {})
        if not items:
            self.review_message.setText(tr("Upstream found nothing that was left behind."))
            self.review_stack.setCurrentIndex(2)
            return
        self.summary.setText(trn("{n} item found", "{n} items found", len(items)))
        self._fill_tree()
        self.export_btn.setEnabled(True)
        self.review_stack.setCurrentIndex(1)
        self._on_select()

    @staticmethod
    def preticked(item: leftovers.Leftover) -> bool:
        """Only what is certain and not huge starts ticked; the user ticks the rest."""
        return item.confidence == leftovers.CERTAIN and not item.large

    def _review_values(self) -> dict:
        """What each sortable column of the review sorts on, for a (position in self.items, leftover) pair."""
        return {"location": lambda r: r[1].location.casefold(),
                "kind": lambda r: tr(KIND_LABELS.get(r[1].kind, r[1].kind)).casefold(),
                "size": lambda r: r[1].size if r[1].size is not None and r[1].kind in _PATH_KINDS else None,
                "owner": lambda r: self.owners[r[0]].casefold() or None}

    def _fill_tree(self, ticked: set[int] | None = None):
        """The review, group by group. Positions in `ticked` start ticked (the certain, not huge ones when None)."""
        self.tree.blockSignals(True)
        self.tree.clear()
        bold = self.tree.font()
        bold.setBold(True)
        for confidence, title, blurb in GROUPS:
            rows = [(i, it) for i, it in enumerate(self.items) if it.confidence == confidence]
            if not rows:
                continue
            rows = self._review_sort.apply(rows, self._review_values())
            head = QTreeWidgetItem(self.tree)
            head.setText(0, f"{tr(title)}  {num(len(rows))}")
            head.setText(4, tr(blurb))
            flags = Qt.ItemIsEnabled
            if confidence in _TICKABLE_GROUPS:
                flags |= Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate
            head.setFlags(flags)
            for col in range(5):
                head.setData(col, _ROW_ROLE, "group")
            head.setFont(0, bold)
            for index, it in rows:
                row = QTreeWidgetItem(head)
                row.setText(0, _ltr(it.location))
                row.setText(1, tr(KIND_LABELS.get(it.kind, it.kind)))
                row.setText(2, _ltr(size_text(it.size)) if it.size is not None and it.kind in _PATH_KINDS else "")
                row.setText(3, isolate(self.owners[index]))
                row.setText(4, render(it.reason, it.reason_values))
                row.setData(0, Qt.UserRole, index)
                row.setToolTip(0, _ltr(it.location))
                row.setToolTip(4, row.text(4))
                if confidence == leftovers.PROTECTED:
                    row.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)   # no box: these can never be removed
                    for col in range(5):
                        row.setData(col, _ROW_ROLE, "protected")
                else:
                    row.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    row.setCheckState(0, Qt.Checked if (self.preticked(it) if ticked is None else index in ticked)
                                      else Qt.Unchecked)
            if confidence in _TICKABLE_GROUPS:
                head.setCheckState(0, self._group_state(head))
        self.tree.expandAll()
        self.tree.blockSignals(False)
        _show_sort(self.tree, _REVIEW_KEYS, self._review_sort)
        self.sort_bar.show_sort(self._review_sort)
        self._update_pick()

    def _sort_review(self, key: str):
        """A header was clicked."""
        self._review_sort.click(key)
        self._resort_review()

    def _sort_review_by(self, key: str, reverse: bool):
        """The Sort by box was used."""
        self._review_sort.set(key, reverse)
        self._resort_review()

    def _resort_review(self):
        """The rows of each group are put in the new order; what is ticked and selected stays."""
        ticked = {row.data(0, Qt.UserRole) for row in self._ticked_rows()}
        chosen = [row.data(0, Qt.UserRole) for row in self.tree.selectedItems()]
        self._fill_tree(ticked)
        for g in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(g)
            for i in range(head.childCount()):
                if chosen and head.child(i).data(0, Qt.UserRole) == chosen[0]:
                    self.tree.setCurrentItem(head.child(i))
        self._on_select()

    @staticmethod
    def _group_state(head: QTreeWidgetItem):
        states = {head.child(i).checkState(0) for i in range(head.childCount())}
        return Qt.Checked if states == {Qt.Checked} else Qt.Unchecked if states == {Qt.Unchecked} else Qt.PartiallyChecked

    def _on_tick(self, *_):
        self._update_pick()

    def _ticked_rows(self):
        for g in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(g)
            for i in range(head.childCount()):
                row = head.child(i)
                if row.flags() & Qt.ItemIsUserCheckable and row.checkState(0) == Qt.Checked:
                    yield row

    def ticked(self) -> list[leftovers.Leftover]:
        """The leftovers the user has ticked (protected ones have no box)."""
        return [self.items[row.data(0, Qt.UserRole)] for row in self._ticked_rows()]

    def _update_pick(self):
        picked = self.ticked()
        size = sum(it.size or 0 for it in picked if it.kind in _PATH_KINDS)
        self.pick_line.setText(tr("{n} ticked · {size}", n=num(len(picked)), size=size_text(size)) if size
                               else tr("{n} ticked", n=num(len(picked))) if picked else tr("Nothing ticked"))
        self.remove_btn.setEnabled(bool(picked) and not self._cleaning)

    def selected(self) -> leftovers.Leftover | None:
        picked = self.tree.selectedItems()
        index = picked[0].data(0, Qt.UserRole) if picked else None
        return self.items[index] if index is not None else None

    def _on_select(self):
        it = self.selected()
        self.status_line.clear()
        self.open_btn.setEnabled(it is not None)
        self.detail_path.setEnabled(it is not None)
        if it is None:
            self.detail_path.clear()
            self.detail_text.setText(tr("Select a row to see its full location and why it was found."))
            return
        self.detail_path.setText(it.location)
        self.detail_path.setCursorPosition(0)
        lines = [render(it.reason, it.reason_values)]
        if it.caveat:
            lines.append(tr(it.caveat))
        if it.kind in _PATH_KINDS:
            lines.append(tr("Size: {size}", size=size_text(it.size)) if it.size is not None
                         else tr("Size not measured"))
        self.detail_text.setText("\n".join(lines))

    def _open_location(self):
        it = self.selected()
        if it is None:
            return
        if it.kind in _PATH_KINDS:
            parent = os.path.dirname(it.location)
            if os.path.exists(it.location):
                reveal(it.location)
            elif os.path.isdir(parent):
                os.startfile(parent)
            else:
                self.status_line.setText(tr("That location no longer exists."))
        else:
            QGuiApplication.clipboard().setText(registry_key(it.location))
            self.status_line.setText(tr("Copied the registry key. Paste it into the address bar of Registry Editor."))

    def export_text(self) -> str:
        lines = [tr("Leftovers of {name}", name=self.cleanup_name), tr("Made by Upstream on {date}",
                 date=datetime.now().strftime("%Y-%m-%d %H:%M")), ""]
        for confidence, title, _ in GROUPS:
            rows = [(it, self.owners[i]) for i, it in enumerate(self.items) if it.confidence == confidence]
            if rows:
                lines += [f"{tr(title)} ({num(len(rows))})", ""]
            for it, owner in rows:
                lines.append(f"[{tr(KIND_LABELS.get(it.kind, it.kind))}] {it.location}")
                lines.append(f"    {render(it.reason, it.reason_values)}")
                if it.caveat:
                    lines.append(f"    {tr(it.caveat)}")
                if it.kind in _PATH_KINDS and it.size is not None:
                    lines.append("    " + tr("Size: {size}", size=size_text(it.size)))
                if owner:
                    lines.append("    " + tr("From {app}", app=owner))
                lines.append("")
        return "\n".join(lines)

    def _export(self):
        safe = "".join(c for c in self.cleanup_name if c not in '<>:"/\\|?*').strip() or "app"
        path, _ = QFileDialog.getSaveFileName(self, tr("Export list"), f"{safe} leftovers.txt",
                                              tr("Text files (*.txt)"))
        if not path:
            return
        try:
            Path(path).write_text(self.export_text(), "utf-8-sig")   # the BOM lets Notepad read Persian
        except OSError as e:
            self.status_line.setText(tr("Couldn't save the list: {error}", error=e.strerror or str(e)))
            return
        log.info("Leftover list of %s saved to %s", self.cleanup_name, path)
        self.status_line.setText(tr("Saved to {path}", path=path))

    # -- removing ------------------------------------------------------
    def _backup_days(self) -> int:
        return int(self.settings.get("cleanup_backup_days", 30)) if self.settings is not None else 30

    def _ask_remove(self):
        picked = self.ticked()
        if not picked or self._cleaning:
            return
        size = sum(it.size or 0 for it in picked if it.kind in _PATH_KINDS)
        self.cc_title.setText(trn("Remove {n} item?", "Remove {n} items?", len(picked)))
        self.cc_counts.setText(counts_text(picked))
        self.cc_size.setText(tr("About {size} of files and folders", size=size_text(size)) if size else "")
        self.cc_size.setVisible(bool(size))
        days = self._backup_days()
        self.cc_backup_note.label.setText(
            tr("Each item is backed up first and kept for {days} days. You can undo this right after, or later from Backups.",
               days=num(days)) if days > 0 else
            tr("Each item is backed up first. You can undo this right after, or later from Backups."))
        admin = 0 if cleanup.is_admin() else sum(1 for it in picked if cleanup.needs_admin(it))
        self.cc_admin_note.label.setText(
            trn("{n} item may need administrator rights. Upstream never gives itself administrator rights, so if it "
                "can't remove it, it is left alone. Start Upstream as administrator to remove it.",
                "{n} items may need administrator rights. Upstream never gives itself administrator rights, so if it "
                "can't remove them, they are left alone. Start Upstream as administrator to remove them.", admin))
        self.cc_admin_note.setVisible(bool(admin))
        service = any(it.kind == "service" for it in picked)
        self.cc_service_note.label.setText(
            tr("Undo can't bring a removed service back. To get one back, reinstall the app.") if service else "")
        self.cc_service_note.setVisible(service)
        self._show(self.p_clean_confirm)

    def _start_cleanup(self):
        picked = self.ticked()
        if not picked or self._cleaning:
            return
        self._begin_work()
        self._stop = threading.Event()
        self.cl_step.setText(tr("Starting…"))
        self.cl_bar.setRange(0, 0)
        self.cl_stop.setEnabled(True)
        log.info("%s: removing %d ticked leftovers", self.cleanup_name, len(picked))
        self._show(self.p_cleaning)
        point = None if self.settings is None else bool(self.settings.get("cleanup_restore_point", False))
        threading.Thread(target=self._clean_work, args=(self.bus, picked, self.cleanup_name, self._stop, point, self.baseline),
                         daemon=True).start()

    @staticmethod
    def _clean_work(bus: _Bus, items, name: str, stop: threading.Event, point: bool | None, baseline: dict):
        def progress(template, percent=-1, **values):
            bus.clean_progress.emit(template, percent, values)

        try:
            result = cleanup.run(items, name, progress, stop, restore_point=point, baseline=baseline)
        except Exception as e:
            log.exception("cleanup of %s failed", name)
            result = cleanup.CleanupResult(failed=[(it, "{error}", {"error": str(e)}) for it in items])
        bus.clean_done.emit(result)

    def _begin_work(self):
        self._cleaning = True
        self._cleanup_token = f"cleanup:{id(self)}"
        _RUNNING.add(self._cleanup_token)
        self._set_backups_enabled(False)

    def _end_work(self):
        self._cleaning = False
        _RUNNING.discard(self._cleanup_token)
        self._set_backups_enabled(True)

    def _set_backups_enabled(self, enabled: bool):
        """The Backups dialog restores and deletes: it stays closed while this dialog's cleanup or undo runs."""
        for button in (self.backups_btn, self.sum_backups_btn):
            button.setEnabled(enabled)

    def _stop_cleanup(self):
        self._stop.set()
        self.cl_stop.setEnabled(False)
        self.cl_step.setText(tr("Stopping after this item…"))

    def _on_clean_progress(self, template: str, percent: int, values: dict):
        self.cl_step.setText(render(template, values))
        if percent < 0:
            self.cl_bar.setRange(0, 0)
        else:
            self.cl_bar.setRange(0, 100)
            self.cl_bar.setValue(percent)

    def _on_clean_done(self, result: cleanup.CleanupResult):
        self._end_work()
        self.result = result
        self._backup_id = result.backup_id
        self.cleanup_finished.emit(self.cleanup_name, result)
        if self._released:
            self.deleteLater()
            return
        self._fill_summary(result)
        self._show(self.p_summary)

    def _fill_summary(self, result: cleanup.CleanupResult):
        if result.cancelled:
            title, tone = tr("Cleanup stopped"), ""
        elif result.removed == 0 and result.failed:
            title, tone = tr("Nothing was removed"), "danger"
        else:
            title, tone = tr("Leftovers removed"), "accent"
        self.sum_title.setText(title)
        self.sum_title.setProperty("tone", tone)
        theme.repolish(self.sum_title)
        lines = [trn("Removed {n} item", "Removed {n} items", result.removed)]
        if result.freed:
            lines.append(tr("{size} moved into the backup", size=size_text(result.freed)))
        if result.already_gone:
            lines.append(trn("{n} item was already gone", "{n} items were already gone", result.already_gone))
        extra = []
        if result.restore_point is True:
            extra.append(tr("A Windows restore point was created."))
        elif result.restore_point is False:
            extra.append(tr("Windows did not create a restore point. That needs administrator rights and System "
                            "Protection turned on, and Windows allows only one a day. Your backup still works."))
        if result.restart_needed:
            extra.append(tr("Restart Windows to finish removing a service."))
        self.sum_text.setText("\n".join(lines))
        self.sum_extra.setText("\n".join(extra))
        self.sum_extra.setVisible(bool(extra))
        failed = [(it.location, template, values) for it, template, values in result.failed]
        self.sum_problems_title.setText(trn("{n} item was not removed", "{n} items were not removed", len(failed)))
        self.sum_problems_title.setVisible(bool(failed))
        self.sum_problems.setVisible(bool(failed))
        _fill_problems(self.sum_problems, failed)
        self.sum_note.setText(tr("Everything removed was backed up first. Undo cleanup puts it all back.")
                              if result.backup_id else "")
        self.sum_note.setVisible(bool(result.backup_id))
        self.sum_bar.setVisible(False)
        self.undo_btn.setVisible(bool(result.backup_id))
        self.undo_btn.setEnabled(True)
        self.sum_done.setEnabled(True)

    # -- undo ----------------------------------------------------------
    def _undo(self):
        if self._cleaning or not self._backup_id:
            return
        self._begin_work()
        self.undo_btn.setEnabled(False)
        self.sum_done.setEnabled(False)
        self.sum_bar.setRange(0, 0)
        self.sum_bar.setVisible(True)
        self.sum_note.setText(tr("Restoring…"))
        self.sum_note.setVisible(True)
        threading.Thread(target=_restore_work, args=(self.bus, self._backup_id), daemon=True).start()

    def _on_undo_progress(self, template: str, percent: int, values: dict):
        self.sum_note.setText(render(template, values))
        if percent >= 0:
            self.sum_bar.setRange(0, 100)
            self.sum_bar.setValue(percent)

    def _on_undo_done(self, result: cleanup.RestoreResult):
        self._end_work()
        self.restored.emit(self.cleanup_name, result)
        if self._released:
            self.deleteLater()
            return
        self.sum_title.setText(tr("Cleanup undone") if result.restored else tr("Nothing was restored"))
        self.sum_title.setProperty("tone", "accent" if result.restored else "danger")
        theme.repolish(self.sum_title)
        self.sum_text.setText(_restore_summary(result))
        self.sum_extra.hide()
        self.sum_problems_title.setText(tr("Left as it is"))
        self.sum_problems_title.setVisible(bool(result.failed))
        self.sum_problems.setVisible(bool(result.failed))
        _fill_problems(self.sum_problems, result.failed)
        self.sum_note.setText(tr("Nothing that exists now was overwritten.") if result.failed else "")
        self.sum_note.setVisible(bool(result.failed))
        self.sum_bar.setVisible(False)
        self.undo_btn.setVisible(bool(result.remaining))
        self.undo_btn.setEnabled(True)
        self.sum_done.setEnabled(True)
        self._backup_id = self._backup_id if result.remaining else None
        self._fit()

    def _open_backups(self):
        if self._cleaning:
            return
        dialog = BackupsDialog(self, settings=self.settings)
        dialog.restored.connect(self.restored)
        dialog.exec()

    # -- closing -------------------------------------------------------
    def reject(self):
        if self._cleaning:   # the worker can't be called back; the Stop button ends a cleanup between two items
            return
        super().reject()


class UninstallDialog(_FlowDialog):
    removed = Signal(str)   # uid, as soon as Windows no longer lists the app
    ended = Signal()        # the uninstaller stopped, whatever came of it

    def __init__(self, status: AppStatus, others: list[InstalledApp], parent=None,
                 updates_running: Callable[[], bool] | bool = False, icon: QPixmap | None = None, settings=None):
        super().__init__(parent, settings)
        self.status, self.app, self.uid = status, status.app, status.uid
        self.others = list(others)
        self._updates_running = updates_running if callable(updates_running) else (lambda: updates_running)
        self.was_removed = False
        self.plans = {quiet: uninstaller.plan(self.app, status.winget_id, prefer_silent=quiet) for quiet in (True, False)}
        self.plan = self.plans[True]
        self._busy = False        # the uninstaller is running; the worker thread cannot be stopped
        self._checking = False    # "Check again" is waiting for Windows; its answer must still reach the window
        self._want_scan = False
        self._silent_used = False
        self._wait_shown = ""     # the reason Uninstall is held back, as the confirm page last showed it
        self._icon = icon if icon is not None and not icon.isNull() else letter_avatar(self.app.name)
        self._poll = QTimer(self)   # updates and other uninstalls end while the confirm page is open
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_wait)

        self.setWindowTitle(tr("Uninstall — {name}", name=self.app.name))
        for page, width in ((self._build_confirm(), 580), (self._build_running(), 580), (self._build_result(), 580)):
            self._add_page(page, width)
        self._add_cleanup_pages()

        self.bus.progress.connect(self._on_progress)
        self.bus.finished.connect(self._on_finished)
        self.bus.checked.connect(self._on_checked)
        self.cancel_btn.setDefault(True)   # only possible once the button belongs to the dialog
        self._show(CONFIRM)

    @property
    def cleanup_name(self) -> str:
        return f"{self.app.name} {self.app.version}".strip()

    def _scan_job(self):
        app, others = self.app, list(self.others)
        return lambda: (leftovers.scan(app, others), None)

    # -- building ------------------------------------------------------
    def _build_confirm(self) -> QWidget:
        page, lay = self._page()
        head = QHBoxLayout()
        head.setSpacing(14)
        icon = QLabel()
        icon.setFixedSize(36, 36)
        icon.setPixmap(self._icon)
        names = QVBoxLayout()
        names.setSpacing(2)
        meta = [tr("Version {version}", version=self.app.version)] if self.app.version else []
        if self.app.size_kb > 0:
            meta.append(app_size_text(self.app))
        meta.append(isolate(self.app.publisher or tr("Unknown publisher")))   # keeps the full stop of "Figma, Inc." in an RTL line
        title = ElidedLabel(self.app.name, "dlgTitle")
        title.ensurePolished()   # its font comes from the stylesheet, and the size hint needs it
        names.addWidget(title)
        names.addWidget(ElidedLabel(" · ".join(meta), "meta"))
        head.addWidget(icon, 0, Qt.AlignTop)
        head.addLayout(names, 1)
        lay.addLayout(head)
        lay.addSpacing(8)

        self.blocked_notice = self._notice("danger")
        self.wait_notice = self._notice()
        self.how_title = QLabel(tr("How it will be removed"))
        self.how_title.setObjectName("section")
        self.how = QLabel()
        self.how.setWordWrap(True)
        self.note_notices = []
        for template, values in self.plan.notes:
            note = self._notice()
            note.label.setText(render(template, values))
            self.note_notices.append(note)
        self.ack_box = QCheckBox(tr("I understand that other apps or devices may stop working"))
        self.ack_box.toggled.connect(self._refresh_confirm)
        self.quiet_box = QCheckBox(tr("Run quietly when possible"))
        self.quiet_box.setChecked(True)
        self.quiet_box.toggled.connect(self._refresh_confirm)
        self.leftover_box = QCheckBox(tr("Look for leftovers afterwards"))
        self.leftover_box.setChecked(True)
        self.leftover_hint = QLabel(tr("The review lists what was left behind. Nothing is removed until you choose "
                                       "it, and everything removed is backed up first."))
        self.leftover_hint.setObjectName("hint")
        self.leftover_hint.setWordWrap(True)

        for w in (self.blocked_notice, self.wait_notice, self.how_title, self.how, *self.note_notices, self.ack_box):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(self.quiet_box)
        lay.addWidget(self.leftover_box)
        lay.addWidget(self.leftover_hint)
        lay.addStretch(1)
        lay.addSpacing(8)

        buttons = QHBoxLayout()
        self.go_btn = self._button(tr("Uninstall"), "danger", self._start)
        self.cancel_btn = self._button(tr("Cancel"), "", self.reject)
        self.go_btn.setAutoDefault(False)   # Enter must not start a removal
        buttons.addWidget(self.go_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        lay.addLayout(buttons)
        return page

    def _build_running(self) -> QWidget:
        page, lay = self._page()
        self.run_title = QLabel(tr("Uninstalling {name}", name=self.app.name))
        self.run_title.setObjectName("dlgTitle")
        self.run_title.setWordWrap(True)
        self.step = QLabel(tr("Starting…"))
        self.step.setWordWrap(True)
        self.run_hint = QLabel()
        self.run_hint.setObjectName("hint")
        self.run_hint.setWordWrap(True)
        for w in (self.run_title, self.step, _bar(), self.run_hint):
            lay.addWidget(w)
        lay.addStretch(1)
        return page

    def _build_result(self) -> QWidget:
        page, lay = self._page()
        self.result_title = QLabel()
        self.result_title.setObjectName("dlgTitle")
        self.result_text = QLabel()
        self.result_text.setWordWrap(True)
        self.result_hint = QLabel()
        self.result_hint.setObjectName("hint")
        self.result_hint.setWordWrap(True)
        for w in (self.result_title, self.result_text, self.result_hint):
            lay.addWidget(w)
        lay.addStretch(1)
        lay.addSpacing(8)
        buttons = QHBoxLayout()
        self.check_btn = self._button(tr("Check again"), "", self._check_again)
        self.retry_btn = self._button(tr("Retry"), "", lambda: self._show(CONFIRM))
        self.window_btn = self._button(tr("Try with its own window"), "", self._try_own_window)
        self.result_close = self._button("", "primary", self.accept)
        for b in (self.check_btn, self.retry_btn, self.window_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.result_close)
        lay.addLayout(buttons)
        return page

    # -- pages ---------------------------------------------------------
    def _on_page(self, index: int):
        if index == CONFIRM:
            self._refresh_confirm()
            self._poll.start()
        else:
            self._poll.stop()

    def _method_text(self, plan: uninstaller.UninstallPlan) -> str:
        if plan.method == "msi":
            text = tr("Windows Installer removes it and shows a small progress window.") if plan.silent                 else tr("Windows Installer removes it and may show its own windows.")
        elif plan.method == "winget":
            text = tr("winget removes it quietly.")
        elif plan.silent:
            text = tr("The app's own uninstaller removes it quietly.")
        else:
            text = tr("Opens {name}'s own uninstall window. Follow the steps there.", name=self.app.name)
        if self.app.scope == "machine":
            text += " " + tr("Windows may ask for permission.")
        return text

    def _refresh_confirm(self, *_):
        """Re-plan for the 'run quietly' choice and show what that means."""
        silent, vendor = self.plans[True], self.plans[False]
        self.plan = silent if self.quiet_box.isChecked() else vendor
        plan = self.plan
        same = (silent.method, silent.file, silent.params, silent.blocked) == \
               (vendor.method, vendor.file, vendor.params, vendor.blocked)
        self.quiet_box.setEnabled(not same and not (silent.blocked and vendor.blocked))
        self.quiet_box.setToolTip(tr("This app has only one way to be removed.") if same else "")

        wait = self._wait_shown = self._wait_reason()
        self.wait_notice.label.setText(wait)
        self.wait_notice.setVisible(bool(wait))

        blocked = bool(plan.blocked)
        self.blocked_notice.label.setText(render(plan.blocked, plan.blocked_values) if blocked else "")
        self.blocked_notice.setVisible(blocked)
        for w in (self.how_title, self.how, self.quiet_box, self.leftover_box, self.leftover_hint, *self.note_notices):
            w.setVisible(not blocked)
        self.ack_box.setVisible(not blocked and uninstaller.needs_ack(plan))
        if not blocked:
            self.how.setText(self._method_text(plan))
            self.how.setToolTip(_ltr(f"{plan.file} {plan.params}".strip()))
        self.go_btn.setVisible(not blocked)
        self.go_btn.setEnabled(not wait and not self._needs_ack())
        self.cancel_btn.setText(tr("Close") if blocked else tr("Cancel"))
        if self.stack.currentIndex() == CONFIRM:
            self._fit()

    @property
    def updates_running(self) -> bool:
        return bool(self._updates_running())

    def _wait_reason(self) -> str:
        """Why Uninstall can't start yet, or ''. Asked every time: updates and other uninstalls end
        while this window is open."""
        if _RUNNING:
            return tr("Another uninstall is still running. Wait until it finishes.")
        if self.updates_running:
            return tr("Updates are running right now. Removing an app at the same time could make both fail, "
                      "so wait until they finish.")
        return ""

    def _needs_ack(self) -> bool:
        return uninstaller.needs_ack(self.plan) and not self.ack_box.isChecked()

    def _poll_wait(self):
        if self._wait_reason() != self._wait_shown:
            self._refresh_confirm()

    # -- running -------------------------------------------------------
    def _start(self):
        if self._busy or self.plan.blocked or self._wait_reason() or self._needs_ack():
            return
        plan = self.plan
        self._busy, self._silent_used = True, plan.silent
        self._want_scan = self.leftover_box.isChecked()
        _RUNNING.add(self.uid)
        log.info("%s: uninstall confirmed (%s)", self.app.name, plan.method)
        self.step.setText(tr("Starting…"))
        self.run_hint.setText(tr("Finish the steps in the uninstaller's own window. Windows may ask for permission first.")
                              if not plan.silent else tr("This can take a minute. Windows may ask for permission."))
        self._show(RUNNING)
        threading.Thread(target=self._work, args=(self.bus, plan), daemon=True).start()

    def _work(self, bus: _Bus, plan: uninstaller.UninstallPlan):
        try:
            outcome, template, values = uninstaller.run(plan, lambda t, **v: bus.progress.emit(t, v))
        except Exception as e:
            log.exception("uninstall failed")
            outcome, template, values = "failed", "{error}", {"error": str(e)}
        gone = outcome == "removed"
        if outcome == "reboot_required":   # Windows may already have dropped the entry
            try:
                gone = uninstaller.is_gone(plan.app, self.status.winget_id)
            except Exception:
                log.exception("could not check %s after the uninstall", plan.app.name)
        bus.finished.emit(outcome, template, values, gone)

    def _on_progress(self, template: str, values: dict):
        self.step.setText(render(template, values))

    def _on_finished(self, outcome: str, template: str, values: dict, gone: bool):
        self._busy = False
        _RUNNING.discard(self.uid)
        self.ended.emit()
        log.info("%s: %s", self.app.name, english(template, values))
        if gone and not self.was_removed:
            self.was_removed = True
            self.removed.emit(self.uid)
        if self._released:   # the window was closed while it ran; nothing left to show
            self.deleteLater()
            return
        self._outcome = (outcome, template, values)
        if outcome == "removed" and self._want_scan:
            self._begin_scan(render(template, values))
        else:
            self._show_result(outcome, template, values)

    def _try_own_window(self):
        self.quiet_box.setChecked(False)
        self._show(CONFIRM)

    # -- result --------------------------------------------------------
    def _show_result(self, outcome: str, template: str, values: dict, hint: str = ""):
        title, tone = RESULT_TITLES.get(outcome, RESULT_TITLES["failed"])
        self.result_title.setText(tr(title))
        self.result_title.setProperty("tone", tone)
        theme.repolish(self.result_title)
        self.result_text.setText(render(template, values))
        if not hint and outcome == "still_listed":
            hint = tr("The uninstaller may still be working, or it finished without removing the app.")
        self.result_hint.setText(hint)
        self.result_hint.setVisible(bool(hint))
        done = outcome in ("removed", "reboot_required")
        own_window = self._silent_used and not self.plans[False].blocked and not done and outcome != "busy" \
            and (self.plans[False].method, self.plans[False].params) != (self.plans[True].method, self.plans[True].params)
        self.check_btn.setVisible(outcome == "still_listed")
        self.check_btn.setEnabled(True)
        self.check_btn.setText(tr("Check again"))
        self.retry_btn.setVisible(not done)
        self.window_btn.setVisible(own_window)
        self.result_close.setText(tr("Done") if done else tr("Close"))
        self.result_close.setObjectName("primary" if done else "")
        theme.repolish(self.result_close)
        self._show(RESULT)

    def _check_again(self):
        self._checking = True
        self.check_btn.setEnabled(False)
        self.check_btn.setText(tr("Checking…"))

        def work(bus: _Bus, app: InstalledApp, winget_id: str | None):
            try:
                gone = uninstaller.is_gone(app, winget_id)
            except Exception:
                log.exception("could not check %s", app.name)
                gone = False
            bus.checked.emit(gone)

        threading.Thread(target=work, args=(self.bus, self.app, self.status.winget_id), daemon=True).start()

    def _on_checked(self, gone: bool):
        self._checking = False
        if self._released:   # the window was closed during the check: report the answer, then go
            if gone and not self.was_removed:
                self.was_removed = True
                self.removed.emit(self.uid)
            if not self._busy and not self._cleaning:
                self.deleteLater()
            return
        if self._busy:   # a new uninstall started meanwhile; its own result decides
            return
        if gone:
            self._on_finished("removed", "{name} was removed", {"name": self.app.name}, True)
            return
        _, template, values = self._outcome
        self._show_result("still_listed", template, values,
                          tr("Still listed. If the uninstaller is still working, wait a moment and check again."))

    # -- closing -------------------------------------------------------
    def reject(self):
        if self._busy:
            box = QMessageBox(self)
            box.setWindowTitle("Upstream")
            box.setText(tr("The uninstaller is still running. Closing this window does not stop it, "
                           "and Upstream will no longer show how it went."))
            keep = box.addButton(tr("Keep waiting"), QMessageBox.AcceptRole)
            leave = box.addButton(tr("Close window"), QMessageBox.RejectRole)
            box.setDefaultButton(keep)
            box.setEscapeButton(keep)   # Esc or the X dismisses the question, which must not close the dialog
            box.exec()
            if box.clickedButton() is not leave:
                return
        super().reject()

    def release(self):
        """For whoever opened the dialog, once exec() returns. A dialog whose uninstaller or check is
        still running stays alive to report the result to the main window, then deletes itself."""
        self._released = True
        if not (self._busy or self._checking or self._cleaning):
            self.deleteLater()


# --------------------------------------------------------------------------------------- batch

class _Job:
    """One app of a batch: its two plans (quiet and vendor window) and what became of it."""

    def __init__(self, status: AppStatus):
        self.status, self.app = status, status.app
        self.plans = {quiet: uninstaller.plan(self.app, status.winget_id, prefer_silent=quiet) for quiet in (True, False)}
        self.outcome = "waiting"
        self.template, self.values = "", {}   # what the uninstaller said, English template and values
        self.gone = False

    def plan(self, quiet: bool) -> uninstaller.UninstallPlan:
        return self.plans[quiet]


def _how(plan: uninstaller.UninstallPlan) -> str:
    if plan.method == "msi":
        return tr("By Windows Installer")
    if plan.method == "winget":
        return tr("By winget")
    return tr("By its own uninstaller, quietly") if plan.silent else tr("In its own uninstall window")


class BatchUninstallDialog(_FlowDialog):
    """Uninstalls several apps one after another with each app's own uninstaller, then does one combined leftover
    review for all that were removed."""
    removed = Signal(str)   # uid of each app, as soon as Windows no longer lists it
    ended = Signal()        # one uninstaller stopped, whatever came of it

    def __init__(self, statuses: list[AppStatus], all_apps: list[InstalledApp], parent=None,
                 updates_running: Callable[[], bool] | bool = False, settings=None):
        super().__init__(parent, settings)
        self.jobs = [_Job(s) for s in statuses]
        self.others = list(all_apps)
        self._updates_running = updates_running if callable(updates_running) else (lambda: updates_running)
        self._busy = False
        self._want_scan = False
        self._wait_shown = ""
        self._token = f"batch:{id(self)}"
        self._stop_batch = threading.Event()
        self._finished = 0
        self._order: list[int] = []
        self._sort = _Sort()                          # the order the apps are listed in; the run order is not affected
        self._rows: dict[int, QTreeWidgetItem] = {}   # job -> its row on the result page
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_wait)

        self.setWindowTitle(trn("Uninstall {n} app", "Uninstall {n} apps", len(self.jobs)))
        self._add_page(self._build_confirm(), 860)
        self._add_page(self._build_running(), 860)
        self._add_cleanup_pages()
        self.bus.progress.connect(self._on_progress)
        self.bus.app_state.connect(self._on_app_state)
        self.bus.app_removed.connect(self._on_app_removed)
        self.bus.batch_done.connect(self._on_batch_done)
        self.cancel_btn.setDefault(True)
        self._show(BATCH_CONFIRM)

    @property
    def batch(self) -> bool:
        return True

    @property
    def cleanup_name(self) -> str:
        names = [j.app.name for j in self.jobs if j.gone] or [j.app.name for j in self.jobs]
        label = ", ".join(names[:3])
        return f"{label} +{len(names) - 3}" if len(names) > 3 else label

    @property
    def removed_apps(self) -> list[InstalledApp]:
        return [j.app for j in self.jobs if j.gone]

    def _quiet(self) -> bool:
        return self.quiet_box.isChecked()

    # -- building ------------------------------------------------------
    def _build_confirm(self) -> QWidget:
        page, lay = self._page()
        self.bc_title = QLabel(trn("Uninstall {n} app?", "Uninstall {n} apps?", len(self.jobs)))
        self.bc_title.setObjectName("dlgTitle")
        self.bc_title.setWordWrap(True)
        self.bc_total = QLabel()
        self.bc_total.setWordWrap(True)
        self.bc_total.setProperty("tone", "accent")
        self.bc_sub = QLabel()
        self.bc_sub.setWordWrap(True)
        self.bc_sub.setObjectName("hint")
        self.wait_notice = self._notice()
        self.apps_tree = QTreeWidget()
        self.apps_tree.setColumnCount(5)
        self.apps_tree.setHeaderLabels([tr("Application"), tr("Version"), tr("Size"), tr("How it will be removed"),
                                        tr("Heads-up")])
        self.apps_tree.setRootIsDecorated(False)
        self.apps_tree.setIndentation(0)
        self.apps_tree.setUniformRowHeights(True)
        self.apps_tree.setSelectionMode(QTreeWidget.NoSelection)
        self.apps_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.apps_tree.setItemDelegate(_Delegate(self.apps_tree))
        for column in range(4):
            self.apps_tree.header().setSectionResizeMode(column, QHeaderView.Interactive)
        self.apps_tree.header().setStretchLastSection(True)
        for column, width in enumerate((220, 90, 90, 190)):
            self.apps_tree.setColumnWidth(column, width)
        self.apps_tree.setMinimumHeight(200)
        self.apps_tree.setMaximumHeight(300)
        _sortable(self.apps_tree, _APP_KEYS, self._sort_apps)
        self.apps_sort_bar = self._app_sort_bar()
        self.ack_box = QCheckBox(tr("I understand that other apps or devices may stop working"))
        self.ack_box.toggled.connect(self._refresh_confirm)
        self.quiet_box = QCheckBox(tr("Run quietly when possible"))
        self.quiet_box.setChecked(True)
        self.quiet_box.toggled.connect(self._refresh_confirm)
        self.leftover_box = QCheckBox(tr("Look for leftovers afterwards"))
        self.leftover_box.setChecked(True)
        self.leftover_hint = QLabel(tr("One review covers every app that was removed. Nothing is removed from it "
                                       "until you choose it, and everything removed is backed up first."))
        self.leftover_hint.setObjectName("hint")
        self.leftover_hint.setWordWrap(True)
        for w in (self.bc_title, self.bc_total, self.bc_sub, self.wait_notice, self.apps_sort_bar, self.apps_tree,
                  self.ack_box):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(self.quiet_box)
        lay.addWidget(self.leftover_box)
        lay.addWidget(self.leftover_hint)
        lay.addStretch(1)
        lay.addSpacing(8)
        buttons = QHBoxLayout()
        self.go_btn = self._button("", "danger", self._start)
        self.go_btn.setAutoDefault(False)   # Enter must not start a removal
        self.cancel_btn = self._button(tr("Cancel"), "", self.reject)
        buttons.addWidget(self.go_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        lay.addLayout(buttons)
        return page

    def _build_running(self) -> QWidget:
        page, lay = self._page()
        self.run_title = QLabel(tr("Uninstalling apps"))
        self.run_title.setObjectName("dlgTitle")
        self.run_title.setWordWrap(True)
        self.step = QLabel(tr("Starting…"))
        self.step.setWordWrap(True)
        self.b_bar = QProgressBar()
        self.b_bar.setFixedHeight(4)
        self.b_bar.setTextVisible(False)
        self.state_tree = QTreeWidget()
        self.state_tree.setColumnCount(4)
        self.state_tree.setHeaderLabels([tr("Application"), tr("Version"), tr("Size"), tr("Outcome")])
        self.state_tree.setRootIsDecorated(False)
        self.state_tree.setIndentation(0)
        self.state_tree.setUniformRowHeights(True)
        self.state_tree.setSelectionMode(QTreeWidget.NoSelection)
        self.state_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.state_tree.setItemDelegate(_Delegate(self.state_tree))
        for column in range(3):
            self.state_tree.header().setSectionResizeMode(column, QHeaderView.Interactive)
        self.state_tree.header().setStretchLastSection(True)
        for column, width in enumerate((250, 90, 90)):
            self.state_tree.setColumnWidth(column, width)
        self.state_tree.setMinimumHeight(200)
        self.state_tree.setMaximumHeight(320)
        _sortable(self.state_tree, _STATE_KEYS, self._sort_apps)
        self.state_sort_bar = self._app_sort_bar()
        self.run_hint = QLabel(tr("Finish the steps in an uninstaller's own window when one opens. Windows may ask "
                                  "for permission."))
        self.run_hint.setObjectName("hint")
        self.run_hint.setWordWrap(True)
        for w in (self.run_title, self.step, self.b_bar, self.state_sort_bar, self.state_tree, self.run_hint):
            lay.addWidget(w)
        lay.addStretch(1)
        buttons = QHBoxLayout()
        self.b_stop = self._button(tr("Stop after this one"), "", self._stop_after_current)
        self.b_done = self._button(tr("Done"), "primary", self.accept)
        self.b_done.hide()
        buttons.addStretch(1)
        buttons.addWidget(self.b_stop)
        buttons.addWidget(self.b_done)
        lay.addLayout(buttons)
        return page

    # -- confirm -------------------------------------------------------
    def _on_page(self, index: int):
        if index == BATCH_CONFIRM:
            self._refresh_confirm()
            self._poll.start()
        else:
            self._poll.stop()

    def _shown(self) -> list[int]:
        """Positions in self.jobs in the order the lists show them: as given until a header is clicked."""
        values = {"name": lambda i: self.jobs[i].app.name.casefold(),
                  "version": lambda i: parse_version(self.jobs[i].app.version),
                  "size": lambda i: self.jobs[i].app.size_kb or None}
        return self._sort.apply(range(len(self.jobs)), values)

    def _app_cells(self, row: QTreeWidgetItem, job: _Job):
        """Name, version and size: the first three columns of both lists."""
        app = job.app
        row.setText(0, isolate(app.name))
        row.setToolTip(0, row.text(0))
        row.setText(1, _ltr(app.version))
        row.setText(2, app_size_text(app))
        row.setTextAlignment(2, Qt.AlignTrailing | Qt.AlignVCenter)

    def _app_sort_bar(self) -> _SortBar:
        bar = _SortBar([("name", tr("Name")), ("version", tr("Version")), ("size", tr("Size"))])
        bar.changed.connect(self._sort_apps_by)
        return bar

    def _sort_apps(self, key: str):
        """A header was clicked."""
        self._sort.click(key)
        self._resort_apps()

    def _sort_apps_by(self, key: str, reverse: bool):
        """A Sort by box was used."""
        self._sort.set(key, reverse)
        self._resort_apps()

    def _resort_apps(self):
        self._fill_apps()
        if self._rows:
            self.state_tree.invisibleRootItem().takeChildren()
            for i in self._shown():
                self.state_tree.addTopLevelItem(self._rows[i])
        for tree, keys in ((self.apps_tree, _APP_KEYS), (self.state_tree, _STATE_KEYS)):
            _show_sort(tree, keys, self._sort)
        for bar in (self.apps_sort_bar, self.state_sort_bar):
            bar.show_sort(self._sort)

    def _fill_apps(self):
        quiet = self._quiet()
        self.apps_tree.clear()
        for i in self._shown():
            job = self.jobs[i]
            plan = job.plan(quiet)
            row = QTreeWidgetItem(self.apps_tree)
            self._app_cells(row, job)
            if plan.blocked:
                row.setText(3, tr("Can't be removed here"))
                row.setText(4, render(plan.blocked, plan.blocked_values))
                for col in range(5):
                    row.setData(col, _ROW_ROLE, "protected")
            else:
                row.setText(3, _how(plan))
                row.setText(4, " ".join(render(t, v) for t, v in plan.notes))
            row.setToolTip(3, row.text(3))
            row.setToolTip(4, row.text(4))

    def _acks(self) -> bool:
        return any(not j.plan(self._quiet()).blocked and uninstaller.needs_ack(j.plan(self._quiet())) for j in self.jobs)

    def _refresh_confirm(self, *_):
        quiet = self._quiet()
        self._fill_apps()
        runnable = [j for j in self.jobs if not j.plan(quiet).blocked]
        skipped = len(self.jobs) - len(runnable)
        self.bc_title.setText(trn("Uninstall {n} app?", "Uninstall {n} apps?", len(runnable)))
        self.bc_total.setText(self._total_text(runnable))
        self.bc_total.setVisible(bool(self.bc_total.text()))
        sub = tr("They are removed one after another. Apps that open their own window go first.")
        if skipped:
            sub += " " + trn("{n} app can't be removed here and is skipped.", "{n} apps can't be removed here and are skipped.",
                             skipped)
        self.bc_sub.setText(sub)
        wait = self._wait_shown = self._wait_reason()
        self.wait_notice.label.setText(wait)
        self.wait_notice.setVisible(bool(wait))
        self.ack_box.setVisible(self._acks())
        self.go_btn.setText(trn("Uninstall {n} app", "Uninstall {n} apps", len(runnable)))
        self.go_btn.setEnabled(bool(runnable) and not wait and not self._needs_ack())
        if self.stack.currentIndex() == BATCH_CONFIRM:
            self._fit()

    @staticmethod
    def _total_text(runnable: list[_Job]) -> str:
        """'3 apps, about 1.2 GB to free', plus a word about the apps Windows reports no size for; '' when none does."""
        total = sum(j.app.size_kb for j in runnable)
        if total <= 0:
            return ""
        text = trn("{n} app, about {size} to free", "{n} apps, about {size} to free", len(runnable),
                   size=num(format_size(total * 1024)))
        unknown = sum(1 for j in runnable if j.app.size_kb <= 0)
        if unknown:
            text += " · " + trn("{n} app has no reported size and isn't counted.",
                              "{n} apps have no reported size and aren't counted.", unknown)
        return text

    @property
    def updates_running(self) -> bool:
        return bool(self._updates_running())

    def _wait_reason(self) -> str:
        if _RUNNING - {self._token}:
            return tr("Another uninstall is still running. Wait until it finishes.")
        if self.updates_running:
            return tr("Updates are running right now. Removing an app at the same time could make both fail, "
                      "so wait until they finish.")
        return ""

    def _needs_ack(self) -> bool:
        return self._acks() and not self.ack_box.isChecked()

    def _poll_wait(self):
        if self._wait_reason() != self._wait_shown:
            self._refresh_confirm()

    # -- running -------------------------------------------------------
    def _start(self):
        quiet = self._quiet()
        runnable = [i for i, j in enumerate(self.jobs) if not j.plan(quiet).blocked]
        if self._busy or not runnable or self._wait_reason() or self._needs_ack():
            return
        self._busy = True
        self._want_scan = self.leftover_box.isChecked()
        self._stop_batch = threading.Event()
        self._finished = 0
        _RUNNING.add(self._token)
        # Apps that open their own window need the user, so they go first; the quiet ones follow.
        self._order = sorted(runnable, key=lambda i: self.jobs[i].plan(quiet).silent)
        plans = [(i, self.jobs[i].plan(quiet)) for i in self._order]
        self.state_tree.clear()
        self._rows = {}
        for i in self._shown():
            job = self.jobs[i]
            row = QTreeWidgetItem(self.state_tree)
            self._app_cells(row, job)
            self._rows[i] = row
            if i in runnable:
                self._set_row(i, "waiting")
            else:
                job.outcome = "skipped"
                self._set_row(i, "skipped", render(job.plan(quiet).blocked, job.plan(quiet).blocked_values))
        self.b_bar.setRange(0, len(plans))
        self.b_bar.setValue(0)
        self.run_title.setText(trn("Uninstalling {n} app", "Uninstalling {n} apps", len(plans)))
        self.step.setText(tr("Starting…"))
        self.b_stop.show()
        self.b_stop.setEnabled(True)
        self.b_stop.setText(tr("Stop after this one"))
        self.b_done.hide()
        log.info("batch: uninstalling %d apps", len(plans))
        self._show(BATCH_RUNNING)
        threading.Thread(target=self._work, args=(self.bus, plans, self._stop_batch, self.status_of), daemon=True).start()

    def status_of(self, index: int) -> AppStatus:
        return self.jobs[index].status

    @staticmethod
    def _work(bus: _Bus, plans: list[tuple[int, uninstaller.UninstallPlan]], stop: threading.Event, status_of):
        for index, plan in plans:
            if stop.is_set():
                bus.app_state.emit(index, "skipped", N_("Skipped because the list was stopped"), {})
                continue
            bus.app_state.emit(index, "running", "", {"name": plan.app.name})
            try:
                outcome, template, values = uninstaller.run(plan, lambda t, **v: bus.progress.emit(t, v))
            except Exception as e:
                log.exception("uninstall of %s failed", plan.app.name)
                outcome, template, values = "failed", "{error}", {"error": str(e)}
            gone = outcome == "removed"
            if outcome == "reboot_required":
                try:
                    gone = uninstaller.is_gone(plan.app, status_of(index).winget_id)
                except Exception:
                    log.exception("could not check %s after the uninstall", plan.app.name)
            if gone:
                bus.app_removed.emit(index)
            bus.app_state.emit(index, outcome, template, values)
        bus.batch_done.emit()

    def _set_row(self, index: int, outcome: str, detail: str = ""):
        title, tone = RESULT_TITLES.get(outcome, RESULT_TITLES["failed"])
        text = tr(title) + (f": {detail}" if detail else "")
        row = self._rows[index]
        row.setText(3, text)
        row.setToolTip(3, text)
        row.setData(3, _ROW_ROLE, "protected" if outcome in ("waiting", "skipped", "cancelled") else None)

    def _on_progress(self, template: str, values: dict):
        self.step.setText(render(template, values))

    def _on_app_state(self, index: int, outcome: str, template: str, values: dict):
        job = self.jobs[index]
        if outcome == "running":
            self.step.setText(tr("Uninstalling {name}", name=job.app.name))
            self._set_row(index, "running")
            self.state_tree.scrollToItem(self._rows[index])
            return
        job.outcome, job.template, job.values = outcome, template, values
        detail = render(template, values) if outcome != "removed" and template else ""
        self._set_row(index, outcome, detail)
        if outcome != "skipped":
            self._finished += 1
            self.b_bar.setValue(self._finished)
            self.ended.emit()
        log.info("%s: %s", job.app.name, english(template, values) if template else outcome)

    def _on_app_removed(self, index: int):
        job = self.jobs[index]
        if not job.gone:
            job.gone = True
            self.removed.emit(job.status.uid)

    def _stop_after_current(self):
        self._stop_batch.set()
        self.b_stop.setEnabled(False)
        self.step.setText(tr("Stopping after the one in progress…"))

    def _batch_outcome(self) -> tuple[str, str, dict]:
        """'removed' when every app that was tried is gone, else how the first one that stayed ended (one that
        never ran last). An app that can't be removed here is not counted: it was never tried."""
        left = [j for j in self.jobs if not j.gone and (j.outcome != "skipped" or j.template)]
        if not left:
            return "removed", "", {}
        job = next((j for j in left if j.outcome not in ("waiting", "skipped")), left[0])
        return job.outcome, job.template, job.values

    def _on_batch_done(self):
        self._busy = False
        _RUNNING.discard(self._token)
        self._outcome = self._batch_outcome()
        if self._released:
            self.deleteLater()
            return
        gone = len(self.removed_apps)
        text = tr("Removed {done} of {total} apps", done=num(gone), total=num(len(self.jobs)))
        if self._want_scan and gone:
            self._begin_scan(text)
            return
        self.run_title.setText(text)
        self.step.setText("")
        self.b_stop.hide()
        self.b_done.show()
        self._fit()

    # -- review --------------------------------------------------------
    def _scan_job(self):
        apps, others = self.removed_apps, list(self.others)
        gone_keys = {a.reg_key for a in apps}
        remaining = [a for a in others if a.reg_key not in gone_keys]

        def job():
            merged: dict[tuple[str, str], tuple[leftovers.Leftover, list[str]]] = {}
            for app in apps:
                for it in leftovers.scan(app, remaining):
                    key = (it.kind, guard.normalize(it.location) if it.kind in _PATH_KINDS else it.location.lower())
                    old = merged.get(key)
                    if old is None:
                        merged[key] = (it, [app.name])
                        continue
                    best = it if leftovers._RANK[it.confidence] < leftovers._RANK[old[0].confidence] else old[0]
                    merged[key] = (best, old[1] + ([app.name] if app.name not in old[1] else []))
            order = {"folder": 0, "file": 1, "shortcut": 2, "regkey": 3, "regvalue": 4, "service": 5}
            rows = sorted(merged.values(), key=lambda r: (leftovers._RANK[r[0].confidence], order.get(r[0].kind, 6),
                                                          r[0].location.lower()))
            return [r[0] for r in rows], [", ".join(r[1]) for r in rows]

        return job

    # -- closing -------------------------------------------------------
    def reject(self):
        if self._busy:
            box = QMessageBox(self)
            box.setWindowTitle("Upstream")
            box.setText(tr("Uninstalls are still running. Closing this window stops the rest of the list, but the "
                           "one in progress keeps going and Upstream will no longer show how it went."))
            keep = box.addButton(tr("Keep waiting"), QMessageBox.AcceptRole)
            leave = box.addButton(tr("Close window"), QMessageBox.RejectRole)
            box.setDefaultButton(keep)
            box.setEscapeButton(keep)
            box.exec()
            if box.clickedButton() is not leave:
                return
            self._stop_batch.set()
        super().reject()

    def release(self):
        """For whoever opened the dialog, once exec() returns. A batch still running stops after the app in
        progress, keeps reporting to the main window, then deletes itself."""
        self._released = True
        if self._busy:
            self._stop_batch.set()
        elif not self._cleaning:
            self.deleteLater()


# -------------------------------------------------------------------------------------- backups

class BackupsDialog(QDialog):
    """The cleanup backups: what each holds, restore it or delete it."""
    restored = Signal(str, object)   # the backup's app name, cleanup.RestoreResult

    def __init__(self, parent=None, settings=None, days: int | None = None):
        super().__init__(parent)
        self.settings = settings
        self.days = days if days is not None else (int(settings.get("cleanup_backup_days", 30)) if settings is not None else 0)
        self.bus = _Bus()
        self._working = False
        self._token = f"restore:{id(self)}"
        self.backups: list[cleanup.Backup] = []
        self._doomed: cleanup.Backup | None = None    # the backup the open confirmation asks about
        self.setWindowTitle(tr("Backups"))
        self.setMinimumWidth(680)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        title = QLabel(tr("Backups"))
        title.setObjectName("dlgTitle")
        hint = QLabel(tr("Leftovers that Upstream removed are kept here first, so you can undo a cleanup. "
                         "Backups older than {days} days are deleted automatically.", days=num(self.days)) if self.days > 0
                      else tr("Leftovers that Upstream removed are kept here first, so you can undo a cleanup."))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(hint)

        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr("Backed up on"), tr("Application"), tr("Items backed up"), tr("Space used")])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setMinimumHeight(220)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.empty = QLabel(tr("No backups yet."))
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("hint")
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.empty)
        lay.addWidget(self.stack, 1)

        self.status = QLabel()
        self.status.setProperty("tone", "accent")
        self.status.setWordWrap(True)
        self.problems_title = QLabel(tr("Left as it is"))
        self.problems_title.setObjectName("section")
        self.problems = _problem_tree()
        self.bar = _bar()
        self.confirm = QFrame()
        self.confirm.setObjectName("notice")
        self.confirm.setProperty("tone", "danger")
        row = QHBoxLayout(self.confirm)
        row.setContentsMargins(12, 9, 12, 9)
        self.question = QLabel()
        self.question.setWordWrap(True)
        self.confirm_yes = _button(tr("Delete for good"), "danger", self._delete_confirmed)
        self.confirm_no = _button(tr("Keep backup"), "", self._keep)
        row.addWidget(self.question, 1)
        row.addWidget(self.confirm_yes)
        row.addWidget(self.confirm_no)
        for w in (self.status, self.problems_title, self.problems, self.bar, self.confirm):
            lay.addWidget(w)
        for w in (self.problems_title, self.problems, self.bar, self.confirm, self.status):
            w.hide()

        buttons = QHBoxLayout()
        self.restore_btn = _button(tr("Restore backup"), "primary", self._restore)
        self.delete_btn = _button(tr("Delete backup"), "", self._delete)
        close = _button(tr("Close"), "", self.accept)
        buttons.addWidget(self.restore_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self.bus.undo_progress.connect(self._on_progress)
        self.bus.undo_done.connect(self._on_done)
        self.refresh()

    def refresh(self):
        self.backups = cleanup.backups()
        self.tree.clear()
        for backup in self.backups:
            row = QTreeWidgetItem(self.tree)
            row.setText(0, when_text(backup.ts))
            row.setText(1, isolate(backup.app_name))
            row.setText(2, num(backup.items))
            row.setText(3, _ltr(size_text(backup.size_bytes)) if backup.size_bytes else "")
            row.setData(0, Qt.UserRole, backup.id)
            row.setToolTip(1, row.text(1))
        self.stack.setCurrentIndex(0 if self.backups else 1)
        if self.backups:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._update_buttons()

    def selected(self) -> cleanup.Backup | None:
        picked = self.tree.selectedItems()
        wanted = picked[0].data(0, Qt.UserRole) if picked else None
        return next((b for b in self.backups if b.id == wanted), None)

    def _on_selection(self):
        if self._doomed is not None:    # another row was picked: the question was about the old one
            self._close_confirmation()
        self._update_buttons()

    def _close_confirmation(self):
        self._doomed = None
        self.confirm.hide()

    def _update_buttons(self):
        ready = self.selected() is not None and not self._working
        self.restore_btn.setEnabled(ready)
        self.delete_btn.setEnabled(ready and not self.confirm.isVisible())

    def _restore(self):
        backup = self.selected()
        if backup is None or self._working:
            return
        self._working = True
        _RUNNING.add(self._token)
        self._name = backup.app_name
        self._close_confirmation()
        self.problems_title.hide()
        self.problems.hide()
        self._say(tr("Restoring…"))
        self.bar.setRange(0, 0)
        self.bar.show()
        self._update_buttons()
        threading.Thread(target=_restore_work, args=(self.bus, backup.id), daemon=True).start()

    def _say(self, text: str, tone: str = "accent"):
        self.status.setText(text)
        self.status.setProperty("tone", tone)
        theme.repolish(self.status)
        self.status.show()

    def _on_progress(self, template: str, percent: int, values: dict):
        self.status.setText(render(template, values))
        if percent >= 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(percent)

    def _on_done(self, result: cleanup.RestoreResult):
        self._working = False
        _RUNNING.discard(self._token)
        self.bar.hide()
        self._say(_restore_summary(result))
        self.problems_title.setVisible(bool(result.failed))
        self.problems.setVisible(bool(result.failed))
        _fill_problems(self.problems, result.failed)
        self.restored.emit(self._name, result)
        self.refresh()

    def _keep(self):
        self._close_confirmation()
        self._update_buttons()

    def _delete(self):
        backup = self.selected()
        if backup is None or self._working:
            return
        self._doomed = backup
        self.question.setText(tr("Delete the backup of {app} from {date} for good? Then it can no longer be restored.",
                                 app=isolate(backup.app_name), date=when_text(backup.ts)))
        self.confirm.show()
        self._update_buttons()

    def _delete_confirmed(self):
        backup, self._doomed = self._doomed, None    # what was asked about, not whatever is selected now
        self.confirm.hide()
        if backup is None or self._working:
            self._update_buttons()
            return
        deleted = cleanup.delete_backup(backup.id)
        self._say(tr("Backup deleted.") if deleted else
                  tr("Couldn't delete this backup. A cleanup or an undo may be running, or it was made while "
                     "Upstream ran as administrator."), "accent" if deleted else "danger")
        self.problems_title.hide()
        self.problems.hide()
        self.refresh()

    def reject(self):
        if self._working:
            return
        super().reject()
