"""The app row and the 'update source' dialog."""
from __future__ import annotations

import os
import re
import time
import unicodedata

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
    QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from ..github import repo_from_urls
from ..i18n import SOURCE_LABELS, isolate_lines, num, tr
from ..scanner import AppStatus
from .theme import repolish

# Update, Cancel and Retry sit in the same spot of the row. For this long after one replaces another a click on
# the newcomer is ignored: it is the second half of a double-click aimed at the old control.
CLICK_GUARD = 0.4


def ui_is_rtl() -> bool:
    return QGuiApplication.layoutDirection() == Qt.RightToLeft


def direction_of(text: str):
    """Direction of the first strong character, as the Unicode bidi algorithm
    does: "Adobe Reader - فارسی" stays left-to-right."""
    for ch in text or "":
        kind = unicodedata.bidirectional(ch)
        if kind == "L":
            return Qt.LeftToRight
        if kind in ("R", "AL"):
            return Qt.RightToLeft
    return Qt.LeftToRight


def ltr_label(text: str = "", name: str = "") -> QLabel:
    """For versions: keeps '3.9.0+38' in order inside an RTL UI."""
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setLayoutDirection(Qt.LeftToRight)
    label.setTextFormat(Qt.PlainText)
    return label


class ElidedLabel(QLabel):
    """One line, elided, laid out in the text's own direction (so 'Figma, Inc.' keeps
    its full stop in an RTL window) and aligned to the UI's leading edge, or to its trailing edge
    (`trailing`) for a note that sits right before a control."""

    def __init__(self, text: str = "", name: str = "", trailing: bool = False):
        super().__init__()
        if name:
            self.setObjectName(name)
        self.trailing = trailing
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_full_text(text)

    @property
    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str):
        self._full = text
        self.setLayoutDirection(direction_of(text))
        self.setToolTip(text)
        self.update()

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(min(fm.horizontalAdvance(self._full) + 4, 520), fm.height() + 2)

    def minimumSizeHint(self):
        return QSize(40, self.fontMetrics().height() + 2)

    def paintEvent(self, event):
        rtl_text = self.layoutDirection() == Qt.RightToLeft
        force = Qt.TextFlag.TextForceRightToLeft if rtl_text else Qt.TextFlag.TextForceLeftToRight
        side = Qt.AlignRight if ui_is_rtl() != self.trailing else Qt.AlignLeft
        flags = side.value | Qt.AlignAbsolute.value | Qt.AlignVCenter.value | force.value
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(self.rect(), flags, self.fontMetrics().elidedText(self._full, Qt.ElideRight, self.width()))


