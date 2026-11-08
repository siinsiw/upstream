"""Main window: updates, all apps, discover, history, ignored apps, and the install queue."""
from __future__ import annotations

import dataclasses
import logging
import os
import queue
import threading
import time
from datetime import datetime
from functools import partial
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QFileInfo, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFileIconProvider, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import badges, details, discover, elevation, export, history, pins, scanner as scan_store
from ..context import CancelToken, Question, UpdateContext
from ..errors import Cancelled
from ..i18n import N_, english, num, render, tr, trn
from ..scanner import AppStatus, Scanner
from ..updater import Updater
from ..versioning import compare
from .details import DetailsDialog
from .discover import DiscoverPage
from .history import HistoryPage
from .library import LibraryPage
from .locale import apply_language as apply_qt_language
from .settings import SettingsDialog
from .theme import app_icon, apply_theme, letter_avatar
from .uninstall import RESULT_TITLES, BackupsDialog, BatchUninstallDialog, UninstallDialog, running as uninstalls_running
from .widgets import AppRow, SourceDialog

log = logging.getLogger("upstream.ui")

TABS = {"updates": "Updates", "all": "All apps", "discover": "Discover", "history": "History", "ignored": "Ignored"}
COUNTED = ("updates", "all", "ignored")     # the tabs whose label carries a count
SEARCHED = ("updates", "all", "ignored")    # the tabs the search box above the list filters
UPDATE_SORTS = (("name", N_("Name")), ("size", N_("Download size, largest first")), ("publisher", N_("Publisher")))
# Progress messages (updater.py) shown while an installer runs; cancelling then only stops the waiting.
INSTALL_PHASES = frozenset({"Installing", "Setup window opened"})
# Seconds a cancelled update gets to stop by itself (a download does, in a moment) before the queue
# moves on without it. A step that does not notice the token is left to finish in the background.
WIND_DOWN = 2.0
# Messages with which an update ends because the user (or the metered / battery policy they chose) stopped it:
# not a failure, so not history. They are the templates the update steps raise as Cancelled or classify as cancelled.
USER_STOPPED = frozenset({
    "Cancelled.", "The installation was cancelled", "Administrator permission (UAC) was declined.",
    "Update cancelled: {name} is still running.",
    "Not installed: you declined a file that wasn't signed by the app's usual publisher.",
    "Not updated: this PC is on a metered connection.",
    "Not updated: the battery is at {percent}% and the PC is not plugged in.",
    "Not updated: battery saver is on and the PC is not plugged in.",
})
UNINSTALL_DONE = ("removed", "reboot_required")
UNINSTALL_FAILED = ("failed", "still_listed")
LOOKING_UP = N_("Looking up older versions…")
HINT_MS = 4000          # how long a refusal's hint replaces the subtitle
MIN_WIDTH = 800         # the narrowest the window may be; wider when its header or pages need more (_fit_header)
SUBTITLE_ROOM = 160     # the least the subtitle keeps while Update all still shows the download size
LAZY_ROWS = 12          # the first rows of a list are always built, however small the list's viewport is
LAZY_REACH = 400        # px above and below the viewport within which a list builds its rows
RESCAN_RETRY_MS = 1000  # how often a check that had to wait for an uninstall tries again
# What formatting a stored template with its stored values raises when the file on disk holds nonsense.
BAD_FORMAT = (ValueError, KeyError, IndexError, TypeError, AttributeError)


class Bus(QObject):
    """Signals emitted from worker threads; Qt delivers them on the UI thread."""
    scan_progress = Signal(str, int, int)
    scan_finished = Signal(object)
    scan_failed = Signal(str)
    recheck_finished = Signal(object)
    job_progress = Signal(str, str, object, int)        # uid, template, values, percent
    job_finished = Signal(str, bool, str, object, str)  # uid, ok, template, values, version
    log_line = Signal(str)
    question = Signal(object, object)                    # context.Question, _Answer
    versions_found = Signal(str, object)                 # uid, versions winget knows (newest first)


class _Answer:
    """Carries the user's reply from the UI thread back to the worker that asked."""

    def __init__(self, token: CancelToken):
        self.token = token      # of the job that asked: once it is cancelled nobody is waiting for the reply
        self.event = threading.Event()
        self.value = False


class UiLogHandler(logging.Handler):
    def __init__(self, bus: Bus):
        super().__init__(logging.INFO)
        self.bus = bus
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.bus.log_line.emit(self.format(record))
        except RuntimeError:
            pass


class Subtitle(QLabel):
    """The line under the title. It gives up width before any button does: elided with the whole text in the
    tooltip, never clipped or covered. text() is always the whole line."""

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def setText(self, text: str):
        super().setText(text)
        self._sync_tip()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_tip()

    def _elided(self) -> bool:
        return self.fontMetrics().horizontalAdvance(self.text()) > self.contentsRect().width()

    def _sync_tip(self):
        self.setToolTip(self.text() if self._elided() else "")

    def paintEvent(self, event):
        if not self._elided():
            super().paintEvent(event)
            return
        rect = self.contentsRect()
        side = Qt.AlignRight if self.layoutDirection() == Qt.RightToLeft else Qt.AlignLeft
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(rect, side.value | Qt.AlignAbsolute.value | Qt.AlignVCenter.value,
                   self.fontMetrics().elidedText(self.text(), Qt.ElideRight, rect.width()))


class ResizeWatcher(QObject):
    """Calls back when the widget it watches is resized."""

    def __init__(self, widget: QWidget, callback):
        super().__init__(widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self.callback()
        return False


def _indented(note: QLabel) -> QHBoxLayout:
    """A hint under a radio button or check box, lined up with its text (the gap flips in a right-to-left window)."""
    row = QHBoxLayout()
    row.addSpacing(26)
    row.addWidget(note, 1)
    return row


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


class PinDialog(QDialog):
    """What to do about newer versions of one app: hold it where it is, keep it on its major version, or nothing.
    `mode` is "hold", "major" or None once the dialog is accepted."""

    def __init__(self, status: AppStatus, parent=None):
        super().__init__(parent)
        app = status.app
        self.mode: str | None = None
        line = pins.major_line(status.pin, app.version)
        self.setWindowTitle(tr("Pin version — {name}", name=app.name))
        self.setMinimumWidth(480)
        options = (
            ("hold", tr("Hold at {version}", version=app.version or tr("the current version")),
             tr("No update is offered for this app.")),
            ("major", tr("Stay on version {line}", line=f"{line}.x") if line is not None else tr("Stay on this major version"),
             tr("Updates within that major version are offered; a newer major version is not.") if line is not None
             else tr("Needs a numeric version, such as 3.12.4.")),
            (None, tr("Don't pin"), tr("Every update is offered.")),
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(_hint(tr("Choose how Upstream treats newer versions of {name}.", name=app.name)))
        group = QButtonGroup(self)
        self.radios: dict[str | None, QRadioButton] = {}
        for mode, title, hint in options:
            radio = self.radios[mode] = QRadioButton(title)
            radio.setEnabled(mode != "major" or line is not None)
            group.addButton(radio)
            lay.addWidget(radio)
            lay.addLayout(_indented(_hint(hint)))
        current = (status.pin or {}).get("mode")
        self.radios[current if current in self.radios and self.radios[current].isEnabled() else None].setChecked(True)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.save_btn = QPushButton(tr("Save"))
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_btn)
        lay.addSpacing(6)
        lay.addLayout(buttons)

    def _save(self):
        self.mode = next((m for m, r in self.radios.items() if r.isChecked()), None)
        self.accept()


class RollbackDialog(QDialog):
    """Picks one of the older versions winget knows. `version` is the choice once the dialog is accepted."""

    def __init__(self, status: AppStatus, versions: list[str], parent=None):
        super().__init__(parent)
        app = status.app
        self.version = ""
        self.setWindowTitle(tr("Roll back — {name}", name=app.name))
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(_hint(tr("Installed: {version}. Choose an older version to install over it.", version=app.version)))
        self.list = QListWidget()
        self.list.setObjectName("versions")
        self.list.setLayoutDirection(Qt.LeftToRight)    # versions are Latin text
        self.list.setMinimumHeight(180)
        for version in versions:
            self.list.addItem(version)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _item: self._choose())
        lay.addWidget(self.list)

        notice = QFrame()
        notice.setObjectName("notice")
        notice.setProperty("tone", "danger")
        nl = QHBoxLayout(notice)
        nl.setContentsMargins(12, 9, 12, 9)
        warning = QLabel(tr("Some apps can't be downgraded, and an older version may not understand data that a "
                            "newer one saved. Close the app first."))
        warning.setWordWrap(True)
        nl.addWidget(warning)
        lay.addWidget(notice)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.next_btn = QPushButton(tr("Continue"))
        self.next_btn.setObjectName("primary")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._choose)
        buttons.addWidget(cancel)
        buttons.addWidget(self.next_btn)
        lay.addSpacing(6)
        lay.addLayout(buttons)

    def _choose(self):
        item = self.list.currentItem()
        if item is not None:
            self.version = item.text()
            self.accept()


