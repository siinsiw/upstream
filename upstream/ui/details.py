"""The details dialog: what Windows and Upstream know about one app, with Repair and Modify.

It opens at once from what the scan already holds. The slow parts (the installer family needs the
uninstaller file, the size of a folder, the Authenticode signature needs PowerShell) run on one worker
thread and arrive through signals. Repair and Modify run on a thread too. Messages from the worker are
(English template, values) pairs, translated when shown, so a language switch applies to what is on screen."""
from __future__ import annotations

import html
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import details
from ..context import CancelToken
from ..i18n import N_, SOURCE_LABELS, num, render, tr
from ..scanner import AppStatus
from .theme import current, letter_avatar, repolish
from .widgets import ElidedLabel, direction_of, ui_is_rtl

log = logging.getLogger("upstream.ui.details")

WIDTH = 660
LABEL_WIDTH = 128
NOTES_LIMIT = 4000
NOTES_HEIGHT = 120
LINK_CHARS = 64
ARCH_LABELS = {"x64": N_("64-bit"), "x86": N_("32-bit")}
SCOPE_LABELS = {"machine": N_("All users"), "user": N_("Current user only")}


@dataclass
class _Row:
    key: str
    label: str
    text: str
    tone: str = ""     # "", muted, accent or danger: the QLabel[tone] styles in the theme


class _Bus(QObject):
    """Worker-thread results; Qt delivers them on the UI thread. A late result reaches nobody once the dialog is gone."""
    family = Signal(str)
    size = Signal(int, bool)           # bytes, complete
    signer = Signal(str, str)          # Authenticode status, signer
    background_done = Signal()
    progress = Signal(str, object)     # template, values
    action_done = Signal(str, bool, str, object)   # "repair" | "modify", ok, template, values