class AppRow(QFrame):
    update_clicked = Signal(str)
    cancel_clicked = Signal(str)
    menu_action = Signal(str, str)
    selection_changed = Signal(str, bool)     # uid, ticked (only in selectable mode)

    def __init__(self, status: AppStatus, skipped: bool, ignored: bool):
        super().__init__()
        self.setObjectName("row")
        self.setAttribute(Qt.WA_Hover)
        self.status = status
        self.uid = status.uid
        self._page_since = float("-inf")    # when the action area last switched controls

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(14)

        self.icon = QLabel()
        self.icon.setFixedSize(36, 36)
        lay.addWidget(self.icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self.name = ElidedLabel(status.app.name, "appName")
        text_col.addWidget(self.name)
        meta = QHBoxLayout()
        meta.setSpacing(6)
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        self.publisher = ElidedLabel(status.app.publisher or tr("Unknown publisher"), "meta")
        meta.addWidget(self.badge, 0)
        meta.addWidget(self.publisher, 1)
        text_col.addLayout(meta)
        lay.addLayout(text_col, 1)

        versions = QHBoxLayout()
        versions.setSpacing(8)
        self.old_ver = ltr_label("", "oldVer")
        self.arrow = QLabel("←" if ui_is_rtl() else "→")
        self.arrow.setObjectName("arrow")
        self.new_ver = ltr_label("", "newVer")
        for w in (self.old_ver, self.arrow, self.new_ver):
            versions.addWidget(w)
        lay.addLayout(versions)

        self.actions = QStackedWidget()
        self.actions.setFixedWidth(240)
        self.button = QPushButton(tr("Update"))
        self.button.setObjectName("rowAction")
        self.button.setProperty("tone", "accent")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self._emit_unless_just_swapped(self.update_clicked))
        # Shown beside the button after a cancel ("Cancelled": the update stays on offer) or a failure (a short
        # reason, the whole text in the tooltip): a neutral note, one line, elided.
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
        # Phase text elides; the percentage has its own label so it is never cut off.
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
        self.cancel_btn.clicked.connect(lambda: self._emit_unless_just_swapped(self.cancel_clicked))
        # Room for the longer label, kept when it is hidden, so every bar in the list ends at the same place.
        self.cancel_btn.ensurePolished()   # the stylesheet sets the font size, so measure after it applies
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

        self.more = QToolButton()
        self.more.setObjectName("more")
        self.more.setText("⋯")
        self.more.setAccessibleName(tr("More actions"))
        self.more.setToolTip(tr("More actions"))
        self.more.setPopupMode(QToolButton.InstantPopup)
        self.more.setCursor(Qt.PointingHandCursor)
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._build_menu)
        self.more.setMenu(self.menu)
        lay.addWidget(self.more)

        self.refresh(status, skipped, ignored)

    # -- state ---------------------------------------------------------
    def refresh(self, status: AppStatus, skipped: bool, ignored: bool):
        self.status, self.skipped, self.ignored = status, skipped, ignored
        app, upd = status.app, status.update
        self.name.set_full_text(app.name)

        source = upd.source if upd else ("github" if status.repo else "winget" if status.winget_id else "")
        self.badge.setVisible(bool(source))
        self.badge.setText(tr(SOURCE_LABELS.get(source, "")) if source else "")
        self.badge.setProperty("source", source)
        repolish(self.badge)

        if upd and not skipped:
            self.old_ver.setText(app.version or "?")
            self.new_ver.setText(upd.version)
            self.arrow.show()
            self.new_ver.show()
            manual = upd.source == "site" and not upd.download_url and not status.fallbacks
            self.button.setText(tr("Open website") if manual else tr("Update"))
            self._show_page(0)
            tip = [tr("Source: {label} — {origin}", label=tr(SOURCE_LABELS[upd.source]), origin=upd.origin)]
            if upd.notes:
                tip.append(isolate_lines(upd.notes[:600]))
            self.setToolTip("\n\n".join(tip))
        else:
            self.old_ver.setText(app.version or "")
            self.arrow.hide()
            self.new_ver.hide()
            if ignored:
                self._state(tr("Not checked"), "muted")
            elif upd and skipped:
                self._state(tr("Skipped {version}", version=upd.version), "muted")
            elif status.note:
                self._state(status.note_text(), "muted")
            elif status.repo or status.winget_id:
                self._state(tr("Up to date"), "muted")
            else:
                self._state(tr("No source found"), "muted")
            self.setToolTip(status.note_text())

    def _state(self, text: str, tone: str):
        self.state.setText(text)
        self.state.setProperty("tone", tone)
        repolish(self.state)
        self._show_page(2)

    def _show_page(self, index: int):
        """Switch the action area. Another control now sits where the old one was: a click on it within
        CLICK_GUARD is ignored, and the keyboard focus is not handed on to it (Space on Update would
        otherwise land on Cancel, and the next Space press it)."""
        if self.actions.currentIndex() == index:
            return
        focused = self.window().focusWidget()
        if focused is not None and self.actions.isAncestorOf(focused):
            focused.clearFocus()
        self._page_since = time.monotonic()
        self.actions.setCurrentIndex(index)

    def _emit_unless_just_swapped(self, signal):
        if time.monotonic() - self._page_since >= CLICK_GUARD:
            signal.emit(self.uid)

    def set_icon(self, pixmap):
        self.icon.setPixmap(pixmap)

    def _cancel_control(self, label: str = "", tip: str = ""):
        """The link beside the progress bar; no label hides it (a recheck, or a stop already under way)."""
        self.cancel_btn.setText(label)
        self.cancel_btn.setToolTip(tip)
        self.cancel_btn.setVisible(bool(label))

    # -- wave 2 hooks: the rows-and-library agent fills these in --------------------------------
    def set_badges(self, badges) -> None:
        """Show one chip per badges.Badge (text and tooltip through render(b.text, b.values))."""

    def set_pin(self, pin) -> None:
        """Show a pin chip for status.pin ({"mode": "hold" or "major", "version": ...}) or hide it (None)."""

    def set_auto_update(self, on: bool) -> None:
        """State of the checkable Update automatically menu entry (the window owns settings auto_update)."""

    def set_meta(self, size_text: str, date_text: str) -> None:
        """Extra columns for the All apps tab (already formatted, empty = not shown)."""

    def set_selectable(self, on: bool) -> None:
        """Show a tick box at the leading edge and emit selection_changed when it changes."""

    @property
    def selected(self) -> bool:
        return False

    def set_selected(self, on: bool) -> None:
        """Set the tick without emitting selection_changed."""

    def set_queued(self):
        self.note.hide()
        self.phase.set_full_text(tr("Queued"))
        self.pct.clear()
        self.bar.setRange(0, 0)
        self._cancel_control(tr("Cancel"))
        self._show_page(1)

    def set_progress(self, text: str, pct: int, cancel: str = ""):
        """`cancel` is "cancel" (the work stops), "wait" (the installer keeps running; only the waiting
        stops) or "" (nothing to cancel)."""
        self.note.hide()
        self.phase.set_full_text(text)
        self.pct.setText(tr("{pct}%", pct=num(pct)) if pct >= 0 else "")
        if pct < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)
        if cancel == "wait":
            self._cancel_control(tr("Stop waiting"),
                                 tr("Upstream stops waiting and moves on. The installer itself is not stopped."))
        else:
            self._cancel_control(tr("Cancel") if cancel else "")
        self._show_page(1)

    def set_cancelling(self):
        self.note.hide()
        self.phase.set_full_text(tr("Cancelling…"))
        self.pct.clear()
        self.bar.setRange(0, 0)
        self._cancel_control()
        self._show_page(1)

    def set_cancelled(self, message: str):
        """Not a failure: the user stopped it, so the row goes back to offering the update."""
        self.note.set_full_text(tr("Cancelled"))
        self.note.setToolTip(message)
        self.note.show()
        self.button.setText(tr("Update"))
        self._show_page(0)
        self.setToolTip(message)
        self.state.setToolTip(message)

    def set_result(self, ok: bool, message: str, version: str):
        self.note.hide()
        if ok:
            self.old_ver.setText(version)
            self.arrow.hide()
            self.new_ver.hide()
            self._state(message, "accent")
        else:
            self.note.set_full_text(" ".join(message.split()))
            self.note.setToolTip(message)
            self.note.show()
            self.button.setText(tr("Retry"))
            self._show_page(0)
            self.setToolTip(message)
        self.state.setToolTip(message)

    # -- menu ----------------------------------------------------------
    def _build_menu(self):
        m, st, upd = self.menu, self.status, self.status.update
        m.clear()

        def add(text, key):
            m.addAction(text, lambda: self.menu_action.emit(self.uid, key))

        if upd and upd.page_url:
            add(tr("Release notes") if upd.source == "github" else tr("Official page"), "notes")
        if st.repo:
            add(tr("GitHub repository ({repo})", repo=st.repo), "repo")
        add(tr("Update source…"), "source")
        m.addSeparator()
        if upd and not self.skipped:
            add(tr("Skip version {version}", version=upd.version), "skip")
        elif self.skipped:
            add(tr("Stop skipping this version"), "unskip")
        add(tr("Stop ignoring") if self.ignored else tr("Ignore this app"), "ignore")
        if st.app.install_location and os.path.isdir(st.app.install_location):
            m.addSeparator()
            add(tr("Open install folder"), "folder")
        if m.actions() and not m.actions()[-1].isSeparator():
            m.addSeparator()
        add(tr("Uninstall…"), "uninstall")