def scrub_stored_text(s: AppStatus) -> None:
    """A snapshot is a file on disk: a note or badge whose template cannot be formatted with its values (a bad format
    spec, a missing key) is shown as the plain template instead of aborting the start-up while a row renders it."""
    try:
        s.note_text()
    except BAD_FORMAT:
        s.note_args = {}
    clean = []
    for badge in s.badges:
        try:
            render(badge.text, badge.values)
            render(badge.tooltip, badge.values)
        except BAD_FORMAT:
            badge = dataclasses.replace(badge, values={})
        clean.append(badge)
    s.badges = clean


def uninstall_outcome(dialog) -> tuple[str, str, dict]:
    """(outcome, template, values) an uninstall dialog ended with: the public `outcome` where the dialog has one."""
    raw = getattr(dialog, "outcome", getattr(dialog, "_outcome", None))
    if isinstance(raw, str):
        return raw, "", {}
    if isinstance(raw, (tuple, list)) and len(raw) == 3 and isinstance(raw[0], str):
        return raw[0], raw[1] or "", raw[2] or {}
    return "", "", {}


def older_versions(installed: str, found: list[str]) -> list[str]:
    """The versions winget lists that are older than the installed one, in winget's order (newest first).
    A version that can't be compared with the installed one is left out: it may not be older."""
    return [v for v in found if (compare(v, installed) or 0) < 0]


