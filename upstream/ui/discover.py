"""Discover tab: search winget for apps that are not installed yet and queue an install."""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QProgressBar,
    QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import discover, winget
from ..context import CancelToken
from ..errors import Cancelled
from ..i18n import num, render, tr, trn
from .theme import letter_avatar, repolish
from .widgets import CLICK_GUARD, ElidedLabel, ltr_label

log = logging.getLogger("upstream.discover")

LIMIT = 30            # results shown
MIN_CHARS = 2         # a shorter query is not sent: winget answers with everything that contains the letter
DEBOUNCE_MS = 450     # typing pause before a search starts by itself (Enter starts it at once)
_ACTIVE = ("queued", "progress", "cancelling")


def _meta(pkg: discover.Package) -> str:
    # winget lists no publisher; search() takes the first part of the id, which the id already shows.
    if pkg.publisher and pkg.publisher.casefold() != pkg.id.split(".", 1)[0].casefold():
        return f"{pkg.publisher} · {pkg.id}"
    return pkg.id


class DiscoverRow(QFrame):
    """One search result, laid out like an update row. Its action area shows the Install button, the progress
    of the install, or the outcome; `apply()` takes one of the page's state tuples:
    ("idle",) ("queued",) ("progress", text, percent, cancel) ("cancelling",) ("cancelled", text)
    ("failed", text) ("installed", text)."""
    install_clicked = Signal(object)    # discover.Package
    cancel_clicked = Signal(str)        # winget id

    def __init__(self, pkg: discover.Package):
        super().__init__()
        self.setObjectName("row")
        self.setAttribute(Qt.WA_Hover)
        self.pkg = pkg
        self._page_since = float("-inf")     # when the action area last switched controls
        self.current: tuple = ("idle",)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(14)

        self.icon = QLabel()
        self.icon.setFixedSize(36, 36)
        self.icon.setPixmap(letter_avatar(pkg.name))
        lay.addWidget(self.icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        text_col.addWidget(ElidedLabel(pkg.name, "appName"))
        text_col.addWidget(ElidedLabel(_meta(pkg), "meta"))
        lay.addLayout(text_col, 1)

        self.version = ltr_label(pkg.version)
        self.version.setProperty("tone", "muted")
        self.version.setVisible(bool(pkg.version))
        lay.addWidget(self.version)

        self.actions = QStackedWidget()
        self.actions.setFixedWidth(240)
        self.button = QPushButton()
        self.button.setObjectName("rowAction")
        self.button.setProperty("tone", "accent")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self._emit_unless_just_swapped(self.install_clicked, self.pkg))
        self.note = ElidedLabel(trailing=True)
        self.note.setProperty("tone", "muted")
        self.note.hide()
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)
        hl.addStretch(1)
        hl.addWidget(self.note, 100)
        hl.addWidget(self.button)
        self.actions.addWidget(holder)

        prog = QWidget()
        outer = QHBoxLayout(prog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        pl = QVBoxLayout()
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(5)
        phase_row = QHBoxLayout()
        phase_row.setSpacing(6)
        self.phase = ElidedLabel()
        self.phase.setProperty("tone", "muted")
        self.pct = QLabel()
        self.pct.setProperty("tone", "muted")
        phase_row.addWidget(self.phase, 1)
        phase_row.addWidget(self.pct, 0)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(4)
        self.bar.setTextVisible(False)
        pl.addStretch(1)
        pl.addLayout(phase_row)
        pl.addWidget(self.bar)
        pl.addStretch(1)
        outer.addLayout(pl, 1)
        self.cancel_btn = QPushButton()
        self.cancel_btn.setObjectName("link")
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.clicked.connect(lambda: self._emit_unless_just_swapped(self.cancel_clicked, self.pkg.id))
        self.cancel_btn.ensurePolished()
        fm = self.cancel_btn.fontMetrics()
        self.cancel_btn.setFixedWidth(max(fm.horizontalAdvance(tr(t)) for t in ("Cancel", "Stop waiting")) + 20)
        policy = self.cancel_btn.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.cancel_btn.setSizePolicy(policy)
        outer.addWidget(self.cancel_btn, 0, Qt.AlignVCenter)
        self.actions.addWidget(prog)

        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.actions.addWidget(self.state)
        lay.addWidget(self.actions)

        more = QToolButton()
        more.setObjectName("more")
        more.setText("⋯")
        more.setAccessibleName(tr("More actions"))
        more.setToolTip(tr("More actions"))
        more.setPopupMode(QToolButton.InstantPopup)
        more.setCursor(Qt.PointingHandCursor)
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._build_menu)
        more.setMenu(self.menu)
        lay.addWidget(more)

        self.apply(("idle",))

    # -- state ---------------------------------------------------------
    def apply(self, state: tuple):
        self.current = state
        kind = state[0]
        self.setToolTip(f"{self.pkg.name}\n{self.pkg.id}")
        self.button.setAccessibleName(tr("Install {name}", name=self.pkg.name))
        self.note.hide()
        if kind == "queued":
            self._progress(tr("Queued"), -1, "cancel")
        elif kind == "progress":
            self._progress(state[1], state[2], state[3])
        elif kind == "cancelling":
            self._progress(tr("Cancelling…"), -1, "")
        elif kind == "installed":
            self._outcome(state[1] or tr("Installed"), "accent")
        else:
            failed = kind == "failed"
            if kind == "cancelled" or failed:
                self.note.set_full_text(tr("Cancelled") if kind == "cancelled" else " ".join(state[1].split()))
                self.note.setToolTip(state[1])
                self.note.show()
                self.setToolTip(state[1])
            self.button.setText(tr("Retry") if failed else tr("Install"))
            self._show_page(0)

    def _progress(self, text: str, pct: int, cancel: str):
        self.phase.set_full_text(text)
        self.pct.setText(tr("{pct}%", pct=num(pct)) if pct >= 0 else "")
        if pct < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)
        label = {"cancel": tr("Cancel"), "wait": tr("Stop waiting")}.get(cancel, "")
        self.cancel_btn.setText(label)
        self.cancel_btn.setToolTip(
            tr("Upstream stops waiting and moves on. The installer itself is not stopped.") if cancel == "wait" else "")
        self.cancel_btn.setVisible(bool(label))
        self._show_page(1)

    def _outcome(self, text: str, tone: str):
        self.state.setText(text)
        self.state.setToolTip(text)
        self.state.setProperty("tone", tone)
        repolish(self.state)
        self._show_page(2)

    def _show_page(self, index: int):
        """Switch the action area. Another control now sits where the old one was: a click on it within
        CLICK_GUARD is ignored, and the keyboard focus is not handed on to it."""
        if self.actions.currentIndex() == index:
            return
        focused = self.window().focusWidget()
        if focused is not None and self.actions.isAncestorOf(focused):
            focused.clearFocus()
        self._page_since = time.monotonic()
        self.actions.setCurrentIndex(index)

    def _emit_unless_just_swapped(self, signal, value):
        if time.monotonic() - self._page_since >= CLICK_GUARD:
            signal.emit(value)

    def _build_menu(self):
        self.menu.clear()
        self.menu.addAction(tr("Copy winget ID"), lambda: QGuiApplication.clipboard().setText(self.pkg.id))
        self.menu.addAction(tr("Copy install command"),
                            lambda: QGuiApplication.clipboard().setText(f"winget install --id {self.pkg.id} --exact"))