class SourceDialog(QDialog):
    """Per-app source override: GitHub repo, winget id, or a site rule."""

    def __init__(self, status: AppStatus, current: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Update source — {name}", name=status.app.name))
        self.setMinimumWidth(580)
        self.result_value: dict | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        intro = QLabel(tr("Checked in this order: GitHub, then the official site, then winget. "
                          "Leave a field empty to keep automatic detection for it."))
        intro.setWordWrap(True)
        intro.setObjectName("hint")
        lay.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(10)
        site = current.get("site") or {}
        self.github = self._field(current.get("github", ""), status.repo or "owner/repo")
        self.winget = self._field(current.get("winget", ""), status.winget_id or "Publisher.App")
        self.site_url = self._field(site.get("url", ""), "https://example.com/download")
        self.site_regex = self._field(site.get("version_regex", ""), r"Version\s+([\d.]+)")
        self.site_dl = self._field(site.get("download_url", ""), "https://example.com/app-{version}-x64.exe")
        form.addRow(tr("GitHub repository"), self.github)
        form.addRow(tr("winget ID"), self.winget)
        form.addRow(tr("Site download page"), self.site_url)
        form.addRow(tr("Version pattern (regex)"), self.site_regex)
        form.addRow(tr("Download link"), self.site_dl)
        lay.addLayout(form)

        hint = QLabel(tr("The download link can use {version}, {version_nodots} and {major}. "
                         "Leave it empty and the Update button just opens the site."))
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        lay.addWidget(hint)

        self.error = QLabel()
        self.error.setProperty("tone", "danger")
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)

        buttons = QHBoxLayout()
        save = QPushButton(tr("Save"))
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        clear = QPushButton(tr("Clear overrides"))
        clear.clicked.connect(self._clear)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        lay.addLayout(buttons)

    @staticmethod
    def _field(value: str, placeholder: str) -> QLineEdit:
        edit = QLineEdit(value)
        edit.setPlaceholderText(placeholder)
        edit.setLayoutDirection(Qt.LeftToRight)
        return edit

    def _fail(self, message: str):
        self.error.setText(message)
        self.error.show()

    def _save(self):
        value: dict = {}
        gh = self.github.text().strip()
        if gh:
            repo = repo_from_urls([gh]) if "github.com" in gh.lower() else gh
            if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                return self._fail(tr("The GitHub repository must look like owner/repo or a github.com link."))
            value["github"] = repo
        if self.winget.text().strip():
            value["winget"] = self.winget.text().strip()
        url, regex, dl = (self.site_url.text().strip(), self.site_regex.text().strip(), self.site_dl.text().strip())
        if url or regex or dl:
            if not (url and regex):
                return self._fail(tr("A site rule needs both the page address and the version pattern."))
            try:
                re.compile(regex)
            except re.error as e:
                return self._fail(tr("The version pattern is not a valid regex: {error}", error=e))
            value["site"] = {"url": url, "version_regex": regex, "download_url": dl}
        self.result_value = value
        self.accept()

    def _clear(self):
        self.result_value = {}
        self.accept()