class MainWindow(QMainWindow):
    def __init__(self, settings, cache, *, persist: bool = False):
        """`persist` lets the window write history entries and the last-scan snapshot. The app turns it on; a window
        built without it (a test) never touches the user's data folder."""
        super().__init__()
        self.settings = settings
        self.persist = persist
        self.scanner = Scanner(settings, cache)
        self.updater = Updater(settings)
        self.bus = Bus()
        self.background = None      # background.Background, set by app.py
        self.statuses: dict[str, AppStatus] = {}
        # Packages from the Discover tab that are queued or installing. Kept apart from `statuses`: they are not
        # installed apps and must not show up in the lists, the counts or the snapshot.
        self.discover_statuses: dict[str, AppStatus] = {}
        self.rows: dict[str, list[AppRow]] = {}
        self.active_jobs: set[str] = set()
        # Apps uninstalled since the last check. A check that began earlier still lists them.
        self.removed: set[str] = set()
        # Last thing each row showed, kept here because populate() rebuilds rows:
        # ("queued",) | ("progress", template, values, pct) | ("cancelling",) | ("cancelled", template, values)
        # | ("result", ok, template, values, version)
        self.job_state: dict[str, tuple] = {}
        self.jobs: queue.Queue[tuple[str, CancelToken]] = queue.Queue()
        self.cancels: dict[str, CancelToken] = {}   # uid -> token of a queued or running update
        self.taken: set[str] = set()                # uids the worker has picked up; the rest are still queued
        self.queue_lock = threading.Lock()          # makes "cancel a queued job" and "worker picks it up" exclusive
        self.bulk_jobs: set[str] = set()            # uids queued by "Update all"
        self.auto_jobs: set[str] = set()            # uids queued by an automatic-update rule: the job never asks anything
        self.scanning = False
        self.last_scan: datetime | None = None
        self.current_tab = "updates"
        self.icon_queue: list[AppRow] = []
        self.icon_cache: dict = {}
        self.icon_provider = QFileIconProvider()
        self.history_page: HistoryPage | None = None    # built when the tab is first opened: it reads the history file
        self._library_statuses: list[AppStatus] = []    # the apps the All apps tab lists (not the ignored ones)
        self._skipped_uids: set[str] = set()
        self._update_rows: list[AppRow] = []
        self._stale = False                 # the list comes from the last saved scan; a fresh check is on its way
        self._rescan_wanted = False         # a Discover install finished: the next check shows the new app
        self._rollback_original: dict[str, tuple] = {}   # uid -> (update, fallbacks) a rollback replaced
        self._rollback_hold: dict[str, str] = {}         # uid -> version to hold the app at once its rollback succeeded
        self._looking_up: set[str] = set()               # uids whose older versions winget is being asked for
        self._leave_ok = False              # the user already confirmed leaving with work running
        self._banner_dismissed = False
        self._banner_note = ""
        self._elevated = self._is_elevated()
        self._subtitle_after = ""           # what the subtitle shows again once a hint has had its time
        # Rows of the Updates and Ignored lists that are not built yet: uid -> (status, skipped, ignored).
        self._deferred: dict[str, dict[str, tuple]] = {"updates": {}, "ignored": {}}
        self._realizing = False             # _realize is running; a call that arrives meanwhile is queued
        self._realize_again: set[str] = set()
        self._all_texts = ("", "", "")      # Update all: the full text, the short one, and the tooltip the short one needs
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.setInterval(HINT_MS)
        self._hint_timer.timeout.connect(self._end_hint)
        self._rescan_timer = QTimer(self)   # a check that had to wait for an uninstall tries again
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(RESCAN_RETRY_MS)
        self._rescan_timer.timeout.connect(self._rescan_if_wanted)

        self._build()
        self.bus.scan_progress.connect(self._on_scan_progress)
        self.bus.scan_finished.connect(self._on_scan_finished)
        self.bus.scan_failed.connect(self._on_scan_failed)
        self.bus.recheck_finished.connect(self._on_recheck_finished)
        self.bus.job_progress.connect(self._on_job_progress)
        self.bus.job_finished.connect(self._on_job_finished)
        self.bus.log_line.connect(self.log_view.appendPlainText)
        self.bus.question.connect(self._on_question)
        self.bus.versions_found.connect(self._on_versions_found)
        logging.getLogger("upstream").addHandler(UiLogHandler(self.bus))

        self.icon_timer = QTimer(self)
        self.icon_timer.setInterval(0)
        self.icon_timer.timeout.connect(self._load_icons)
        threading.Thread(target=self._job_loop, daemon=True).start()

    @staticmethod
    def _is_elevated() -> bool:
        try:
            return elevation.is_elevated()
        except Exception:
            return False

    # -- layout --------------------------------------------------------
    def _build(self):
        self.setWindowTitle("Upstream")
        self.setWindowIcon(app_icon())
        self.resize(1040, 740)
        self.setMinimumSize(MIN_WIDTH, 520)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(34, 26, 34, 14)
        v.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Upstream")
        title.setObjectName("title")
        self.subtitle = Subtitle()
        self.subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        self._head = head
        head.addLayout(titles)
        head.addStretch(1)
        self.settings_btn = QPushButton()
        self.settings_btn.clicked.connect(self.open_settings)
        self.export_btn = QPushButton()
        self.export_menu = QMenu(self.export_btn)
        self.export_btn.setMenu(self.export_menu)
        self.scan_btn = QPushButton()
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_all_btn = QPushButton()
        self.cancel_all_btn.setEnabled(False)
        self.cancel_all_btn.clicked.connect(self.cancel_all_jobs)
        self.all_btn = QPushButton()
        self.all_btn.setObjectName("primary")
        self.all_btn.setEnabled(False)
        self.all_btn.clicked.connect(self.update_all)
        for b in (self.settings_btn, self.export_btn, self.scan_btn, self.cancel_all_btn, self.all_btn):
            b.setCursor(Qt.PointingHandCursor)
            head.addWidget(b, 0, Qt.AlignBottom)
        v.addLayout(head)
        v.addSpacing(16)

        # Updates that will each ask for administrator rights: one restart as administrator asks once.
        self.banner_box = QWidget()
        bl = QVBoxLayout(self.banner_box)
        bl.setContentsMargins(0, 0, 0, 10)
        banner = QFrame()
        banner.setObjectName("notice")
        row = QHBoxLayout(banner)
        row.setContentsMargins(14, 9, 10, 9)
        row.setSpacing(10)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_note = QLabel()
        self.banner_note.setProperty("tone", "danger")
        self.banner_note.setWordWrap(True)
        self.banner_note.hide()
        texts.addWidget(self.banner_text)
        texts.addWidget(self.banner_note)
        self.banner_btn = QPushButton()
        self.banner_btn.setCursor(Qt.PointingHandCursor)
        self.banner_btn.clicked.connect(self.restart_as_admin)
        self.banner_dismiss = QPushButton()
        self.banner_dismiss.setObjectName("link")
        self.banner_dismiss.setCursor(Qt.PointingHandCursor)
        self.banner_dismiss.clicked.connect(self.dismiss_banner)
        row.addLayout(texts, 1)
        row.addWidget(self.banner_btn, 0, Qt.AlignVCenter)
        row.addWidget(self.banner_dismiss, 0, Qt.AlignVCenter)
        bl.addWidget(banner)
        self.banner_box.hide()
        v.addWidget(self.banner_box)

        self.scan_bar = QProgressBar()
        self.scan_bar.setFixedHeight(3)
        self.scan_bar.setTextVisible(False)
        self.scan_bar.hide()
        v.addWidget(self.scan_bar)
        v.addSpacing(6)

        bar = QHBoxLayout()
        tabs = QHBoxLayout()
        tabs.setSpacing(22)
        self.tab_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key in TABS:
            b = QPushButton()
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _checked=False, k=key: self.show_tab(k))
            group.addButton(b)
            tabs.addWidget(b)
            self.tab_buttons[key] = b
        bar.addLayout(tabs)
        bar.addStretch(1)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(150)
        self.search.setMaximumWidth(250)
        self.search.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search.textChanged.connect(self.apply_filter)
        bar.addWidget(self.search)
        v.addLayout(bar)
        v.addSpacing(12)

        self.stack = QStackedWidget()
        self.lists: dict[str, QListWidget] = {}
        for key in ("updates", "ignored"):
            lw = QListWidget()
            lw.setSpacing(3)
            lw.setSelectionMode(QAbstractItemView.NoSelection)
            lw.setFocusPolicy(Qt.NoFocus)
            lw.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            lw.verticalScrollBar().setSingleStep(20)
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            lw.verticalScrollBar().valueChanged.connect(partial(self._realize, key))
            ResizeWatcher(lw.viewport(), partial(self._realize, key))
            self.lists[key] = lw
        self.updates_page = QWidget()
        up = QVBoxLayout(self.updates_page)
        up.setContentsMargins(0, 0, 0, 0)
        up.setSpacing(8)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.sort_label = QLabel()
        self.sort_label.setProperty("tone", "muted")
        self.sort_combo = QComboBox()
        for key, _label in UPDATE_SORTS:
            self.sort_combo.addItem("", key)
        self.sort_label.setBuddy(self.sort_combo)
        self.sort_combo.setCurrentIndex(self.sort_combo.findData(self._updates_sort()))
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        tools.addWidget(self.sort_label)
        tools.addWidget(self.sort_combo)
        tools.addStretch(1)
        up.addLayout(tools)
        up.addWidget(self.lists["updates"], 1)
        self.library = LibraryPage(self._make_row)
        self.library.batch_uninstall_requested.connect(self.batch_uninstall)
        self.library.batch_export_requested.connect(self.batch_export)
        self.library.batch_ignore_requested.connect(self.batch_ignore)
        self.discover_page = DiscoverPage()
        self.discover_page.install_requested.connect(self._on_discover_install)
        self.discover_page.cancel_requested.connect(self._on_discover_cancel)
        for page in (self.updates_page, self.lists["ignored"], self.library, self.discover_page):
            self.stack.addWidget(page)
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

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setLayoutDirection(Qt.LeftToRight)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(150)
        self.log_view.hide()
        v.addSpacing(8)
        v.addWidget(self.log_view)

        foot = QHBoxLayout()
        self.footer = QLabel()
        self.footer.setObjectName("footer")
        self.log_btn = QPushButton()
        self.log_btn.setObjectName("link")
        self.log_btn.setCursor(Qt.PointingHandCursor)
        self.log_btn.clicked.connect(self.toggle_log)
        foot.addWidget(self.footer)
        foot.addStretch(1)
        foot.addWidget(self.log_btn)
        v.addSpacing(6)
        v.addLayout(foot)

        self.subtitle.setText(tr("Starting…"))
        self.retranslate()
        start = self.settings.get("last_tab")
        self.show_tab(start if start in TABS else "updates", remember=False)
        # Start in the search box: typing filters at once, and no button looks pre-selected.
        self.search.setFocus()

    def retranslate(self):
        """Re-apply every visible string; used at start and when the language changes."""
        self.settings_btn.setText(tr("Settings"))
        self.export_btn.setText(tr("Export list…"))
        self.export_menu.clear()
        for fmt in export.FORMATS:
            self.export_menu.addAction(tr(export.FORMAT_LABELS[fmt]), lambda _checked=False, f=fmt: self.export_flow(f))
        self.scan_btn.setText(tr("Check again"))
        self.cancel_all_btn.setText(tr("Cancel all"))
        self.search.setPlaceholderText(tr("Search apps"))
        self.sort_label.setText(tr("Sort by"))
        for i, (_key, label) in enumerate(UPDATE_SORTS):
            self.sort_combo.setItemText(i, tr(label))
        self.log_btn.setText(tr("Hide log") if self.log_view.isVisible() else tr("Show log"))
        self.banner_btn.setText(tr(elevation.RESTART_LABEL))
        self.banner_dismiss.setText(tr("Not now"))
        self.library.retranslate()
        self.discover_page.retranslate()
        if self.history_page is not None:
            self.history_page.retranslate()
        if self._hint_timer.isActive():     # a hint in the old language is not worth keeping
            self._hint_timer.stop()
            self._end_hint()
        if self.scanning or self._stale:
            self._set_subtitle(self._checking_text())
        if self.statuses:
            self.populate()
        else:
            self._update_counts()
            self._sync_stack()
        for uid in self.discover_statuses:
            if uid in self.job_state:
                self._mirror_discover(uid, self.job_state[uid])

    def _set_subtitle(self, text: str):
        """Every subtitle change goes through here: while a hint is showing, the new text waits for it to end."""
        if self._hint_timer.isActive():
            self._subtitle_after = text
        else:
            self.subtitle.setText(text)

    def _flash_hint(self, text: str):
        """Say why a click did nothing, for a few seconds, then bring back the subtitle that was (or became) current."""
        if not self._hint_timer.isActive():
            self._subtitle_after = self.subtitle.text()
        self.subtitle.setText(text)
        self._hint_timer.start()

    def _end_hint(self):
        self.subtitle.setText(self._subtitle_after)

    def _checking_text(self) -> str:
        if self._stale and self.last_scan:
            return tr("Last checked {time}, refreshing…", time=self._when_text(self.last_scan))
        return tr("Checking…")

    @staticmethod
    def _when_text(when: datetime) -> str:
        """The time of day, with the date added when it was not today."""
        today = when.date() == datetime.now().date()
        return num(when.strftime("%H:%M" if today else "%Y-%m-%d %H:%M"))

    # -- tabs ----------------------------------------------------------
    def show_tab(self, key: str, remember: bool = True):
        if key not in TABS:
            key = "updates"
        self.current_tab = key
        self.tab_buttons[key].setChecked(True)
        self.search.setVisible(key in SEARCHED)
        if key == "history":
            self._history_page()
        self._sync_stack()
        if key == "discover":
            self.discover_page.focus_search()
        if remember and self.settings.get("last_tab") != key:
            self.settings.set("last_tab", key)
            self._save_settings()

    def _history_page(self) -> HistoryPage:
        if self.history_page is None:
            self.history_page = HistoryPage()
            self.stack.addWidget(self.history_page)
        return self.history_page

    def _save_settings(self):
        try:
            self.settings.save()
        except OSError as e:
            log.warning("Could not save the settings: %s", e)

    # -- settings ------------------------------------------------------
    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        dialog.language_changed.connect(self.apply_language)
        dialog.theme_changed.connect(self.apply_theme)
        dialog.background_changed.connect(self._background_changed)
        dialog.history_cleared.connect(self._history_cleared)
        dialog.badges_changed.connect(self._badges_changed)
        dialog.backups_requested.connect(lambda: self.open_backups(dialog))
        dialog.exec()
        if dialog.sources_dirty:
            self.scanner.reset_github()
            self._update_counts()

    def apply_language(self, lang: str):
        self.settings.set("language", lang)
        self.settings.save()
        apply_qt_language(QApplication.instance(), lang)
        self.retranslate()

    def apply_theme(self, key: str):
        self.settings.set("theme", key)
        self.settings.save()
        apply_theme(QApplication.instance(), key)

    def _background_changed(self):
        if self.background is not None:
            self.background.settings_changed()

    def _history_cleared(self):
        if self.history_page is not None:
            self.history_page.refresh()

    def _badges_changed(self, on: bool):
        """Badges are computed from what the scan already fetched, so the switch applies at once."""
        for s in self.statuses.values():
            s.badges = []
            if on:
                try:
                    s.badges = list(badges.compute(s, self.scanner.cache))
                except Exception:
                    log.exception("badges for %s failed", s.app.name)
        self.populate()
        self._save_snapshot()

    def open_backups(self, parent=None):
        days = self.settings.get("cleanup_backup_days", 30)
        dialog = BackupsDialog(parent or self, self.settings, days if isinstance(days, int) and not isinstance(days, bool) else 30)
        dialog.restored.connect(self._record_restore)
        dialog.exec()

    # -- scanning ------------------------------------------------------
    def show_last_scan(self) -> bool:
        """Instant start: fill the window from the last saved scan before the new one finishes. False when there is
        none."""
        try:
            saved = scan_store.load_snapshot()
        except Exception:
            log.exception("Could not read the last scan")
            return False
        if not saved or self.statuses:
            return False
        try:
            statuses, taken = saved
            for s in statuses:
                scrub_stored_text(s)
            self.statuses = {s.uid: s for s in statuses}
            self.last_scan = self._snapshot_time(taken)
            self._stale = True
            self.populate()
            self._sync_discover_installed()
        except Exception:
            # A file on disk must never keep the window from opening: start from an empty list; the check fills it.
            log.exception("Could not show the last check")
            self.statuses, self.last_scan, self._stale = {}, None, False
            try:
                self.populate()
            except Exception:
                log.exception("Could not clear the list after the last check failed to show")
            return False
        log.info("Showing the last check (%d apps, %s)", len(statuses),
                 self.last_scan.strftime("%Y-%m-%d %H:%M") if self.last_scan else "time unknown")
        return True

    @staticmethod
    def _snapshot_time(taken) -> datetime | None:
        """When the saved check was made; None when the stored number is not a time Windows can represent."""
        try:
            return datetime.fromtimestamp(taken)
        except (OverflowError, OSError, ValueError, TypeError):
            return None

    def start_scan(self):
        if self.scanning:
            return
        # A check that cannot start now is not forgotten: the queue going idle runs it, and so does the uninstall
        # that is in the way once it has ended (a Discover install asks for one, so does the "refreshing" subtitle).
        if self.active_jobs:
            self._rescan_wanted = True
            return
        if self._uninstall_running():
            self._rescan_wanted = True
            if not self._rescan_timer.isActive():
                self._rescan_timer.start()
            return
        self._rescan_wanted = False
        self.removed.clear()
        self.scanning = True
        self.scan_btn.setEnabled(False)
        self.all_btn.setEnabled(False)
        self.scan_bar.setRange(0, 0)
        self.scan_bar.show()
        self._set_subtitle(self._checking_text())
        self._sync_stack()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _rescan_if_wanted(self):
        if self._rescan_wanted:
            self._rescan_wanted = False
            self.start_scan()       # puts the flag back when something is still in the way

    def _scan_worker(self):
        try:
            result = self.scanner.scan(lambda text, i, n: self.bus.scan_progress.emit(text, i, n))
        except Exception as e:
            log.exception("scan failed")
            self.bus.scan_failed.emit(str(e))
            return
        self.bus.scan_finished.emit(result)

    def _on_scan_progress(self, text: str, i: int, n: int):
        if self._stale:
            self._set_subtitle(self._checking_text())    # the old list stays on screen; the bar shows the progress
        else:
            self._set_subtitle(tr("{text} {i} of {n}", text=text, i=num(i), n=num(n)) if n else text)
        if n:
            self.scan_bar.setRange(0, n)
            self.scan_bar.setValue(i)
        else:
            self.scan_bar.setRange(0, 0)

    def _on_scan_finished(self, statuses):
        self.scanning = False
        self._stale = False
        self.scan_bar.hide()
        self.scan_btn.setEnabled(True)
        self.last_scan = datetime.now()
        self.statuses = {s.uid: s for s in statuses if s.uid not in self.removed}
        # Finished jobs' results belong to the old list; a job that is still queued or running keeps its row control.
        self.job_state = {uid: state for uid, state in self.job_state.items()
                          if uid in self.active_jobs or uid in self._looking_up}
        self.discover_statuses = {uid: s for uid, s in self.discover_statuses.items() if uid in self.active_jobs}
        self.populate()
        self._sync_discover_installed()
        log.info("Check finished: %d apps, %d updates", len(statuses), sum(1 for s in statuses if s.update))
        if self.background is not None:
            try:
                self.background.scan_finished()
            except Exception:
                log.exception("background.scan_finished failed")

    def _on_scan_failed(self, message: str):
        self.scanning = False
        self._stale = False
        self.scan_bar.hide()
        self.scan_btn.setEnabled(True)
        self._set_subtitle(tr("Check failed: {error}", error=message))

    def _sync_discover_installed(self):
        self.discover_page.set_installed_ids({s.winget_id for s in self.statuses.values() if s.winget_id})

    # -- snapshot ------------------------------------------------------
    def _save_snapshot(self):
        """What the next start shows at once. Not while a check runs: it saves its own result when it ends."""
        if not self.persist or self.scanning or not self.statuses or self.last_scan is None:
            return
        scan_store.save_snapshot(self._snapshot_statuses(), taken=self.last_scan.timestamp())

    def _snapshot_statuses(self) -> list[AppStatus]:
        """The statuses as the next start should see them. A rollback offer is a request of this session, never a
        pending update: while one is queued or running the original offer is saved in its place."""
        out = []
        for s in self.statuses.values():
            if s.update is not None and s.update.winget_mode == "rollback":
                update, fallbacks = self._rollback_original.get(s.uid, (None, []))
                if update is not None and update.winget_mode == "rollback":
                    update, fallbacks = None, []
                s = dataclasses.replace(s, update=update, fallbacks=list(fallbacks))
            out.append(s)
        return out

    def shutdown(self):
        """Save what the next start needs. Safe to call more than once."""
        self._save_snapshot()

    # -- lists ---------------------------------------------------------
    def populate(self):
        self.lists["ignored"].clear()
        self.rows = {}
        self.icon_queue = []
        self._update_rows = []
        shown, skipped_uids, ignored = [], set(), []
        for s in sorted(self.statuses.values(), key=lambda s: s.app.name.casefold()):
            ident = s.app.ident
            skipped = bool(s.update) and self.settings.skipped_version(ident) == s.update.version
            if self.settings.is_ignored(ident):
                ignored.append((s, skipped, True))
                continue
            shown.append(s)
            if skipped:
                skipped_uids.add(s.uid)
        self._fill_list("ignored", ignored)
        self._library_statuses, self._skipped_uids = shown, skipped_uids
        self.library.set_statuses(shown, skipped_uids, set())
        self._fill_updates()
        self._update_counts()
        self.apply_filter()
        self.icon_timer.start()

    def _updates_sort(self) -> str:
        key = self.settings.get("updates_sort", "name")
        return key if key in dict(UPDATE_SORTS) else "name"

    def _sorted_for_updates(self) -> list[AppStatus]:
        # Rows with a job stay in Updates until the next check, so results remain visible.
        wanted = [s for s in self._library_statuses
                  if (s.update and s.uid not in self._skipped_uids) or s.uid in self.job_state]
        by_name = lambda s: s.app.name.casefold()    # noqa: E731
        sort = self._updates_sort()
        if sort == "size":      # an unknown size (0) sorts after every known one
            return sorted(wanted, key=lambda s: (not self._size_of(s), -self._size_of(s), by_name(s)))
        if sort == "publisher":
            return sorted(wanted, key=lambda s: (not s.app.publisher, s.app.publisher.casefold(), by_name(s)))
        return sorted(wanted, key=by_name)

    @staticmethod
    def _size_of(s: AppStatus) -> int:
        size = getattr(s.update, "size", 0) if s.update else 0
        return size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0

    def _fill_updates(self):
        """(Re)build the Updates list only; the other lists keep their rows."""
        for row in self._update_rows:
            mine = self.rows.get(row.uid, [])
            if row in mine:
                mine.remove(row)
            if not mine:
                self.rows.pop(row.uid, None)
        self._update_rows = []
        self.lists["updates"].clear()
        self._fill_list("updates", [(s, False, False) for s in self._sorted_for_updates()])

    def _on_sort_changed(self, _index: int = 0):
        key = self.sort_combo.currentData()
        if key == self._updates_sort() and self.settings.get("updates_sort") == key:
            return
        self.settings.set("updates_sort", key)
        self._save_settings()
        self._fill_updates()
        self.apply_filter()
        self._update_counts()
        self.icon_timer.start()

    def _make_row(self, s: AppStatus, skipped: bool, ignored: bool) -> AppRow:
        """The one place rows are made (the All apps page calls it too): signals, the registry the job states are
        replayed from, the icon queue, the wave-2 chips and the state of a job that is already running."""
        row = AppRow(s, skipped, ignored)
        row.update_clicked.connect(self.request_update)
        row.cancel_clicked.connect(self.cancel_job)
        row.menu_action.connect(self.on_menu)
        row.set_badges(s.badges)
        row.set_pin(s.pin)
        row.set_auto_update(s.app.ident in self._auto_idents())
        self.rows.setdefault(s.uid, []).append(row)
        self.icon_queue.append(row)
        if s.uid in self.job_state:
            self._show_state(row, self.job_state[s.uid])
        return row

    def _fill_list(self, tab: str, entries: list[tuple[AppStatus, bool, bool]]):
        """One item per app, in order. A row is a dozen widgets and a list of hundreds took seconds to build, so only
        the first rows are built here and the rest when they come near the viewport (_realize). Every row has the
        height the first one measured."""
        lw = self.lists[tab]
        deferred = self._deferred[tab] = {}
        height = 0
        for s, skipped, ignored in entries:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, s.uid)
            if height:
                item.setSizeHint(QSize(0, height))
                lw.addItem(item)
                deferred[s.uid] = (s, skipped, ignored)
                continue
            row = self._make_row(s, skipped, ignored)
            height = row.sizeHint().height()
            item.setSizeHint(QSize(0, height))
            lw.addItem(item)
            self._attach_row(tab, item, row)

    def _attach_row(self, tab: str, item: QListWidgetItem, row: AppRow):
        if tab == "updates":
            self._update_rows.append(row)
        self.lists[tab].setItemWidget(item, row)

    def _realize(self, tab: str, *_):
        """Build the rows that are near the viewport (a scroll, a resize, a filter) and not built yet. Adding a row can
        resize the viewport (a scroll bar appears), which asks again: that second call waits for the first to finish."""
        self._realize_again.add(tab)
        if self._realizing:
            return
        self._realizing = True
        try:
            while self._realize_again:
                self._realize_tab(self._realize_again.pop())
        finally:
            self._realizing = False

    def _realize_tab(self, tab: str):
        deferred = self._deferred.get(tab)
        if not deferred:
            return
        lw = self.lists[tab]
        bottom = lw.viewport().height() + LAZY_REACH
        made = 0
        for i in range(lw.count()):
            item = lw.item(i)
            args = deferred.get(item.data(Qt.UserRole))
            if args is None or item.isHidden():
                continue
            if i >= LAZY_ROWS:
                rect = lw.visualItemRect(item)
                if rect.top() > bottom:
                    break
                if rect.bottom() < -LAZY_REACH:
                    continue
            deferred.pop(item.data(Qt.UserRole), None)
            self._attach_row(tab, item, self._make_row(*args))
            made += 1
        if made:
            self.icon_timer.start()

    def _live_rows(self, uid: str) -> list[AppRow]:
        """The rows of an app that still exist: a page may rebuild its rows (a new sort) without telling the registry."""
        rows = self.rows.get(uid)
        if not rows:
            return []
        live = [r for r in rows if shiboken6.isValid(r)]
        if len(live) != len(rows):
            if live:
                self.rows[uid] = live
            else:
                del self.rows[uid]
        return live

    def _auto_idents(self) -> list:
        idents = self.settings.get("auto_update", [])
        return idents if isinstance(idents, list) else []

    def _load_icons(self):
        for _ in range(10):
            if not self.icon_queue:
                self.icon_timer.stop()
                return
            row = self.icon_queue.pop(0)
            if shiboken6.isValid(row):
                row.set_icon(self._icon_for(row.status.app))

    def _icon_for(self, app):
        key = app.icon_path or f"@{app.name}"
        if key not in self.icon_cache:
            pixmap = None
            if app.icon_path:
                icon = QIcon(app.icon_path) if app.icon_path.lower().endswith(".ico") \
                    else self.icon_provider.icon(QFileInfo(app.icon_path))
                if not icon.isNull():
                    pixmap = icon.pixmap(36, 36)
            self.icon_cache[key] = pixmap if pixmap and not pixmap.isNull() else letter_avatar(app.name)
        return self.icon_cache[key]

    def _status_of(self, uid: str) -> AppStatus | None:
        return self.statuses.get(uid) or self.discover_statuses.get(uid)

    # -- API for the background feature, the single-instance hand-over and the tray --------
    def pending_updates(self) -> list[str]:
        return self._pending_updates()

    def is_busy(self) -> bool:
        return bool(self.scanning or self.active_jobs or uninstalls_running())

    def show_and_raise(self):
        """Bring the window to the front, also from the tray or from a second launch."""
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        """Quit for good: the tray menu, the restart as administrator, the last close of a window that was hidden."""
        if not self._confirm_leave():
            return
        self.shutdown()
        QApplication.quit()

    def _confirm_leave(self) -> bool:
        """True when nothing is running, or the user agrees to leave it. The question is asked once: a close that
        was confirmed is followed by a quit that must not ask again."""
        if self._leave_ok or not (self.active_jobs or uninstalls_running()):
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Upstream")
        box.setText(tr("Updates are still running. The install that already started will finish, "
                       "but the rest of the queue is cancelled.") if self.active_jobs
                    else tr("An uninstall or cleanup is still running. Closing Upstream does not stop an uninstaller, "
                            "and Upstream will no longer show how it went. A cleanup in progress would stop where it is."))
        leave = box.addButton(tr("Close"), QMessageBox.AcceptRole)
        box.addButton(tr("Stay"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not leave:
            return False
        self.cancel_all_jobs()
        self._leave_ok = True
        return True

    def _pending_updates(self) -> list[str]:
        out = []
        for i in range(self.lists["updates"].count()):
            uid = self.lists["updates"].item(i).data(Qt.UserRole)
            s = self.statuses.get(uid)
            # A rollback is something the user asked for on one app, never part of "Update all" or an automatic run.
            if s and s.update and s.update.winget_mode != "rollback" and uid not in self.active_jobs                     and uid not in self._looking_up:
                out.append(uid)
        return out

    def _update_counts(self):
        counts = {"updates": self.lists["updates"].count(), "all": len(self._library_statuses),
                  "ignored": self.lists["ignored"].count()}
        for key, text in TABS.items():
            count = counts.get(key, 0) if key in COUNTED else 0
            label = tr(text)
            self.tab_buttons[key].setText(f"{label}  {num(count)}" if count else label)
        pending = self._pending_updates()
        self.cancel_all_btn.setEnabled(bool(self.active_jobs))
        self.all_btn.setEnabled(bool(pending) and not self.scanning)
        self._all_texts = self._update_all_texts([self.statuses[u] for u in pending])
        self._fit_header()
        self.export_btn.setEnabled(bool(self.statuses))

        if self._stale:
            self._set_subtitle(self._checking_text())
        elif not self.scanning and self.last_scan:
            if self.active_jobs:
                status = trn("{n} update in progress", "{n} updates in progress", len(self.active_jobs))
            elif pending:
                status = trn("{n} update ready", "{n} updates ready", len(pending))
            else:
                status = tr("Everything is up to date")
            status += tr(" · last checked {time}", time=self._when_text(self.last_scan))
            self._set_subtitle(status)

        gh = self.scanner.github_if_ready
        parts = [trn("{n} installed app", "{n} installed apps", len(self.statuses))] if self.statuses else []
        if self._elevated:
            parts.append(tr("Running as administrator"))
        if gh:  # until the next check builds the client, the token state is unknown
            parts.append(tr("GitHub with token") if gh.token else tr("GitHub without token (60 requests/hour)"))
        text = " · ".join(parts)
        if gh and gh.remaining:
            text += tr(" · {n} requests left", n=num(gh.remaining))
        self.footer.setText(text)
        self._update_banner(pending)

    @staticmethod
    def _download_size(statuses: list[AppStatus]) -> tuple[int, bool]:
        """(bytes the updates download, whether every size is known). Sizes are 0 where the source did not say."""
        known = [MainWindow._size_of(s) for s in statuses if MainWindow._size_of(s)]
        return sum(known), len(known) == len(statuses)

    def _update_all_text(self, pending: list[AppStatus]) -> str:
        return self._update_all_texts(pending)[0]

    def _update_all_texts(self, pending: list[AppStatus]) -> tuple[str, str, str]:
        """(the button text, the short text for a narrow header, the tooltip that carries what the short one leaves out)."""
        if not pending:
            return (tr("Update all"),) * 2 + ("",)
        short = tr("Update all ({n})", n=num(len(pending)))
        total, complete = self._download_size(pending)
        if not total:
            return short, short, ""
        size = num(details.format_size(total))
        if complete:
            return (tr("Update all ({n}) · {size}", n=num(len(pending)), size=size), short,
                    tr("Download size: {size}", size=size))
        return (tr("Update all ({n}) · at least {size}", n=num(len(pending)), size=size), short,
                tr("Download size: at least {size}", size=size))

    def _fit_header(self):
        """Nothing in the header is clipped at any width the window can have. The subtitle elides; Update all drops the
        download size (into its tooltip) when the header cannot keep it and still leave the subtitle some room; and the
        window's minimum width is what the layout really needs, with the short Update all, but never below MIN_WIDTH."""
        full, short, tip = self._all_texts
        if not short:
            return
        btn, lay = self.all_btn, self.centralWidget().layout()
        lay.invalidate()
        self._head.invalidate()
        fm, now = btn.fontMetrics(), btn.sizeHint().width()
        chrome = now - fm.horizontalAdvance(btn.text())

        def head_with(text: str) -> int:     # the header's least width were Update all to read `text`
            return self._head.minimumSize().width() - now + chrome + fm.horizontalAdvance(text)

        margins = lay.contentsMargins()
        sides = margins.left() + margins.right()
        others = max((lay.itemAt(i).minimumSize().width() for i in range(lay.count())
                      if lay.itemAt(i).layout() is not self._head), default=0)
        needed = max(MIN_WIDTH, sides + max(others, head_with(short)))
        if needed != self.minimumWidth():
            self.setMinimumWidth(needed)
        fits = full == short or self.width() - sides >= head_with(full) + SUBTITLE_ROOM
        btn.setText(full if fits else short)
        btn.setToolTip("" if fits else tip)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_header()

    def _sync_stack(self):
        key = self.current_tab
        if key == "discover":
            self.stack.setCurrentWidget(self.discover_page)
            return
        if key == "history":
            self.stack.setCurrentWidget(self._history_page())
            return
        if key == "all":
            if self.statuses:    # the page keeps its toolbar even when a filter leaves no rows
                self.stack.setCurrentWidget(self.library)
                return
        else:
            lw = self.lists[key]
            if any(not lw.item(i).isHidden() for i in range(lw.count())):
                self.stack.setCurrentWidget(self.updates_page if key == "updates" else lw)
                return
        if not self.statuses:
            title, text = tr("Checking your apps…"), tr(
                "The first check takes a little longer: each app's official source is found once and remembered.")
        elif self.search.text().strip():
            title, text = tr("Nothing matches"), ""
        elif key == "updates":
            title, text = tr("Everything is up to date"), tr(
                "If an app's source wasn't found, set it from All apps → ⋯ → Update source.")
        elif key == "ignored":
            title, text = tr("Nothing here"), tr("Apps you ignore are kept here.")
        else:
            title, text = tr("No apps found"), ""
        self.empty_title.setText(title)
        self.empty_text.setText(text)
        self.stack.setCurrentWidget(self.empty)

    def apply_filter(self):
        text = self.search.text()
        q = text.strip().casefold()
        self.library.set_filter_text(text)
        for lw in self.lists.values():
            for i in range(lw.count()):
                item = lw.item(i)
                s = self.statuses.get(item.data(Qt.UserRole))
                hay = f"{s.app.name} {s.app.publisher}".casefold() if s else ""
                item.setHidden(bool(q) and q not in hay)
        for tab in self._deferred:
            self._realize(tab)
        self._sync_stack()

    def toggle_log(self):
        visible = not self.log_view.isVisible()
        self.log_view.setVisible(visible)
        self.log_btn.setText(tr("Hide log") if visible else tr("Show log"))

    # -- administrator banner ----------------------------------------------
    def _update_banner(self, pending: list[str] | None = None):
        uids = self._pending_updates() if pending is None else pending
        count = 0
        if not self._banner_dismissed and uids:
            try:
                count = elevation.machine_installs_pending([self.statuses[u] for u in uids if u in self.statuses])
                if count and not elevation.can_relaunch_elevated():
                    count = 0
            except Exception:
                log.exception("Could not tell whether updates need administrator rights")
        self.banner_box.setVisible(count > 0)
        if not count:
            self._banner_note = ""
            return
        busy = self.is_busy()
        self.banner_text.setText(elevation.banner_text(count))
        self.banner_btn.setEnabled(not busy)
        self.banner_btn.setToolTip(tr("Wait until the current work has finished.") if busy else "")
        self.banner_note.setText(tr(self._banner_note) if self._banner_note else "")
        self.banner_note.setVisible(bool(self._banner_note))

    def dismiss_banner(self):
        self._banner_dismissed = True
        self.banner_box.hide()

    def restart_as_admin(self):
        """One restart as administrator replaces a UAC prompt per update. Nothing may be running: the new copy
        takes over once this one has exited."""
        if self.is_busy():
            return
        self._save_snapshot()
        if elevation.relaunch_elevated():
            self.quit_app()
        else:
            self._banner_note = N_("Administrator permission (UAC) was declined.")
            self._update_banner()

    # -- export --------------------------------------------------------
    def export_flow(self, fmt: str, uids: list[str] | None = None):
        """Ask where to save the list of installed apps (or of the apps in `uids`) and write it."""
        pool = list(self.statuses.values()) if uids is None else [self.statuses[u] for u in uids if u in self.statuses]
        report = export.export_report(pool, fmt)
        if not report.can_write:
            self._inform(report.summary())
            return
        folder = self.settings.get("export_dir") or ""
        start = str(Path(folder) / export.default_name(fmt)) if folder and os.path.isdir(folder) else export.default_name(fmt)
        path, _ = QFileDialog.getSaveFileName(self, tr("Export list…"), start, export.file_filter(fmt))
        if not path:
            return
        try:
            export.export_apps(pool, Path(path), fmt)
        except (OSError, ValueError) as e:
            log.warning("Export to %s failed: %s", path, e)
            self._inform(tr("Couldn't save the list: {error}", error=str(e)), warning=True)
            return
        self.settings.set("export_dir", str(Path(path).parent))
        self._save_settings()
        log.info("Exported %d apps (%s) to %s", report.written, fmt, path)
        self._inform(report.summary())

    def batch_export(self, uids: list[str]):
        fmt = self._choose_format()
        if fmt:
            self.export_flow(fmt, uids)

    def _choose_format(self) -> str | None:
        menu = QMenu(self)
        actions = {menu.addAction(tr(export.FORMAT_LABELS[fmt])): fmt for fmt in export.FORMATS}
        chosen = menu.exec(QCursor.pos())
        return actions.get(chosen)

    def _inform(self, text: str, detail: str = "", warning: bool = False):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if warning else QMessageBox.Information)
        box.setWindowTitle("Upstream")
        box.setText(text)
        if detail:
            box.setInformativeText(detail)
        box.addButton(QMessageBox.Ok)
        box.exec()

    # -- updates -------------------------------------------------------
    def request_update(self, uid: str, bulk: bool = False, auto: bool = False) -> bool:
        """Queue an update. False when it could not be queued now (a check or an uninstall is running, ...).
        `auto` is an automatic-update rule asking: the job never asks the user anything."""
        s = self._status_of(uid)
        if not s or not s.update or uid in self.active_jobs or uid in self._looking_up:
            return False
        # A check replaces the list and the rows' job states; an update started meanwhile would lose its Cancel control.
        # The last saved check is on screen while the new one runs, so a click on its Update buttons lands here.
        if self.scanning:
            if not (bulk or auto):
                self._flash_hint(tr("Wait until the check finishes, then try again."))
            return False
        if self._uninstall_running():
            return False
        upd = s.update
        if upd.source == "site" and not upd.download_url and not s.fallbacks:
            QDesktopServices.openUrl(QUrl(upd.page_url))
            return False
        self.active_jobs.add(uid)
        token = self.cancels[uid] = CancelToken()
        if bulk:
            self.bulk_jobs.add(uid)
        if auto:
            self.auto_jobs.add(uid)
        self._set_state(uid, ("queued",))
        self.jobs.put((uid, token))
        self._update_counts()
        return True

    def update_all(self):
        for uid in self._pending_updates():
            s = self.statuses[uid]
            if s.update.source == "site" and not s.update.download_url and not s.fallbacks:
                continue
            self.request_update(uid, bulk=True)

    def cancel_job(self, uid: str):
        """Take a queued update out of the queue, or tell the running one to stop."""
        token = self.cancels.get(uid)
        if token is None or token.is_set:
            return
        with self.queue_lock:
            token.cancel()
            queued = uid not in self.taken
        s = self._status_of(uid)
        log.info("Cancelling %s (%s)", s.app.name if s else uid, "queued" if queued else "running")
        if queued:
            # The worker may be busy with another app for a long time; the row must not wait for it.
            # When it reaches this entry it sees the cancelled token and skips it.
            self._on_job_finished(uid, False, N_("Cancelled."), {}, "")
        else:
            self._set_state(uid, ("cancelling",))

    def cancel_all_jobs(self):
        for uid in list(self.cancels):
            self.cancel_job(uid)

    def _job_loop(self):
        """The one worker: takes queued updates in order. Whatever goes wrong with one, it reports that update
        as failed and carries on; a dead worker would leave every later job 'Queued' for good."""
        while True:
            uid, token = self.jobs.get()
            try:
                outcome = self._run_job(uid, token)
            except Exception as e:
                log.exception("update job failed")
                outcome = (False, "{error}", {"error": str(e)}, "")
            if outcome:
                self.bus.job_finished.emit(uid, *outcome)

    def _run_job(self, uid: str, token: CancelToken) -> tuple | None:
        """(ok, template, values, version) of one queued update; None when it was cancelled while queued
        (cancel_job already updated the row)."""
        with self.queue_lock:
            if token.is_set:
                return None
            self.taken.add(uid)
        status = self._status_of(uid)
        if status is None or status.update is None:
            return False, "No update pending anymore", {}, ""
        name = status.app.name
        ctx = UpdateContext(self.settings, token, partial(self._ask_from_worker, token), uid in self.bulk_jobs,
                            auto=uid in self.auto_jobs)
        log.info("Updating %s from %s (%s)", name, status.update.source, status.update.origin)
        ok, template, values, version = self._run_update(uid, status, token, ctx)
        log.info("%s: %s", name, english(template, values))
        return ok, template, values, version or ""

    def _run_update(self, uid: str, status: AppStatus, token: CancelToken, ctx: UpdateContext) -> tuple:
        """Run one update on its own thread. Once the user cancels, the update gets WIND_DOWN seconds to
        notice; if it is still busy (an installer that is already running) the queue moves on and it
        finishes in the background, silently."""
        result: list[tuple] = []

        def progress(template: str, pct: int, **values):
            if not token.is_set:
                self.bus.job_progress.emit(uid, template, values, pct)

        def work():
            try:
                token.check()   # cancelled between being picked up and getting here: run nothing at all
                result.append(self.updater.run(status, progress, ctx))
            except Cancelled as e:
                result.append((False, e.template, e.values, status.app.version))
            except Exception as e:
                log.exception("update failed")
                result.append((False, "{error}", {"error": str(e)}, ""))

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        give_up = None
        while worker.is_alive():
            worker.join(0.1)
            if token.is_set:
                give_up = give_up or time.monotonic() + WIND_DOWN
                if time.monotonic() >= give_up:
                    break
        if result:
            return result[0]
        return False, N_("Stopped waiting for the installer. It may still be running."), {}, status.app.version

    def _set_state(self, uid: str, state: tuple):
        self.job_state[uid] = state
        for row in self._live_rows(uid):
            self._show_state(row, state)
        if uid in self.discover_statuses:
            self._mirror_discover(uid, state)

    def _show_state(self, row: AppRow, state: tuple):
        if state[0] == "queued":
            row.set_queued()
        elif state[0] == "progress":
            _, template, values, pct = state
            # Only updates can be cancelled; a recheck of one row shows the same bar but has nothing to stop.
            cancel = "" if row.uid not in self.cancels else "wait" if template in INSTALL_PHASES else "cancel"
            row.set_progress(render(template, values), pct, cancel)
        elif state[0] == "cancelling":
            row.set_cancelling()
        elif state[0] == "cancelled":
            row.set_cancelled(render(state[1], state[2]))
        else:
            _, ok, template, values, version = state
            row.set_result(ok, render(template, values), version)

    def _on_job_progress(self, uid: str, template: str, values: dict, pct: int):
        token = self.cancels.get(uid)
        if token is None or token.is_set:
            return  # finished, or being cancelled: a late message must not bring the bar back
        self._set_state(uid, ("progress", template, values, pct))

    def _ask_from_worker(self, token: CancelToken, question: Question) -> bool:
        """Runs on an update's worker thread: show the question in the window and wait for the reply."""
        answer = _Answer(token)
        self.bus.question.emit(question, answer)
        while not answer.event.wait(0.25):
            if token.is_set:
                return False
        return answer.value

    def _on_question(self, question: Question, answer: _Answer):
        try:
            if answer.token.is_set:
                return      # cancelled while the question was on its way: the worker has stopped waiting
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning if question.danger else QMessageBox.Question)
            box.setWindowTitle(tr("Upstream"))
            box.setText(render(question.template, question.values))
            if question.detail:
                box.setInformativeText(render(question.detail, question.values))
            yes = box.addButton(tr(question.yes), QMessageBox.AcceptRole)
            no = box.addButton(tr(question.no), QMessageBox.RejectRole)
            box.setDefaultButton(no if question.danger else yes)
            box.exec()
            answer.value = box.clickedButton() is yes
        finally:
            answer.event.set()  # whatever went wrong above, the worker must not be left polling for a dialog

    def _on_job_finished(self, uid: str, ok: bool, template: str, values: dict, version: str):
        s = self._status_of(uid)
        before = (s.app.version, s.update) if s else None   # the success below clears the offer
        self.active_jobs.discard(uid)
        token = self.cancels.pop(uid, None)
        self.bulk_jobs.discard(uid)
        auto = uid in self.auto_jobs
        self.auto_jobs.discard(uid)
        with self.queue_lock:
            self.taken.discard(uid)
        cancelled = not ok and token is not None and token.is_set
        original = self._rollback_original.pop(uid, None)
        hold = self._rollback_hold.pop(uid, None)
        if ok and s:
            s.app.version = version or s.app.version
            s.update, s.fallbacks = None, []
        self._record_job(s, before, ok, cancelled, template, values, version)
        if auto and self.background is not None:
            try:
                # Lets Background forget a version it could not try (battery, metered) so the next check tries again.
                self.background.auto_finished(uid, ok, template)
            except Exception:
                log.exception("Background.auto_finished failed")
        if uid in self.discover_statuses and ok:
            self._rescan_wanted = True
        rolled_back_in_vain = not ok and original is not None and s is not None
        if rolled_back_in_vain:
            # A rollback that did not happen (called off, declined, failed): the row goes back to offering what it
            # offered before, not the older version the rollback stood in for.
            s.update, s.fallbacks = original
            self.job_state.pop(uid, None)
            for row in self._live_rows(uid):
                row.refresh(s, row.skipped, row.ignored)
        else:
            self._set_state(uid, ("cancelled", template, values) if cancelled
                            else ("result", ok, template, values, version))
        if ok and hold and s:
            self._hold_after_rollback(s, version or hold)
        self._update_counts()
        if not self.active_jobs:
            self._queue_idle()
            if not cancelled and not auto:
                QApplication.alert(self)
        if rolled_back_in_vain and not cancelled and template not in USER_STOPPED:
            self._inform(tr("{name} could not be rolled back.", name=s.app.name), render(template, values), warning=True)

    def _hold_after_rollback(self, s: AppStatus, version: str):
        """The user asked to keep the app on the older version: without a pin the next check offers the newer one
        again (and Background installs it when the app updates automatically)."""
        try:
            pins.set_pin(self.settings, s.app.ident, "hold", version)
        except OSError as e:
            log.warning("Could not save the pin of %s: %s", s.app.name, e)
            self._inform(tr("Couldn't save the settings: {error}", error=str(e)), warning=True)
        else:
            log.info("Pin for %s: hold at %s (after a rollback)", s.app.name, version)
        self._recheck(s)

    def _queue_idle(self):
        self._save_snapshot()
        if self._rescan_wanted or self._stale:
            self._rescan_wanted = False
            QTimer.singleShot(0, self.start_scan)       # refused again while an uninstall runs: it is remembered

    # -- history -------------------------------------------------------
    def _record(self, *args, **kwargs):
        if self.persist:
            history.record(*args, **kwargs)

    def _record_job(self, s: AppStatus | None, before: tuple | None, ok: bool, cancelled: bool,
                    template: str, values: dict, version: str):
        """One history entry for an update, install or rollback that ran. A job the user stopped is not recorded."""
        if s is None or before is None or before[1] is None or cancelled or template in USER_STOPPED:
            return
        installed, upd = before
        kind = "rollback" if upd.winget_mode == "rollback" else "install" if not installed else "update"
        self._record(kind, s.app.name, installed, (version or upd.version) if ok else upd.version, ok, template,
                     values, upd.source)

    def _record_uninstall(self, dialog, closing: bool = False):
        """Called after an uninstaller stopped (a deferred call: the dialog stores its outcome just after the signal)
        and once more when the dialog was closed. What was written is remembered on the dialog itself: one entry per
        app for a batch, one per final outcome for a single app."""
        seen = getattr(dialog, "_history_seen", None)
        if seen is None:
            seen = set()
            try:
                dialog._history_seen = seen
            except AttributeError:
                pass
        jobs = getattr(dialog, "jobs", None)
        if jobs is None:    # the single-app dialog
            outcome, template, values = uninstall_outcome(dialog)
            if not outcome and getattr(dialog, "was_removed", False):
                outcome = "removed"     # the dialog was closed while it ran: only Windows' answer is known
            if outcome == "still_listed" and not closing:
                return      # "Check again" can still turn it into removed: only the answer the user leaves with counts
            if outcome in seen or outcome not in UNINSTALL_DONE + UNINSTALL_FAILED:
                return
            seen.add(outcome)
            self._record_uninstall_result(dialog.app, outcome, template, values)
            return
        for i, job in enumerate(jobs):
            if i not in seen and job.outcome in UNINSTALL_DONE + UNINSTALL_FAILED:
                seen.add(i)
                self._record_uninstall_result(job.app, job.outcome, "", {})

    def _record_uninstall_result(self, app, outcome: str, template: str, values: dict):
        if outcome not in UNINSTALL_DONE + UNINSTALL_FAILED:
            return      # cancelled, busy, skipped: nothing was removed and nothing went wrong
        if not template and outcome in RESULT_TITLES:
            template = RESULT_TITLES[outcome][0]
        self._record("uninstall", app.name, app.version, "", outcome in UNINSTALL_DONE, template, values)

    def _record_cleanup(self, name: str, result):
        """After the user removed leftovers (cleanup.CleanupResult)."""
        removed = result.removed
        if not removed and not result.failed:
            return      # nothing was asked of it, or it was stopped before the first item
        template = (N_("Removed {n} item") if removed == 1 else N_("Removed {n} items")) if removed \
            else N_("Nothing was removed")
        self._record("cleanup", name, ok=not result.failed, template=template, values={"n": removed})

    def _record_restore(self, name: str, result):
        """After the user undid a cleanup (cleanup.RestoreResult)."""
        restored = result.restored
        if not restored and not result.failed:
            return
        template = (N_("Restored {n} item") if restored == 1 else N_("Restored {n} items")) if restored \
            else N_("Nothing was restored")
        self._record("restore", name, ok=not result.failed, template=template, values={"n": restored})

    # -- discover ------------------------------------------------------
    def _on_discover_install(self, pkg):
        s = discover.status_for(pkg)
        if s.uid in self.active_jobs:
            return
        self.discover_statuses[s.uid] = s
        if not self.request_update(s.uid):
            self.discover_statuses.pop(s.uid, None)
            self.discover_page.set_result(pkg.id, False,
                                          tr("Wait until the current check or uninstall has finished, then try again."))

    def _on_discover_cancel(self, pkg_id: str):
        self.cancel_job("WINGET\\NEW\\" + pkg_id)

    def _mirror_discover(self, uid: str, state: tuple):
        """Show a queued or running Discover install on its row of the Discover page."""
        s = self.discover_statuses.get(uid)
        if s is None or not s.winget_id:
            return
        page, pkg_id = self.discover_page, s.winget_id
        if state[0] == "queued":
            page.set_queued(pkg_id)
        elif state[0] == "progress":
            _, template, values, pct = state
            cancel = "" if uid not in self.cancels else "wait" if template in INSTALL_PHASES else "cancel"
            page.set_state(pkg_id, render(template, values), pct, cancel)
        elif state[0] == "cancelling":
            page.set_cancelling(pkg_id)
        elif state[0] == "cancelled":
            page.set_cancelled(pkg_id, render(state[1], state[2]))
        else:
            _, ok, template, values, _version = state
            page.set_result(pkg_id, ok, render(template, values))

    # -- row menu ------------------------------------------------------
    def on_menu(self, uid: str, key: str):
        s = self.statuses.get(uid)
        if not s:
            return
        ident = s.app.ident
        if key == "notes" and s.update and s.update.page_url:
            QDesktopServices.openUrl(QUrl(s.update.page_url))
        elif key == "repo" and s.repo:
            QDesktopServices.openUrl(QUrl(f"https://github.com/{s.repo}"))
        elif key == "folder":
            os.startfile(s.app.install_location)
        elif key == "skip" and s.update:
            self.settings.skip_version(ident, s.update.version)
            self.populate()
        elif key == "unskip":
            self.settings.skip_version(ident, None)
            self.populate()
        elif key == "ignore":
            now_ignored = not self.settings.is_ignored(ident)
            self.settings.set_ignored(ident, now_ignored)
            if now_ignored or s.uid in self.active_jobs:
                self.populate()     # a running update has nothing to recheck; the row only changes tab
            else:
                self._recheck(s)
        elif key == "source":
            self.edit_source(s)
        elif key == "uninstall":
            self.uninstall(s)
        elif key == "details":
            self.show_details(s)
        elif key == "pin":
            self.edit_pin(s)
        elif key == "unpin":
            self.change_pin(s, None)
        elif key == "rollback":
            self.start_rollback(s)
        elif key == "auto":
            self.toggle_auto_update(s)

    def edit_source(self, s: AppStatus):
        dialog = SourceDialog(s, self.settings.override(s.app.ident), self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.settings.set_override(s.app.ident, dialog.result_value or None)
        log.info("Source override for %s: %s", s.app.name, dialog.result_value or "cleared")
        self._recheck(s)

    def show_details(self, s: AppStatus):
        dialog = DetailsDialog(s, self, icon=self._icon_for(s.app), updates_running=self.is_busy)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.show()

    def toggle_auto_update(self, s: AppStatus):
        ident = s.app.ident
        idents = list(self._auto_idents())
        on = ident not in idents
        if on:
            idents.append(ident)
        else:
            idents.remove(ident)
        self.settings.set("auto_update", idents)
        self._save_settings()
        for other in self.statuses.values():
            if other.app.ident == ident:
                for row in self._live_rows(other.uid):
                    row.set_auto_update(on)
        log.info("Automatic updates for %s: %s", s.app.name, "on" if on else "off")
        self._background_changed()

    # -- pins and rollback ---------------------------------------------
    def edit_pin(self, s: AppStatus):
        dialog = PinDialog(s, self)
        if dialog.exec() == QDialog.Accepted:
            self.change_pin(s, dialog.mode)

    def change_pin(self, s: AppStatus, mode: str | None):
        try:
            pins.set_pin(self.settings, s.app.ident, mode, s.app.version)
        except OSError as e:
            log.warning("Could not save the pin of %s: %s", s.app.name, e)
            self._inform(tr("Couldn't save the settings: {error}", error=str(e)), warning=True)
            return
        log.info("Pin for %s: %s", s.app.name, mode or "removed")
        self._recheck(s)

    def start_rollback(self, s: AppStatus):
        """Look up the older versions winget has (it takes a few seconds), then let the user pick one."""
        if not s.winget_id or s.uid in self.active_jobs or s.uid in self._looking_up or self.scanning                 or self._uninstall_running():
            return
        self._looking_up.add(s.uid)     # until it answers, Update all and the background leave the app alone
        self._set_state(s.uid, ("progress", LOOKING_UP, {}, -1))

        def work():
            try:
                found = pins.available_versions(s.winget_id)
            except Exception:
                log.exception("could not list the versions of %s", s.winget_id)
                found = []
            self.bus.versions_found.emit(s.uid, found)

        threading.Thread(target=work, daemon=True).start()

    def _on_versions_found(self, uid: str, found: list):
        self._looking_up.discard(uid)
        if uid not in self.active_jobs:     # a job queued meanwhile owns the row's state now
            self.job_state.pop(uid, None)
        self.populate()     # the row stops showing the lookup
        s = self.statuses.get(uid)
        if s is None or uid in self.active_jobs:
            return
        older = older_versions(s.app.version, found)
        if not older:
            self._inform(tr("No older version of {name} is available through winget.", name=s.app.name))
            return
        dialog = RollbackDialog(s, older, self)
        if dialog.exec() == QDialog.Accepted:
            self.roll_back(s, dialog.version)

    def roll_back(self, s: AppStatus, version: str):
        """Install `version` over the current one, through the normal queue and without trying other sources.
        The picker and the confirmation are modal and a check can finish behind them, replacing every status: the
        app is looked up again by uid, so the rollback lands on the status the queue will run."""
        cur = self.statuses.get(s.uid)
        if cur is None:
            return
        proceed, hold = self._confirm_rollback(cur, version)
        if not proceed:
            return
        cur = self.statuses.get(s.uid)
        if cur is None:
            return
        update = pins.rollback_update(cur, version)
        if update is None:
            self._inform(tr("winget doesn't know this app, so it can't be rolled back."))
            return
        self._rollback_original[cur.uid] = (cur.update, cur.fallbacks)
        if hold:
            self._rollback_hold[cur.uid] = version
        cur.update, cur.fallbacks = update, []
        if not self.request_update(cur.uid):
            cur.update, cur.fallbacks = self._rollback_original.pop(cur.uid)
            self._rollback_hold.pop(cur.uid, None)

    def _confirm_rollback(self, s: AppStatus, version: str) -> tuple[bool, bool]:
        """(go on, also hold the app at `version`). Without a hold the next check offers the newer version again, and
        an app that updates automatically gets it back."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Upstream")
        box.setText(tr("Roll back {name} to {version}?", name=s.app.name, version=version))
        box.setInformativeText(
            tr("{name} {installed} will be replaced by the older version {version}. Upstream asks winget to do it; "
               "if the app can't be downgraded, nothing changes.", name=s.app.name, installed=s.app.version,
               version=version))
        hold = QCheckBox(tr("Also hold this app at {version} so it is not updated again", version=version))
        hold.setChecked(True)
        box.setCheckBox(hold)
        go = box.addButton(tr("Roll back"), QMessageBox.AcceptRole)
        stay = box.addButton(tr("Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(stay)
        box.exec()
        return box.clickedButton() is go, hold.isChecked()

    # -- uninstall -----------------------------------------------------
    def uninstall(self, s: AppStatus):
        dialog = UninstallDialog(s, [x.app for x in self.statuses.values()], self,
                                 updates_running=lambda: bool(self.active_jobs), icon=self._icon_for(s.app),
                                 settings=self.settings)
        dialog.removed.connect(self._on_uninstalled)
        dialog.ended.connect(self._update_counts)
        dialog.ended.connect(partial(QTimer.singleShot, 0, partial(self._record_uninstall, dialog)))
        dialog.cleanup_finished.connect(self._record_cleanup)
        dialog.restored.connect(self._record_restore)
        dialog.exec()
        dialog.release()
        self._record_uninstall(dialog, closing=True)

    def batch_uninstall(self, uids: list[str]):
        statuses = [self.statuses[u] for u in uids if u in self.statuses]
        if not statuses or self._uninstall_running():
            return
        dialog = BatchUninstallDialog(statuses, [x.app for x in self.statuses.values()], self,
                                      updates_running=lambda: bool(self.active_jobs), settings=self.settings)
        dialog.removed.connect(self._on_uninstalled)
        dialog.ended.connect(self._update_counts)
        dialog.ended.connect(partial(QTimer.singleShot, 0, partial(self._record_uninstall, dialog)))
        dialog.cleanup_finished.connect(self._record_cleanup)
        dialog.restored.connect(self._record_restore)
        dialog.exec()
        dialog.release()
        self._record_uninstall(dialog, closing=True)

    def batch_ignore(self, uids: list[str]):
        for uid in uids:
            s = self.statuses.get(uid)
            if s:
                self.settings.set_ignored(s.app.ident, True)
        self.populate()

    def _uninstall_running(self) -> bool:
        """Closing the dialog doesn't stop its uninstaller, so updates and checks wait for it too:
        updating a half-removed app fails, and a check would list it again."""
        if not uninstalls_running():
            return False
        self._set_subtitle(tr("An uninstall is still running. Wait until it finishes."))
        return True

    def _on_uninstalled(self, uid: str):
        self.removed.add(uid)
        s = self.statuses.pop(uid, None)
        self.job_state.pop(uid, None)
        if s:
            log.info("Uninstalled %s %s", s.app.name, s.app.version)
        self.populate()
        self._save_snapshot()
        self._sync_discover_installed()

    def _recheck(self, s: AppStatus):
        # A recheck replaces the row's state: for an update that is queued or running that would drop its Cancel control.
        if self.scanning or s.uid in self.active_jobs:
            return
        self._set_state(s.uid, ("progress", "Rechecking", {}, -1))

        def work():
            try:
                self.scanner.recheck(s)
            except Exception:
                log.exception("recheck failed")
            self.bus.recheck_finished.emit(s)

        threading.Thread(target=work, daemon=True).start()

    def _on_recheck_finished(self, s: AppStatus):
        if s.uid not in self.removed:
            self.statuses[s.uid] = s
        if s.uid not in self.active_jobs:       # an update queued while the recheck ran owns the row's state now
            self.job_state.pop(s.uid, None)
        self.populate()
        self._save_snapshot()

    def closeEvent(self, event):
        if self.background is not None and self.background.should_hide_on_close():
            event.ignore()
            self.hide()
            return
        if not self._confirm_leave():
            event.ignore()
            return
        self.shutdown()
        event.accept()