class _Message(QWidget):
    """The centred title and text of the empty states. The text keeps a readable line length: the side margins
    follow the width (a word-wrapped label that is aligned instead would be given only one line of height)."""
    COLUMN = 520

    def __init__(self):
        super().__init__()
        self.box = QVBoxLayout(self)

    def resizeEvent(self, event):
        side = max(20, (event.size().width() - self.COLUMN) // 2)
        self.box.setContentsMargins(side, 0, side, 0)
        super().resizeEvent(event)


class DiscoverPage(QWidget):
    install_requested = Signal(object)       # discover.Package
    cancel_requested = Signal(str)           # winget id of a package that is queued or installing
    _found = Signal(object, str, object)     # search token, query, list[Package] or the exception

    def __init__(self, parent=None, *, search_fn=None, available_fn=None):
        """`search_fn(query, limit, cancel=token)` and `available_fn()` stand in for discover.search and
        winget.available (tests pass fakes)."""
        super().__init__(parent)
        self._search = search_fn or discover.search
        self._available = available_fn or winget.available
        self._token: CancelToken | None = None
        self._results: list[discover.Package] = []
        self._error = ""
        self._query = ""                 # what the last search was for
        self._searching = False
        self._view = "hint"              # hint | searching | results | empty | error | nowinget
        self._installed: set[str] = set()
        self._states: dict[str, tuple] = {}      # lower-cased winget id -> DiscoverRow state, kept across searches
        self._rows: dict[str, DiscoverRow] = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(16)
        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(280)
        self.search_box.setMaximumWidth(460)
        self.search_box.textChanged.connect(self._typed)
        self.search_box.returnPressed.connect(self._submit)
        top.addWidget(self.search_box, 1)
        top.addStretch(1)
        self.summary = QLabel()
        self.summary.setProperty("tone", "muted")
        top.addWidget(self.summary)
        v.addLayout(top)
        v.addSpacing(10)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedHeight(3)
        self.busy.setTextVisible(False)
        self.busy.hide()
        v.addWidget(self.busy)
        v.addSpacing(6)

        self.stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setSpacing(3)
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.verticalScrollBar().setSingleStep(20)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stack.addWidget(self.list)
        self.message = _Message()
        ml = self.message.box
        ml.addStretch(2)
        self.message_title = QLabel()
        self.message_title.setObjectName("emptyTitle")
        self.message_text = QLabel()
        self.message_text.setObjectName("emptyText")
        self.message_text.setWordWrap(True)
        self.message_title.setWordWrap(True)
        for w in (self.message_title, self.message_text):
            w.setAlignment(Qt.AlignCenter)
            ml.addWidget(w)
        self.message_button = QPushButton()
        self.message_button.setCursor(Qt.PointingHandCursor)
        self.message_button.clicked.connect(self._try_again)
        ml.addSpacing(8)
        ml.addWidget(self.message_button, 0, Qt.AlignHCenter)
        ml.addStretch(3)
        self.stack.addWidget(self.message)
        v.addWidget(self.stack, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._submit)
        self._found.connect(self._on_found)

        self.setFocusProxy(self.search_box)
        if not self._available():
            self._view = "nowinget"
        self.retranslate()

    # -- text ----------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply every visible text after a language switch."""
        self.search_box.setPlaceholderText(tr("Search winget for an app to install"))
        self.search_box.setAccessibleName(tr("Search winget for an app to install"))
        self._render()

    def _render(self):
        view = self._view
        self.search_box.setEnabled(view != "nowinget")
        self.summary.clear()
        title, text, button = "", "", ""
        if view == "results":
            self._fill_list()
            n = len(self._results)
            self.summary.setText(tr("Showing the first {n} results. Add more of the name to narrow them down.",
                                    n=num(n)) if n >= LIMIT else trn("{n} result", "{n} results", n))
            self.stack.setCurrentWidget(self.list)
            return
        if view == "searching":
            title, text = tr("Searching…"), tr("Asking winget. This usually takes a second or two.")
        elif view == "empty":
            title = tr("No match in winget")
            text = tr("Nothing in the winget catalogue matches “{query}”. Try another spelling, or only part of the name.",
                      query=self._query)
        elif view == "error":
            title, text, button = tr("The search didn't work"), self._error, tr("Try again")
        elif view == "nowinget":
            title = tr("winget isn't available")
            text = tr("Finding and installing new apps needs winget, which comes with App Installer from the "
                      "Microsoft Store. Install or update it, then try again.")
            button = tr("Try again")
        else:
            title = tr("Find an app to install")
            text = tr("Type a name above. Results come from the winget catalogue, and each app is installed "
                      "from its publisher's own installer, which winget checks first.")
        self.message_title.setText(title)
        self.message_text.setText(text)
        self.message_button.setText(button)
        self.message_button.setVisible(bool(button))
        self.stack.setCurrentWidget(self.message)

    # -- searching -----------------------------------------------------
    def focus_search(self) -> None:
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _typed(self, text: str):
        if len(text.strip()) < MIN_CHARS:
            self._debounce.stop()
            self._stop_search()
            self._results = []
            self._set_view("hint" if self._available() else "nowinget")
        else:
            self._debounce.start()

    def _submit(self):
        self._debounce.stop()
        query = " ".join(self.search_box.text().split())
        if len(query) < MIN_CHARS:
            return
        if not self._available():
            self._stop_search()
            self._set_view("nowinget")
            return
        self._stop_search()
        token = self._token = CancelToken()
        self._searching = True
        self.busy.show()
        if self._view != "results":
            self._set_view("searching")
        threading.Thread(target=self._work, args=(token, query), daemon=True, name="discover-search").start()

    def _stop_search(self):
        if self._token is not None:
            self._token.cancel()
        self._token, self._searching = None, False
        self.busy.hide()

    def _work(self, token: CancelToken, query: str):
        try:
            out = self._search(query, LIMIT, cancel=token)
        except Cancelled:
            return
        except discover.SearchError as e:
            log.warning("search %r failed: %s", query, e)
            out = e
        except Exception as e:      # shown on the page; the stack trace goes to the log
            log.exception("search %r failed", query)
            out = e
        if token.is_set:
            return
        try:
            self._found.emit(token, query, out)
        except RuntimeError:        # the page was closed while winget was working
            pass

    def _on_found(self, token, query: str, out):
        if token is not self._token:        # a newer search or a cleared box has replaced it
            return
        self._token, self._searching = None, False
        self.busy.hide()
        self._query = query
        if isinstance(out, BaseException):
            if isinstance(out, discover.SearchError) and out.template == discover.NO_WINGET:
                self._set_view("nowinget")
                return
            self._error = (render(out.template, out.values) if isinstance(out, discover.SearchError)
                           else tr("Something went wrong: {error}", error=str(out)))
            self._set_view("error")
            return
        self._results = list(out)
        self._set_view("results" if self._results else "empty")

    def _try_again(self):
        if not self._available():
            self._set_view("nowinget")
        elif len(self.search_box.text().strip()) >= MIN_CHARS:
            self._submit()
        else:
            self._set_view("hint")

    def _set_view(self, view: str):
        self._view = view
        self._render()

    # -- rows ----------------------------------------------------------
    def _fill_list(self):
        self.list.clear()
        self._rows = {}
        for pkg in self._results:
            row = DiscoverRow(pkg)
            row.install_clicked.connect(self._on_install)
            row.cancel_clicked.connect(self.cancel_requested)
            row.apply(self._state_of(pkg.id))
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, row.sizeHint().height()))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self._rows[pkg.id.casefold()] = row

    def _state_of(self, pkg_id: str) -> tuple:
        key = pkg_id.casefold()
        state = self._states.get(key)
        if state and state[0] in _ACTIVE:
            return state
        if key in self._installed:
            return state if state and state[0] == "installed" else ("installed", "")
        return state or ("idle",)

    def _set_row(self, pkg_id: str, state: tuple):
        key = pkg_id.casefold()
        self._states[key] = state
        row = self._rows.get(key)
        if row is not None:
            row.apply(self._state_of(pkg_id))

    def _on_install(self, pkg: discover.Package):
        self._set_row(pkg.id, ("queued",))
        self.install_requested.emit(pkg)

    # -- API for the window ----------------------------------------------
    def set_installed_ids(self, ids) -> None:
        """winget ids (any case) of the apps already on this PC; their rows show "Installed" instead of Install."""
        self._installed = {i.casefold() for i in ids}
        for row in self._rows.values():
            row.apply(self._state_of(row.pkg.id))

    def set_queued(self, pkg_id: str) -> None:
        self._set_row(pkg_id, ("queued",))

    def set_state(self, pkg_id: str, text: str, percent: int, cancel: str = "") -> None:
        """Progress of an install; percent -1 is busy. `cancel`: "cancel" (the work stops), "wait" (only the
        waiting stops, the installer keeps running) or "" (no control). `text` is already translated: call
        again after a language switch."""
        self._set_row(pkg_id, ("progress", text, percent, cancel))

    def set_cancelling(self, pkg_id: str) -> None:
        self._set_row(pkg_id, ("cancelling",))

    def set_cancelled(self, pkg_id: str, text: str) -> None:
        """The user stopped it: not a failure, the Install button comes back."""
        self._set_row(pkg_id, ("cancelled", text))

    def set_result(self, pkg_id: str, ok: bool, text: str) -> None:
        """The outcome of an install. A success shows `text` where the button was (and counts as installed); a
        failure shows it beside a Retry button."""
        if ok:
            self._installed.add(pkg_id.casefold())
        self._set_row(pkg_id, ("installed", text) if ok else ("failed", text))

    def mark_installed(self, pkg_id: str) -> None:
        self._installed.add(pkg_id.casefold())
        state = self._states.get(pkg_id.casefold())
        self._set_row(pkg_id, state if state and state[0] == "installed" else ("installed", ""))
