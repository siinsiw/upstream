"""The uninstall dialog: confirm, run, result, and a read-only review of what was left behind.

Upstream deletes nothing here. uninstaller.run() starts the app's own uninstaller on a worker
thread and the review only lists what leftovers.scan() found. Messages from the worker are
(English template, values) pairs, translated when shown, and reach the UI through signals."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPalette, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QStackedWidget, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import leftovers, uninstaller
from ..i18n import english, isolate, num, render, tr, trn
from ..inventory import InstalledApp
from ..scanner import AppStatus
from .theme import current, letter_avatar
from .widgets import ElidedLabel, ui_is_rtl

log = logging.getLogger("upstream.ui.uninstall")

CONFIRM, RUNNING, RESULT, REVIEW = range(4)
PAGE_WIDTH = {CONFIRM: 580, RUNNING: 580, RESULT: 580, REVIEW: 900}
REVIEW_HEIGHT = 700
_LRI, _PDI = "⁦", "⁩"   # left-to-right isolate: a path stays in one piece inside an RTL window
_HIVES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}
_PATH_KINDS = ("folder", "file", "shortcut")
_RUNNING: set[str] = set()   # apps whose uninstaller is running, even if its dialog was closed
_ROW_ROLE = Qt.UserRole + 1   # "group" | "protected": the delegate colours these from the current theme


def N_(text: str) -> str:
    """Marks a template for translation where a table defines it; tr() is applied when it is shown."""
    return text


KIND_LABELS = {"folder": N_("Folder"), "file": N_("File"), "shortcut": N_("Shortcut"),
               "regkey": N_("Registry key"), "regvalue": N_("Registry value"), "service": N_("Service")}
GROUPS = (
    (leftovers.CERTAIN, N_("Certain"), N_("Inside the app's folder, or pointing into it")),
    (leftovers.LIKELY, N_("Likely"), N_("Named exactly like the app")),
    (leftovers.POSSIBLE, N_("Possible"), N_("Weaker matches: check them yourself")),
    (leftovers.PROTECTED, N_("Protected"), N_("Found, but Upstream will never offer to remove them")),
)
RESULT_TITLES = {  # outcome -> (headline, tone)
    "removed": (N_("Removed"), "accent"), "reboot_required": (N_("Restart needed"), "accent"),
    "still_listed": (N_("Not removed yet"), "danger"), "cancelled": (N_("Cancelled"), ""),
    "busy": (N_("Windows is busy"), ""), "failed": (N_("Couldn't remove it"), "danger"),
}


def running() -> int:
    """How many uninstallers are running right now (the main window warns before closing)."""
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


class _Bus(QObject):
    """Worker-thread results; Qt delivers them on the UI thread. It outlives the dialog on purpose:
    a late result then reaches nobody instead of a deleted widget."""
    progress = Signal(str, object)                # template, values
    finished = Signal(str, str, object, bool)     # outcome, template, values, Windows no longer lists the app
    checked = Signal(bool)
    scan_done = Signal(object)
    scan_failed = Signal(str)


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


class UninstallDialog(QDialog):
    removed = Signal(str)   # uid, as soon as Windows no longer lists the app
    ended = Signal()        # the uninstaller stopped, whatever came of it

    def __init__(self, status: AppStatus, others: list[InstalledApp], parent=None,
                 updates_running: Callable[[], bool] | bool = False, icon: QPixmap | None = None):
        super().__init__(parent)
        self.status, self.app, self.uid = status, status.app, status.uid
        self.others = list(others)
        self._updates_running = updates_running if callable(updates_running) else (lambda: updates_running)
        self.was_removed = False
        self.items: list[leftovers.Leftover] = []
        self.plans = {quiet: uninstaller.plan(self.app, status.winget_id, prefer_silent=quiet) for quiet in (True, False)}
        self.plan = self.plans[True]
        self.bus = _Bus()
        self._busy = False        # the uninstaller is running; the worker thread cannot be stopped
        self._checking = False    # "Check again" is waiting for Windows; its answer must still reach the window
        self._released = False
        self._want_scan = False
        self._silent_used = False
        self._outcome: tuple[str, str, dict] = ("", "", {})
        self._wait_shown = ""     # the reason Uninstall is held back, as the confirm page last showed it
        self._icon = icon if icon is not None and not icon.isNull() else letter_avatar(self.app.name)
        self._poll = QTimer(self)   # updates and other uninstalls end while the confirm page is open
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_wait)

        self.setWindowTitle(tr("Uninstall — {name}", name=self.app.name))
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        for page in (self._build_confirm(), self._build_running(), self._build_result(), self._build_review()):
            self.stack.addWidget(page)

        self.bus.progress.connect(self._on_progress)
        self.bus.finished.connect(self._on_finished)
        self.bus.checked.connect(self._on_checked)
        self.bus.scan_done.connect(self._on_scan_done)
        self.bus.scan_failed.connect(self._on_scan_failed)
        self.cancel_btn.setDefault(True)   # only possible once the button belongs to the dialog
        self._show(CONFIRM)

    # -- building ------------------------------------------------------
    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        return page, lay

    @staticmethod
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

    @staticmethod
    def _button(text: str, name: str = "", slot=None) -> QPushButton:
        b = QPushButton(text)
        if name:
            b.setObjectName(name)
        b.setCursor(Qt.PointingHandCursor)
        if slot:
            b.clicked.connect(slot)
        return b

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
        self.leftover_hint = QLabel(tr("The review only lists what was left behind. Upstream deletes nothing from it."))
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
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setFixedHeight(4)
        bar.setTextVisible(False)
        self.run_hint = QLabel()
        self.run_hint.setObjectName("hint")
        self.run_hint.setWordWrap(True)
        for w in (self.run_title, self.step, bar, self.run_hint):
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
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setFixedHeight(4)
        bar.setTextVisible(False)
        sl.addWidget(bar)
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

        self.readonly_note = QLabel(tr("Nothing on this list has been deleted. It is only a report: "
                                       "cleanup with backup and undo will come in a later version."))
        self.readonly_note.setObjectName("hint")
        self.readonly_note.setWordWrap(True)
        lay.addWidget(self.readonly_note)
        buttons = QHBoxLayout()
        self.open_btn = self._button(tr("Open location"), "", self._open_location)
        self.export_btn = self._button(tr("Export list…"), "", self._export)
        done = self._button(tr("Done"), "primary", self.accept)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.export_btn)
        buttons.addStretch(1)
        buttons.addWidget(done)
        lay.addLayout(buttons)
        return page

    def _build_results(self) -> QWidget:
        page, lay = self._page()
        lay.setSpacing(8)
        self.summary = QLabel()
        self.summary.setObjectName("hint")
        lay.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([tr("Item"), tr("Type"), tr("Why")])
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollPerPixel)
        self.tree.setItemDelegate(_Delegate(self.tree))
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setColumnWidth(0, 400)
        self.tree.itemSelectionChanged.connect(self._on_select)
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

    # -- pages ---------------------------------------------------------
    def _show(self, index: int):
        for i in range(self.stack.count()):
            policy = QSizePolicy.Preferred if i == index else QSizePolicy.Ignored   # only the shown page sizes the dialog
            self.stack.widget(i).setSizePolicy(policy, policy)
        self.stack.setCurrentIndex(index)
        self.setMinimumSize(0, 0)
        self.setMinimumWidth(PAGE_WIDTH[index])
        if index == CONFIRM:
            self._refresh_confirm()
            self._poll.start()
        else:
            self._poll.stop()
        self._fit()

    def _fit(self):
        """Size the dialog to the shown page (the stack would otherwise keep the tallest page's height)."""
        index = self.stack.currentIndex()
        width = PAGE_WIDTH[index]
        if index == REVIEW:
            self.resize(max(self.width(), width), max(self.height(), REVIEW_HEIGHT))
            return
        margins = self.layout().contentsMargins()
        page = self.stack.currentWidget().layout()
        inner = width - margins.left() - margins.right()
        height = page.totalHeightForWidth(inner) if page.hasHeightForWidth() else page.sizeHint().height()
        self.resize(width, height + margins.top() + margins.bottom())

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
        self.result_title.style().unpolish(self.result_title)
        self.result_title.style().polish(self.result_title)
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
        self.result_close.style().unpolish(self.result_close)
        self.result_close.style().polish(self.result_close)
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
            if not self._busy:
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

    # -- review --------------------------------------------------------
    def _begin_scan(self, removed_text: str):
        self.review_sub.setText(removed_text)
        self.review_stack.setCurrentIndex(0)
        self.open_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self._show(REVIEW)

        def work(bus: _Bus, app: InstalledApp, others: list[InstalledApp]):
            try:
                items = leftovers.scan(app, others)
            except Exception as e:
                log.exception("leftover scan failed")
                bus.scan_failed.emit(str(e))
                return
            bus.scan_done.emit(items)

        threading.Thread(target=work, args=(self.bus, self.app, self.others), daemon=True).start()

    def _on_scan_failed(self, error: str):
        self.review_message.setText(tr("Looking for leftovers failed: {error}", error=error))
        self.review_stack.setCurrentIndex(2)

    def _on_scan_done(self, items: list):
        self.items = items
        if not items:
            self.review_message.setText(tr("Upstream found nothing that was left behind."))
            self.review_stack.setCurrentIndex(2)
            return
        self.summary.setText(trn("{n} item found", "{n} items found", len(items)))
        self._fill_tree()
        self.export_btn.setEnabled(True)
        self.review_stack.setCurrentIndex(1)
        self._on_select()

    def _fill_tree(self):
        self.tree.clear()
        bold = self.tree.font()
        bold.setBold(True)
        for confidence, title, blurb in GROUPS:
            rows = [(i, it) for i, it in enumerate(self.items) if it.confidence == confidence]
            if not rows:
                continue
            head = QTreeWidgetItem(self.tree)
            head.setText(0, f"{tr(title)}  {num(len(rows))}")
            head.setText(2, tr(blurb))
            head.setFlags(Qt.ItemIsEnabled)
            for col in range(3):
                head.setData(col, _ROW_ROLE, "group")
            head.setFont(0, bold)
            for index, it in rows:
                row = QTreeWidgetItem(head)
                row.setText(0, _ltr(it.location))
                row.setText(1, tr(KIND_LABELS.get(it.kind, it.kind)))
                row.setText(2, render(it.reason, it.reason_values))
                row.setData(0, Qt.UserRole, index)
                row.setToolTip(0, _ltr(it.location))
                row.setToolTip(2, row.text(2))
                if confidence == leftovers.PROTECTED:
                    for col in range(3):
                        row.setData(col, _ROW_ROLE, "protected")
        self.tree.expandAll()

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
        app = self.app
        lines = [tr("Leftovers of {name}", name=f"{app.name} {app.version}".strip()),
                 tr("Made by Upstream on {date}", date=datetime.now().strftime("%Y-%m-%d %H:%M")),
                 self.readonly_note.text(), ""]
        for confidence, title, _ in GROUPS:
            rows = [it for it in self.items if it.confidence == confidence]
            if rows:
                lines += [f"{tr(title)} ({num(len(rows))})", ""]
            for it in rows:
                lines.append(f"[{tr(KIND_LABELS.get(it.kind, it.kind))}] {it.location}")
                lines.append(f"    {render(it.reason, it.reason_values)}")
                if it.caveat:
                    lines.append(f"    {tr(it.caveat)}")
                if it.kind in _PATH_KINDS and it.size is not None:
                    lines.append("    " + tr("Size: {size}", size=size_text(it.size)))
                lines.append("")
        return "\n".join(lines)

    def _export(self):
        safe = "".join(c for c in self.app.name if c not in '<>:"/\\|?*').strip() or "app"
        path, _ = QFileDialog.getSaveFileName(self, tr("Export list"), f"{safe} leftovers.txt",
                                              tr("Text files (*.txt)"))
        if not path:
            return
        try:
            Path(path).write_text(self.export_text(), "utf-8-sig")   # the BOM lets Notepad read Persian
        except OSError as e:
            self.status_line.setText(tr("Couldn't save the list: {error}", error=e.strerror or str(e)))
            return
        log.info("Leftover list of %s saved to %s", self.app.name, path)
        self.status_line.setText(tr("Saved to {path}", path=path))

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
        if not (self._busy or self._checking):
            self.deleteLater()