class _Rule(QFrame):
    """A one-pixel rule in the current theme's line colour."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(1)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(current()["line"]))
        p.end()


def _shorten(url: str) -> str:
    return url if len(url) <= LINK_CHARS else url[:LINK_CHARS - 1] + "…"


class DetailsDialog(QDialog):
    ready = Signal()    # the background work (installer family, size, signature) has finished

    def __init__(self, status: AppStatus, parent=None, icon: QPixmap | None = None,
                 updates_running: Callable[[], bool] | bool = False):
        super().__init__(parent)
        self.status, self.app = status, status.app
        self.facts = details.gather(self.app)
        self._updates_running = updates_running if callable(updates_running) else (lambda: updates_running)
        self._icon = icon if icon is not None and not icon.isNull() else letter_avatar(self.app.name, 44)
        self._family = self.facts.installer_kind
        self._family_known = bool(self._family)
        self._size = (self.facts.size_bytes, not self.facts.size_approx)
        self._size_pending = not self.facts.size_bytes and bool(self.facts.install_location)
        self._signer: tuple[str, str] | None = None
        self._busy = ""                      # "repair" | "modify" while one is running
        self._say: tuple[str, dict, str] = ("", {}, "")    # status line: (template, values, tone)
        self._cells: dict[str, ElidedLabel] = {}
        self._started = False
        self._lent = 0                       # height the status strip took from the screen, to give back when it goes
        self._stop = CancelToken()           # ends the background walk when the dialog closes
        self._repair_cancel: CancelToken | None = None
        self.bus = _Bus()
        self.bus.family.connect(self._on_family)
        self.bus.size.connect(self._on_size)
        self.bus.signer.connect(self._on_signer)
        self.bus.background_done.connect(self.ready)
        self.bus.progress.connect(self._on_progress)
        self.bus.action_done.connect(self._on_action_done)

        self.setMinimumWidth(560)
        self._build_shell()
        self.retranslate()
        self._fit()
        QTimer.singleShot(0, self, self.start_background)    # the dialog is the context: deleted first, it never starts

    # -- building ------------------------------------------------------
    @staticmethod
    def _button(name: str = "", slot=None) -> QPushButton:
        b = QPushButton()
        if name:
            b.setObjectName(name)
        b.setCursor(Qt.PointingHandCursor)
        if slot:
            b.clicked.connect(slot)
        return b

    def _build_shell(self):
        self.header = QWidget()
        head = QHBoxLayout(self.header)
        head.setContentsMargins(26, 22, 26, 14)
        head.setSpacing(14)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(44, 44)
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setPixmap(self._icon)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.title = ElidedLabel(self.app.name, "dlgTitle")
        self.title.ensurePolished()    # its font comes from the stylesheet, and the size hint needs it
        self.subtitle = ElidedLabel("", "meta")
        names.addWidget(self.title)
        names.addWidget(self.subtitle)
        head.addWidget(self.icon_label, 0, Qt.AlignTop)
        head.addLayout(names, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.activity = QWidget()
        act = QVBoxLayout(self.activity)
        act.setContentsMargins(26, 10, 26, 0)
        act.setSpacing(6)
        line = QHBoxLayout()
        line.setSpacing(10)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.stop_btn = self._button("link", self._stop_waiting)
        line.addWidget(self.status_label, 1)
        line.addWidget(self.stop_btn, 0, Qt.AlignTop)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setFixedHeight(4)
        self.bar.setTextVisible(False)
        act.addLayout(line)
        act.addWidget(self.bar)

        self.footer = QWidget()
        foot = QHBoxLayout(self.footer)
        foot.setContentsMargins(26, 12, 26, 18)
        self.repair_btn = self._button("", self._on_repair)
        self.modify_btn = self._button("", self._on_modify)
        self.copy_btn = self._button("", self._copy_details)
        self.close_btn = self._button("primary", self.accept)
        self.close_btn.setDefault(True)
        for b in (self.repair_btn, self.modify_btn, self.copy_btn):
            foot.addWidget(b)
        foot.addStretch(1)
        foot.addWidget(self.close_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.header)
        root.addWidget(self.scroll, 1)
        root.addWidget(self.activity)
        root.addWidget(self.footer)

    def _fit(self):
        """Tall enough for everything when the screen allows it; on a small screen the content scrolls."""
        page = self.scroll.widget().layout()
        page.activate()   # height-for-width is unknown until the layout has been set up
        chrome = sum(w.sizeHint().height() for w in (self.header, self.footer)) \
            + (self.activity.sizeHint().height() if not self.activity.isHidden() else 0)
        wanted = (page.totalHeightForWidth(WIDTH) if page.hasHeightForWidth() else page.sizeHint().height()) + chrome
        self.resize(max(self.width(), WIDTH), min(wanted, self._room() or wanted))

    def _room(self) -> int:
        screen = (self.parentWidget() or self).screen() or QGuiApplication.primaryScreen()
        return int(screen.availableGeometry().height() * 0.92) if screen else 0

    def _grow(self, delta: int):
        """The status strip came or went: give its height to the dialog, so the content does not slide under it."""
        height = self.height() + delta
        if delta > 0 and self._room():
            height = min(height, max(self._room(), self.height()))    # never shrink a dialog the user made taller
        self.resize(self.width(), max(self.minimumSizeHint().height(), height))

    # -- text ----------------------------------------------------------
    def retranslate(self):
        """Every visible string again, in the current language: at start and when the language changes."""
        app = self.app
        self.setWindowTitle(tr("Details — {name}", name=app.name))
        meta = [tr("Version {version}", version=app.version)] if app.version else []
        meta.append(app.publisher or tr("Unknown publisher"))
        self.subtitle.set_full_text(" · ".join(meta))
        self.repair_btn.setText(tr("Repair"))
        self.modify_btn.setText(tr("Modify"))
        self.copy_btn.setText(tr("Copy details"))
        self.close_btn.setText(tr("Close"))
        self.stop_btn.setText(tr("Stop waiting"))
        self.stop_btn.setToolTip(tr("Upstream stops waiting for Windows Installer. The repair itself is not stopped."))
        old = self.scroll.takeWidget()
        self.scroll.setWidget(self._build_content())
        if old is not None:
            old.deleteLater()
        self._refresh_buttons()
        self._show_status()

    def _general_rows(self) -> list[_Row]:
        f = self.facts
        arch = ARCH_LABELS.get(f.arch)
        return [
            _Row("arch", tr("Architecture"), tr(arch) if arch else (f.arch.upper() or tr("Unknown"))),
            _Row("scope", tr("Installed for"), tr(SCOPE_LABELS[f.scope]) if f.scope in SCOPE_LABELS else tr("Unknown")),
            self._family_row(),
            _Row("date", tr("Installed on"), num(f.install_date) if f.install_date else tr("Unknown")),
            self._size_row(),
            self._signer_row(),
        ]

    def _family_row(self) -> _Row:
        label = details.family_label(self._family)
        if label:
            return _Row("family", tr("Installer"), label)
        if not self._family_known:
            return _Row("family", tr("Installer"), tr("Checking…"), "muted")
        return _Row("family", tr("Installer"), tr("Unknown"))

    def _size_row(self) -> _Row:
        size, complete = self._size
        if size:
            text = num(details.format_size(size))
            return _Row("size", tr("Size on disk"), text if complete else tr("At least {size}", size=text))
        if self._size_pending:
            return _Row("size", tr("Size on disk"), tr("Calculating…"), "muted")
        return _Row("size", tr("Size on disk"), tr("Unknown"))

    def _signer_row(self) -> _Row:
        label = tr("Signature")
        if self._signer is None:
            return _Row("signer", label, tr("Checking…"), "muted")
        status, signer = self._signer
        if status == "Valid" and signer:
            return _Row("signer", label, tr("Signed by {signer}", signer=signer))
        if status == "NotSigned":
            return _Row("signer", label, tr("Not signed"))
        if status == "HashMismatch":
            return _Row("signer", label, tr("The signature is broken: the file was changed after it was signed"), "danger")
        if status in ("NotTrusted", "UnknownError"):
            text = tr("Signed by {signer}, but not trusted by Windows", signer=signer) if signer \
                else tr("Windows doesn't trust the signature")
            return _Row("signer", label, text, "danger")
        if status == "NoFile":
            return _Row("signer", label, tr("No program file to check"), "muted")
        return _Row("signer", label, tr("Couldn't check the signature"), "muted")

    def _update_rows(self) -> list[_Row]:
        st, app, upd = self.status, self.app, self.status.update
        rows = [_Row("installed", tr("Installed version"), app.version or tr("Unknown"))]
        if upd:
            rows.append(_Row("latest", tr("Latest version"), upd.version, "accent"))
            rows.append(_Row("source", tr("Source"), tr("{label} — {origin}", label=tr(SOURCE_LABELS.get(upd.source, "")),
                                                        origin=upd.origin)))
        else:
            text = st.note_text() if st.note else tr("Up to date") if st.repo or st.winget_id else tr("No source found")
            rows.append(_Row("latest", tr("Latest version"), text, "muted"))
        if st.repo and not (upd and upd.source == "github"):
            rows.append(_Row("repo", tr("GitHub repository"), st.repo))
        if st.winget_id and not (upd and upd.source == "winget"):
            rows.append(_Row("winget", tr("winget ID"), st.winget_id))
        return rows

    def _command_text(self) -> str:
        return self.facts.uninstall_string

    def report_text(self) -> str:
        """Everything the dialog shows, as plain text for the clipboard."""
        app, f, upd = self.app, self.facts, self.status.update
        lines = [f"{app.name} {app.version}".strip(), app.publisher or tr("Unknown publisher"), ""]
        lines += [f"{r.label}: {r.text}" for r in self._general_rows()]
        lines.append(f"{tr('Location')}: {f.install_location or tr('Unknown')}")
        lines += ["", *(f"{r.label}: {r.text}" for r in self._update_rows())]
        if upd and upd.notes:
            lines += ["", f"{tr('Release notes')}:", upd.notes[:NOTES_LIMIT].strip()]
        links = details.web_links(app, self.status.repo, upd.page_url if upd else None)
        if links:
            lines += ["", f"{tr('Links')}:", *links]
        if self._command_text():
            lines += ["", f"{tr('Uninstall command')}:", self._command_text()]
        return "\n".join(lines)

    # -- content -------------------------------------------------------
    def _build_content(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(26, 6, 26, 14)
        lay.setSpacing(8)
        self._cells = {}
        self._heading(lay, tr("General"), rule=False)
        lay.addLayout(self._grid(self._general_rows()))

        self._heading(lay, tr("Location"))
        folder = self.facts.install_location
        if folder:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(self._read_only(folder), 1)
            open_btn = self._button("", self._open_folder)
            open_btn.setText(tr("Open install folder"))
            row.addWidget(open_btn)
            lay.addLayout(row)
        else:
            lay.addWidget(self._muted(tr("No install folder is recorded for this app.")))

        self._heading(lay, tr("Updates"))
        lay.addLayout(self._grid(self._update_rows()))
        upd = self.status.update
        if upd and upd.notes:
            lay.addSpacing(2)
            lay.addWidget(self._muted(tr("Release notes")))
            notes = upd.notes[:NOTES_LIMIT].strip()
            box = QPlainTextEdit(notes)
            box.setReadOnly(True)
            box.setFixedHeight(NOTES_HEIGHT)
            box.setLayoutDirection(direction_of(notes))
            lay.addWidget(box)

        links = details.web_links(self.app, self.status.repo, upd.page_url if upd else None)
        if links:
            self._heading(lay, tr("Links"))
            for url in links:
                lay.addWidget(self._link(url))

        self._heading(lay, tr("Uninstall command"))
        command = self._command_text()
        if command:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(self._read_only(command), 1)
            copy_btn = self._button("", lambda: self._copy_command(command))
            copy_btn.setText(tr("Copy"))
            row.addWidget(copy_btn)
            lay.addLayout(row)
        else:
            lay.addWidget(self._muted(tr("No uninstall command is registered with Windows.")))
        lay.addStretch(1)
        return page

    def _heading(self, lay: QVBoxLayout, text: str, rule: bool = True):
        if rule:
            lay.addSpacing(8)
            lay.addWidget(_Rule())
            lay.addSpacing(4)
        title = QLabel(text)
        title.setObjectName("section")
        lay.addWidget(title)

    def _grid(self, rows: list[_Row]) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)
        grid.setColumnMinimumWidth(0, LABEL_WIDTH)
        grid.setColumnStretch(1, 1)
        for i, row in enumerate(rows):
            label = QLabel(row.label)
            label.setProperty("tone", "muted")
            value = ElidedLabel()
            self._cells[row.key] = value
            self._set_cell(value, row)
            grid.addWidget(label, i, 0, Qt.AlignTop)
            grid.addWidget(value, i, 1)
        return grid

    @staticmethod
    def _set_cell(cell: ElidedLabel, row: _Row):
        cell.set_full_text(row.text)
        cell.setProperty("tone", row.tone)
        repolish(cell)

    @staticmethod
    def _read_only(text: str) -> QLineEdit:
        edit = QLineEdit(text)
        edit.setReadOnly(True)
        edit.setLayoutDirection(Qt.LeftToRight)
        edit.setCursorPosition(0)
        return edit

    @staticmethod
    def _muted(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("tone", "muted")
        label.setWordWrap(True)
        return label

    def _link(self, url: str) -> QLabel:
        label = QLabel(f'<a href="{html.escape(url, quote=True)}">{html.escape(_shorten(url))}</a>')
        label.setTextFormat(Qt.RichText)
        label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        label.setOpenExternalLinks(False)
        label.setLayoutDirection(Qt.LeftToRight)
        label.setAlignment((Qt.AlignRight if ui_is_rtl() else Qt.AlignLeft) | Qt.AlignVCenter)
        label.setToolTip(url)
        label.linkActivated.connect(self._open_link)
        return label

    # -- live parts ----------------------------------------------------
    def _refresh_cells(self):
        for row in (self._family_row(), self._size_row(), self._signer_row()):
            cell = self._cells.get(row.key)
            if cell is not None:
                self._set_cell(cell, row)

    def start_background(self):
        """Once: the work that is too slow for the UI thread. The timer in __init__ calls it after the first paint."""
        if self._started:
            return
        self._started = True
        folder = self.facts.install_location if self._size_pending else ""
        threading.Thread(target=self._background, args=(self.bus, self.app, folder, self._stop), daemon=True).start()

    @staticmethod
    def _background(bus: _Bus, app, folder: str, stop: CancelToken):
        try:
            bus.family.emit(details.installer_family(app))
            if folder:
                bus.size.emit(*details.folder_size(folder, cancel=stop))
            bus.signer.emit(*details.signer_of(app))
        except Exception:
            log.exception("%s: the details could not be completed", app.name)
            bus.signer.emit("Unknown", "")
        bus.background_done.emit()

    def _on_family(self, kind: str):
        self._family, self._family_known = kind, True
        self._refresh_cells()

    def _on_size(self, size: int, complete: bool):
        self._size, self._size_pending = (size, complete), False
        self._refresh_cells()

    def _on_signer(self, status: str, signer: str):
        self._signer = (status, signer)
        self._refresh_cells()

    # -- buttons and actions -------------------------------------------
    def _refresh_buttons(self):
        busy = bool(self._busy)
        self._sync_button(self.repair_btn, self.facts.can_repair and not busy, details.repair_blocker(self.app))
        self._sync_button(self.modify_btn, self.facts.can_modify and not busy, details.modify_blocker(self.app))

    @staticmethod
    def _sync_button(button: QPushButton, enabled: bool, blocker: tuple[str, dict] | None):
        button.setEnabled(enabled)
        button.setToolTip(render(*blocker) if blocker else "")

    def _say_status(self, template: str, values: dict | None = None, tone: str = ""):
        self._say = (template, values or {}, tone)
        self._show_status()

    def _show_status(self):
        template, values, tone = self._say
        self.status_label.setText(render(template, values))
        self.status_label.setProperty("tone", tone)
        repolish(self.status_label)
        self.status_label.setVisible(bool(template))
        self.stop_btn.setVisible(self._busy == "repair")
        self.bar.setVisible(bool(self._busy))
        shown, was = bool(template) or bool(self._busy), self.activity.isVisibleTo(self)
        self.activity.setVisible(shown)
        if self.isVisible() and shown != was:
            before = self.height()
            self._grow(self.activity.sizeHint().height() if shown else -self._lent)
            self._lent = max(self.height() - before, 0) if shown else 0

    def _begin(self, kind: str, template: str, values: dict | None = None):
        self._busy = kind
        self._refresh_buttons()
        self._say_status(template, values, "muted")

    def _confirm_repair(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(tr("Repair {name}", name=self.app.name))
        box.setText(tr("Windows Installer will reinstall all the files and registry entries of {name}. Settings kept "
                       "outside the app's folder are not touched. Windows may ask for permission.", name=self.app.name))
        go = box.addButton(tr("Repair"), QMessageBox.AcceptRole)
        keep = box.addButton(tr("Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is go

    def _hold_for_updates(self) -> bool:
        if not self._updates_running():
            return False
        self._say_status(N_("Updates are running right now. Wait until they finish, then try again."), tone="muted")
        return True

    def _on_repair(self):
        if self._busy or not self.facts.can_repair or self._hold_for_updates() or not self._confirm_repair():
            return
        self._repair_cancel = token = CancelToken()
        self._begin("repair", N_("Repairing {name}…"), {"name": self.app.name})

        def work(bus: _Bus, app):
            try:
                ok, template, values = details.repair(app, lambda t, **v: bus.progress.emit(t, v), token)
            except Exception as e:
                log.exception("%s: repair failed", app.name)
                ok, template, values = False, N_("{error}"), {"error": str(e)}
            bus.action_done.emit("repair", ok, template, values)

        threading.Thread(target=work, args=(self.bus, self.app), daemon=True).start()

    def _on_modify(self):
        if self._busy or not self.facts.can_modify or self._hold_for_updates():
            return
        self._begin("modify", N_("Opening the Modify window of {name}…"), {"name": self.app.name})

        def work(bus: _Bus, app):
            try:
                ok, template, values = details.modify(app)
            except Exception as e:
                log.exception("%s: modify failed", app.name)
                ok, template, values = False, N_("{error}"), {"error": str(e)}
            bus.action_done.emit("modify", ok, template, values)

        threading.Thread(target=work, args=(self.bus, self.app), daemon=True).start()

    def _stop_waiting(self):
        if self._repair_cancel is not None:
            self._repair_cancel.cancel()
            self.stop_btn.hide()

    def _on_progress(self, template: str, values: dict):
        self._say_status(template, values, "muted")

    def _on_action_done(self, kind: str, ok: bool, template: str, values: dict):
        self._busy, self._repair_cancel = "", None
        self._refresh_buttons()
        tone = "accent" if ok else "muted" if template in details.NEUTRAL else "danger"
        self._say_status(template, values, tone)

    def _copy_details(self):
        QGuiApplication.clipboard().setText(self.report_text())
        self._say_status(N_("Copied to the clipboard."), tone="accent")

    def _copy_command(self, command: str):
        QGuiApplication.clipboard().setText(command)
        self._say_status(N_("Copied to the clipboard."), tone="accent")

    def _open_folder(self):
        folder = self.facts.install_location
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    @staticmethod
    def _open_link(url: str):
        if url.lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))

    # -- closing and events ---------------------------------------------
    def changeEvent(self, event):
        if event.type() == QEvent.LanguageChange and hasattr(self, "scroll"):
            self.retranslate()
        super().changeEvent(event)

    def done(self, result: int):
        self._stop.cancel()
        if self._repair_cancel is not None:     # the repair carries on in Windows Installer; only the waiting ends
            self._repair_cancel.cancel()
        super().done(result)
