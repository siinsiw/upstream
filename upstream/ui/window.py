"""Main window: update list, all apps, ignored apps, and the install queue."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import datetime
from functools import partial

from PySide6.QtCore import QFileInfo, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QDialog, QFileIconProvider, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from ..context import CancelToken, Question, UpdateContext
from ..errors import Cancelled
from ..i18n import N_, english, num, render, tr, trn
from ..scanner import AppStatus, Scanner
from ..updater import Updater
from .locale import apply_language as apply_qt_language
from .settings import SettingsDialog
from .theme import app_icon, apply_theme, letter_avatar
from .uninstall import UninstallDialog, running as uninstalls_running
from .widgets import AppRow, SourceDialog

log = logging.getLogger("upstream.ui")

TABS = {"updates": "Updates", "all": "All apps", "ignored": "Ignored"}
# Progress messages (updater.py) shown while an installer runs; cancelling then only stops the waiting.
INSTALL_PHASES = frozenset({"Installing", "Setup window opened"})
# Seconds a cancelled update gets to stop by itself (a download does, in a moment) before the queue
# moves on without it. A step that does not notice the token is left to finish in the background.
WIND_DOWN = 2.0


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


class MainWindow(QMainWindow):
    def __init__(self, settings, cache):
        super().__init__()
        self.settings = settings
        self.scanner = Scanner(settings, cache)
        self.updater = Updater(settings)
        self.bus = Bus()
        self.statuses: dict[str, AppStatus] = {}
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
        self.scanning = False
        self.last_scan: datetime | None = None
        self.current_tab = "updates"
        self.icon_queue: list[AppRow] = []
        self.icon_cache: dict = {}
        self.icon_provider = QFileIconProvider()

        self._build()
        self.bus.scan_progress.connect(self._on_scan_progress)
        self.bus.scan_finished.connect(self._on_scan_finished)
        self.bus.scan_failed.connect(self._on_scan_failed)
        self.bus.recheck_finished.connect(self._on_recheck_finished)
        self.bus.job_progress.connect(self._on_job_progress)
        self.bus.job_finished.connect(self._on_job_finished)
        self.bus.log_line.connect(self.log_view.appendPlainText)
        self.bus.question.connect(self._on_question)
        logging.getLogger("upstream").addHandler(UiLogHandler(self.bus))

        self.icon_timer = QTimer(self)
        self.icon_timer.setInterval(0)
        self.icon_timer.timeout.connect(self._load_icons)
        threading.Thread(target=self._job_loop, daemon=True).start()

    # -- layout --------------------------------------------------------
    def _build(self):
        self.setWindowTitle("Upstream")
        self.setWindowIcon(app_icon())
        self.resize(1040, 740)
        self.setMinimumSize(800, 520)
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
        self.subtitle = QLabel()
        self.subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles)
        head.addStretch(1)
        self.settings_btn = QPushButton()
        self.settings_btn.clicked.connect(self.open_settings)
        self.scan_btn = QPushButton()
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_all_btn = QPushButton()
        self.cancel_all_btn.setEnabled(False)
        self.cancel_all_btn.clicked.connect(self.cancel_all_jobs)
        self.all_btn = QPushButton()
        self.all_btn.setObjectName("primary")
        self.all_btn.setEnabled(False)
        self.all_btn.clicked.connect(self.update_all)
        for b in (self.settings_btn, self.scan_btn, self.cancel_all_btn, self.all_btn):
            b.setCursor(Qt.PointingHandCursor)
            head.addWidget(b, 0, Qt.AlignBottom)
        v.addLayout(head)
        v.addSpacing(16)

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
            b.clicked.connect(partial(self.show_tab, key))
            group.addButton(b)
            tabs.addWidget(b)
            self.tab_buttons[key] = b
        bar.addLayout(tabs)
        bar.addStretch(1)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(250)
        self.search.textChanged.connect(self.apply_filter)
        bar.addWidget(self.search)
        v.addLayout(bar)
        v.addSpacing(12)

        self.stack = QStackedWidget()
        self.lists: dict[str, QListWidget] = {}
        for key in TABS:
            lw = QListWidget()
            lw.setSpacing(3)
            lw.setSelectionMode(QAbstractItemView.NoSelection)
            lw.setFocusPolicy(Qt.NoFocus)
            lw.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            lw.verticalScrollBar().setSingleStep(20)
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.lists[key] = lw
            self.stack.addWidget(lw)
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
        self.show_tab("updates")
        # Start in the search box: typing filters at once, and no button looks pre-selected.
        self.search.setFocus()

    def retranslate(self):
        """Re-apply every visible string; used at start and when the language changes."""
        self.settings_btn.setText(tr("Settings"))
        self.scan_btn.setText(tr("Check again"))
        self.cancel_all_btn.setText(tr("Cancel all"))
        self.search.setPlaceholderText(tr("Search apps"))
        self.log_btn.setText(tr("Hide log") if self.log_view.isVisible() else tr("Show log"))
        if self.scanning:
            self.subtitle.setText(tr("Checking…"))
        if self.statuses:
            self.populate()
        else:
            self._update_counts()
            self._sync_stack()

    # -- settings ------------------------------------------------------
    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        dialog.language_changed.connect(self.apply_language)
        dialog.theme_changed.connect(self.apply_theme)
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

    # -- scanning ------------------------------------------------------
    def start_scan(self):
        if self.scanning or self.active_jobs or self._uninstall_running():
            return
        self.removed.clear()
        self.scanning = True
        self.scan_btn.setEnabled(False)
        self.all_btn.setEnabled(False)
        self.scan_bar.setRange(0, 0)
        self.scan_bar.show()
        self.subtitle.setText(tr("Checking…"))
        self._sync_stack()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            result = self.scanner.scan(lambda text, i, n: self.bus.scan_progress.emit(text, i, n))
        except Exception as e:
            log.exception("scan failed")
            self.bus.scan_failed.emit(str(e))
            return
        self.bus.scan_finished.emit(result)

    def _on_scan_progress(self, text: str, i: int, n: int):
        self.subtitle.setText(tr("{text} {i} of {n}", text=text, i=num(i), n=num(n)) if n else text)
        if n:
            self.scan_bar.setRange(0, n)
            self.scan_bar.setValue(i)
        else:
            self.scan_bar.setRange(0, 0)

    def _on_scan_finished(self, statuses):
        self.scanning = False
        self.scan_bar.hide()
        self.scan_btn.setEnabled(True)
        self.last_scan = datetime.now()
        self.statuses = {s.uid: s for s in statuses if s.uid not in self.removed}
        # Finished jobs' results belong to the old list; a job that is still queued or running keeps its row control.
        self.job_state = {uid: state for uid, state in self.job_state.items() if uid in self.active_jobs}
        self.populate()
        log.info("Check finished: %d apps, %d updates", len(statuses), sum(1 for s in statuses if s.update))

    def _on_scan_failed(self, message: str):
        self.scanning = False
        self.scan_bar.hide()
        self.scan_btn.setEnabled(True)
        self.subtitle.setText(tr("Check failed: {error}", error=message))

    # -- lists ---------------------------------------------------------
    def populate(self):
        for lw in self.lists.values():
            lw.clear()
        self.rows = {}
        self.icon_queue = []
        for s in sorted(self.statuses.values(), key=lambda s: s.app.name.casefold()):
            ident = s.app.ident
            ignored = self.settings.is_ignored(ident)
            skipped = bool(s.update) and self.settings.skipped_version(ident) == s.update.version
            if ignored:
                self._add_row("ignored", s, skipped, True)
                continue
            self._add_row("all", s, skipped, False)
            # Rows with a job stay in Updates until the next check, so results remain visible.
            if (s.update and not skipped) or s.uid in self.job_state:
                self._add_row("updates", s, skipped, False)
        for uid, state in self.job_state.items():
            for row in self.rows.get(uid, []):
                self._show_state(row, state)
        self._update_counts()
        self.apply_filter()
        self.icon_timer.start()

    def _add_row(self, tab: str, s: AppStatus, skipped: bool, ignored: bool):
        row = AppRow(s, skipped, ignored)
        row.update_clicked.connect(self.request_update)
        row.cancel_clicked.connect(self.cancel_job)
        row.menu_action.connect(self.on_menu)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, s.uid)
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        lw = self.lists[tab]
        lw.addItem(item)
        lw.setItemWidget(item, row)
        self.rows.setdefault(s.uid, []).append(row)
        self.icon_queue.append(row)

    def _load_icons(self):
        for _ in range(10):
            if not self.icon_queue:
                self.icon_timer.stop()
                return
            row = self.icon_queue.pop(0)
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
        QApplication.quit()

    def _pending_updates(self) -> list[str]:
        out = []
        for i in range(self.lists["updates"].count()):
            uid = self.lists["updates"].item(i).data(Qt.UserRole)
            s = self.statuses.get(uid)
            if s and s.update and uid not in self.active_jobs:
                out.append(uid)
        return out

    def _update_counts(self):
        for key, text in TABS.items():
            count = self.lists[key].count()
            label = tr(text)
            self.tab_buttons[key].setText(f"{label}  {num(count)}" if count else label)
        pending = self._pending_updates()
        self.cancel_all_btn.setEnabled(bool(self.active_jobs))
        self.all_btn.setEnabled(bool(pending) and not self.scanning)
        self.all_btn.setText(tr("Update all ({n})", n=num(len(pending))) if pending else tr("Update all"))

        if not self.scanning and self.last_scan:
            if self.active_jobs:
                status = trn("{n} update in progress", "{n} updates in progress", len(self.active_jobs))
            elif pending:
                status = trn("{n} update ready", "{n} updates ready", len(pending))
            else:
                status = tr("Everything is up to date")
            status += tr(" · last checked {time}", time=num(self.last_scan.strftime("%H:%M")))
            self.subtitle.setText(status)

        gh = self.scanner.github_if_ready
        parts = [trn("{n} installed app", "{n} installed apps", len(self.statuses))] if self.statuses else []
        if gh:  # until the next check builds the client, the token state is unknown
            parts.append(tr("GitHub with token") if gh.token else tr("GitHub without token (60 requests/hour)"))
        text = " · ".join(parts)
        if gh and gh.remaining:
            text += tr(" · {n} requests left", n=num(gh.remaining))
        self.footer.setText(text)

    def show_tab(self, key: str):
        self.current_tab = key
        self.tab_buttons[key].setChecked(True)
        self._sync_stack()

    def _sync_stack(self):
        lw = self.lists[self.current_tab]
        if any(not lw.item(i).isHidden() for i in range(lw.count())):
            self.stack.setCurrentWidget(lw)
            return
        if not self.statuses:
            title, text = tr("Checking your apps…"), tr(
                "The first check takes a little longer: each app's official source is found once and remembered.")
        elif self.search.text().strip():
            title, text = tr("Nothing matches"), ""
        elif self.current_tab == "updates":
            title, text = tr("Everything is up to date"), tr(
                "If an app's source wasn't found, set it from All apps → ⋯ → Update source.")
        elif self.current_tab == "ignored":
            title, text = tr("Nothing here"), tr("Apps you ignore are kept here.")
        else:
            title, text = tr("No apps found"), ""
        self.empty_title.setText(title)
        self.empty_text.setText(text)
        self.stack.setCurrentWidget(self.empty)

    def apply_filter(self):
        q = self.search.text().strip().casefold()
        for lw in self.lists.values():
            for i in range(lw.count()):
                item = lw.item(i)
                s = self.statuses.get(item.data(Qt.UserRole))
                hay = f"{s.app.name} {s.app.publisher}".casefold() if s else ""
                item.setHidden(bool(q) and q not in hay)
        self._sync_stack()

    def toggle_log(self):
        visible = not self.log_view.isVisible()
        self.log_view.setVisible(visible)
        self.log_btn.setText(tr("Hide log") if visible else tr("Show log"))

    # -- updates -------------------------------------------------------
    def request_update(self, uid: str, bulk: bool = False):
        s = self.statuses.get(uid)
        # A check replaces the list and the rows' job states; an update started meanwhile would lose its Cancel control.
        if not s or not s.update or uid in self.active_jobs or self.scanning or self._uninstall_running():
            return
        upd = s.update
        if upd.source == "site" and not upd.download_url and not s.fallbacks:
            QDesktopServices.openUrl(QUrl(upd.page_url))
            return
        self.active_jobs.add(uid)
        token = self.cancels[uid] = CancelToken()
        if bulk:
            self.bulk_jobs.add(uid)
        self._set_state(uid, ("queued",))
        self.jobs.put((uid, token))
        self._update_counts()

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
        s = self.statuses.get(uid)
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
        status = self.statuses.get(uid)
        if status is None or status.update is None:
            return False, "No update pending anymore", {}, ""
        name = status.app.name
        ctx = UpdateContext(self.settings, token, partial(self._ask_from_worker, token), uid in self.bulk_jobs)
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
        for row in self.rows.get(uid, []):
            self._show_state(row, state)

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
        self.active_jobs.discard(uid)
        token = self.cancels.pop(uid, None)
        self.bulk_jobs.discard(uid)
        with self.queue_lock:
            self.taken.discard(uid)
        s = self.statuses.get(uid)
        cancelled = not ok and token is not None and token.is_set
        if ok and s:
            s.app.version = version or s.app.version
            s.update, s.fallbacks = None, []
        self._set_state(uid, ("cancelled", template, values) if cancelled else ("result", ok, template, values, version))
        self._update_counts()
        if not self.active_jobs and not cancelled:
            QApplication.alert(self)

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

    def edit_source(self, s: AppStatus):
        dialog = SourceDialog(s, self.settings.override(s.app.ident), self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.settings.set_override(s.app.ident, dialog.result_value or None)
        log.info("Source override for %s: %s", s.app.name, dialog.result_value or "cleared")
        self._recheck(s)

    def uninstall(self, s: AppStatus):
        dialog = UninstallDialog(s, [x.app for x in self.statuses.values()], self,
                                 updates_running=lambda: bool(self.active_jobs), icon=self._icon_for(s.app))
        dialog.removed.connect(self._on_uninstalled)
        dialog.ended.connect(self._update_counts)
        dialog.exec()
        dialog.release()

    def _uninstall_running(self) -> bool:
        """Closing the dialog doesn't stop its uninstaller, so updates and checks wait for it too:
        updating a half-removed app fails, and a check would list it again."""
        if not uninstalls_running():
            return False
        self.subtitle.setText(tr("An uninstall is still running. Wait until it finishes."))
        return True

    def _on_uninstalled(self, uid: str):
        self.removed.add(uid)
        s = self.statuses.pop(uid, None)
        self.job_state.pop(uid, None)
        if s:
            log.info("Uninstalled %s %s", s.app.name, s.app.version)
        self.populate()

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
        self.job_state.pop(s.uid, None)
        self.populate()

    def closeEvent(self, event):
        if self.active_jobs or uninstalls_running():
            box = QMessageBox(self)
            box.setWindowTitle("Upstream")
            box.setText(tr("Updates are still running. The install that already started will finish, "
                           "but the rest of the queue is cancelled.") if self.active_jobs
                        else tr("An uninstall is still running. Closing Upstream does not stop it, "
                                "but Upstream will no longer show how it went."))
            leave = box.addButton(tr("Close"), QMessageBox.AcceptRole)
            box.addButton(tr("Stay"), QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not leave:
                event.ignore()
                return
            self.cancel_all_jobs()
        event.accept()
