"""Settings: language, theme (live preview cards), source options, install safety, background, history,
security badges and leftover cleanup.

Every control applies at once: it writes the settings store and saves, like the checkboxes always did."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .. import background, history
from ..i18n import LANGUAGES, N_, tr, trn
from .theme import THEME_ORDER, THEMES, current, repolish

log = logging.getLogger("upstream.ui.settings")

# (settings key, title, one-line explanation, [(stored value, label)]): the first value is the default
POLICIES = (
    ("publisher_policy", N_("Publisher check"),
     N_("When an update isn't verifiably signed by the app's usual publisher."),
     (("ask", N_("Ask me")), ("block", N_("Block the update")), ("off", N_("Don't check")))),
    ("close_apps_policy", N_("Running apps"),
     N_("When an app is open while it is being updated."),
     (("ask", N_("Ask me")), ("auto", N_("Close it automatically")), ("never", N_("Leave it to the installer")))),
    ("metered_policy", N_("Metered connection"),
     N_("When Windows marks your connection as metered (limited data)."),
     (("ask", N_("Ask me")), ("skip", N_("Skip the update")), ("ignore", N_("Update anyway")))),
    ("battery_policy", N_("Battery"),
     N_("When your PC is unplugged and the battery is low or battery saver is on."),
     (("ask", N_("Ask me")), ("skip", N_("Skip the update")), ("ignore", N_("Update anyway")))),
)
PROXY_MODES = (("system", N_("Use Windows settings")), ("none", N_("No proxy")), ("custom", N_("Custom address")))
PROXY_EXAMPLE = "http://proxy.example.com:8080"
TIMEOUT_MINUTES = (0, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240)   # 0: wait for ever
DEFAULT_TIMEOUT = 30
CHECK_HOURS = (0, 1, 3, 6, 12, 24)                    # 0: no scheduled check
BACKUP_DAYS = (7, 14, 30, 60, 90, 180, 365)
DEFAULT_BACKUP_DAYS = 30
CONTROL_WIDTH = 230


def proxy_address(text: str) -> str | None:
    """'host:port' or an http(s) URL, optionally with user:pass@, as a full URL; None if it cannot be a proxy."""
    text = text.strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path.strip("/") or parts.query or parts.fragment:
        return None
    return text.rstrip("/")


def _timeout_minutes(value) -> int:
    """The stored limit as whole minutes; a hand-edited value that makes no sense becomes the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return DEFAULT_TIMEOUT
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):
        return DEFAULT_TIMEOUT


def _count(value, default: int) -> int:
    """A stored whole number (hours, days); a hand-edited value that makes no sense becomes the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):
        return default


def _draw_preview(p: QPainter, area: QRectF, t: dict):
    """A miniature app row in the theme's colours."""
    p.fillRect(area, QColor(t["bg"]))
    row = QRectF(area.left() + 9, area.top() + 12, area.width() - 18, 32)
    p.setPen(QPen(QColor(t["line"]), 1))
    p.setBrush(QColor(t["surface"]))
    p.drawRoundedRect(row, 6, 6)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(t["muted"]))
    p.drawRoundedRect(QRectF(row.left() + 7, row.top() + 9, 14, 14), 3, 3)
    p.setBrush(QColor(t["text"]))
    p.drawRoundedRect(QRectF(row.left() + 27, row.top() + 10, 34, 4), 2, 2)
    p.setBrush(QColor(t["muted"]))
    p.drawRoundedRect(QRectF(row.left() + 27, row.top() + 18, 22, 4), 2, 2)
    p.setBrush(QColor(t["accent"]))
    p.drawRoundedRect(QRectF(row.right() - 27, row.top() + 11, 20, 10), 5, 5)


class ThemeCard(QAbstractButton):
    PREVIEW_H = 58

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(QSize(132, 92))
        self.setAttribute(Qt.WA_Hover)

    def label(self) -> str:
        return tr("System") if self.key == "system" else tr(THEMES[self.key]["label"])

    def paintEvent(self, event):
        cur = current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(outer, 10, 10)
        p.setClipPath(clip)

        preview = QRectF(outer.left(), outer.top(), outer.width(), self.PREVIEW_H)
        if self.key == "system":
            half = preview.width() / 2
            left = QRectF(preview.left(), preview.top(), half, preview.height())
            right = QRectF(preview.left() + half, preview.top(), half, preview.height())
            p.save()
            p.setClipRect(left, Qt.IntersectClip)
            _draw_preview(p, preview, THEMES["paper"])
            p.restore()
            p.save()
            p.setClipRect(right, Qt.IntersectClip)
            _draw_preview(p, preview, THEMES["graphite"])
            p.restore()
        else:
            _draw_preview(p, preview, THEMES[self.key])

        caption = QRectF(outer.left(), preview.bottom(), outer.width(), outer.height() - self.PREVIEW_H)
        p.fillRect(caption, QColor(cur["surface"]))
        p.setPen(QColor(cur["text"] if self.isChecked() else cur["muted"]))
        font = p.font()
        font.setBold(self.isChecked())
        p.setFont(font)
        p.drawText(caption, Qt.AlignCenter, self.label())

        p.setClipping(False)
        if self.isChecked():
            border, width = cur["accent"], 2
        elif self.underMouse() or self.hasFocus():
            border, width = cur["muted"], 1
        else:
            border, width = cur["line"], 1
        p.setPen(QPen(QColor(border), width))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(outer.adjusted(width / 2, width / 2, -width / 2, -width / 2), 10, 10)
        p.end()


class _Rule(QFrame):
    """A one-pixel rule in the current theme's line colour."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(1)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(current()["line"]))
        p.end()


class _Combo(QComboBox):
    """The wheel scrolls the page and never changes a value on its way past (these are safety settings)."""

    def wheelEvent(self, event):
        event.ignore()


class _Setting:
    """A title, a one-line explanation and the control they describe."""

    def __init__(self, title: str, hint: str, control: QWidget):
        self.title_text, self.hint_text, self.control = title, hint, control
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setBuddy(control)
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        control.setFixedWidth(CONTROL_WIDTH)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.title)
        text.addWidget(self.hint)
        self.layout = QHBoxLayout()
        self.layout.setContentsMargins(0, 4, 0, 4)
        self.layout.setSpacing(20)
        self.layout.addLayout(text, 1)
        self.layout.addWidget(control, 0, Qt.AlignVCenter)

    def retranslate(self):
        self.title.setText(tr(self.title_text))
        self.hint.setText(tr(self.hint_text))
        self.control.setAccessibleName(tr(self.title_text))
        self.control.setAccessibleDescription(tr(self.hint_text))


class _Toggle:
    """A check box with its one-line explanation under it."""

    def __init__(self, title: str, hint: str):
        self.title_text, self.hint_text = title, hint
        self.box = QCheckBox()
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        note = QHBoxLayout()
        note.addSpacing(26)     # the check box and its gap: the hint lines up with the text, also when mirrored
        note.addWidget(self.hint, 1)
        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(0, 4, 0, 4)
        self.layout.setSpacing(2)
        self.layout.addWidget(self.box)
        self.layout.addLayout(note)

    def retranslate(self):
        self.box.setText(tr(self.title_text))
        self.hint.setText(tr(self.hint_text))
        self.box.setAccessibleDescription(tr(self.hint_text))


class SettingsDialog(QDialog):
    language_changed = Signal(str)
    theme_changed = Signal(str)
    background_changed = Signal()       # tray, schedule, notifications or start-up changed: Background re-reads them
    history_cleared = Signal()
    badges_changed = Signal(bool)       # security badges switched: the rows show or drop them at once
    backups_requested = Signal()        # the Backups… button: the window opens the dialog

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.sources_dirty = False
        self._save_error = ""
        self._note = ("", "")   # the proxy note: (template, tone)
        self._autostart_failed = False
        self.toggles: dict[str, _Toggle] = {}
        self.setMinimumWidth(640)

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(26, 22, 26, 10)
        lay.setSpacing(10)

        self.lang_title = QLabel()
        self.lang_title.setObjectName("section")
        self.lang = _Combo()
        for code, name in LANGUAGES.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(settings.get("language", "en"))))
        self.lang.currentIndexChanged.connect(self._on_language)
        lang_row = QHBoxLayout()
        lang_row.addWidget(self.lang)
        lang_row.addStretch(1)
        lay.addWidget(self.lang_title)
        lay.addLayout(lang_row)
        lay.addSpacing(10)

        self.theme_title = QLabel()
        self.theme_title.setObjectName("section")
        lay.addWidget(self.theme_title)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards: list[ThemeCard] = []
        group = QButtonGroup(self)
        chosen = settings.get("theme", "system")
        for i, key in enumerate(THEME_ORDER):
            card = ThemeCard(key)
            card.setChecked(key == chosen)
            card.clicked.connect(lambda _=False, k=key: self._on_theme(k))
            group.addButton(card)
            grid.addWidget(card, i // 4, i % 4)
            self.cards.append(card)
        lay.addLayout(grid)
        lay.addSpacing(10)

        self.src_title = QLabel()
        self.src_title.setObjectName("section")
        lay.addWidget(self.src_title)
        self.use_gh = QCheckBox()
        self.use_gh.setChecked(bool(settings.get("use_gh_cli_token", True)))
        self.use_gh.toggled.connect(lambda v: self._set_source("use_gh_cli_token", v))
        self.pre = QCheckBox()
        self.pre.setChecked(bool(settings.get("include_prereleases", False)))
        self.pre.toggled.connect(lambda v: self._set_source("include_prereleases", v))
        self.src_hint = QLabel()
        self.src_hint.setObjectName("hint")
        for w in (self.use_gh, self.pre, self.src_hint):
            lay.addWidget(w)
        lay.addSpacing(10)

        self.safety_title = QLabel()
        self.safety_title.setObjectName("section")
        lay.addWidget(self.safety_title)
        self.rows: list[_Setting] = []
        self.policy: dict[str, QComboBox] = {}
        for key, title, hint, options in POLICIES:
            combo = self.policy[key] = _Combo()
            for stored, _label in options:
                combo.addItem("", stored)
            combo.setCurrentIndex(max(0, combo.findData(settings.get(key, options[0][0]))))
            combo.currentIndexChanged.connect(lambda _=0, k=key, c=combo: self._store(k, c.currentData()))
            self._add_row(lay, _Setting(title, hint, combo))

        proxy = settings.get("proxy", {})
        proxy = proxy if isinstance(proxy, dict) else {}
        self.proxy_mode = _Combo()
        for mode, _label in PROXY_MODES:
            self.proxy_mode.addItem("", mode)
        self.proxy_mode.setCurrentIndex(max(0, self.proxy_mode.findData(proxy.get("mode"))))
        self._add_row(lay, _Setting(N_("Network proxy"), N_("Used for update checks and downloads."), self.proxy_mode))
        self.proxy_label = QLabel()
        self.proxy_label.setProperty("tone", "muted")
        self.proxy_url = QLineEdit(str(proxy.get("url") or ""))
        self.proxy_url.setPlaceholderText(PROXY_EXAMPLE)
        self.proxy_url.setLayoutDirection(Qt.LeftToRight)
        self._style_proxy_field()
        self.proxy_label.setBuddy(self.proxy_url)
        url_row = QHBoxLayout()
        url_row.setSpacing(12)
        url_row.addWidget(self.proxy_label)
        url_row.addWidget(self.proxy_url, 1)
        lay.addLayout(url_row)
        self.proxy_note = QLabel()
        self.proxy_note.setWordWrap(True)
        self.proxy_note.hide()
        lay.addWidget(self.proxy_note)
        self._on_proxy_mode(store=False)
        self.proxy_mode.currentIndexChanged.connect(lambda _=0: self._on_proxy_mode())
        self.proxy_url.editingFinished.connect(self._commit_proxy)

        saved_minutes = _timeout_minutes(settings.get("install_timeout_min", DEFAULT_TIMEOUT))
        self.timeout = _Combo()
        for minutes in sorted({*TIMEOUT_MINUTES, saved_minutes}):
            self.timeout.addItem("", minutes)
        self.timeout.setCurrentIndex(self.timeout.findData(saved_minutes))
        self.timeout.currentIndexChanged.connect(lambda _=0: self._store("install_timeout_min", self.timeout.currentData()))
        self._add_row(lay, _Setting(
            N_("Installer time limit"),
            N_("How long to wait for one installer. After that Upstream moves on; the installer is not stopped."),
            self.timeout))

        self.safety_hint = QLabel()
        self.safety_hint.setObjectName("hint")
        lay.addWidget(self.safety_hint)
        lay.addSpacing(10)

        self.background_title = QLabel()
        self.background_title.setObjectName("section")
        lay.addWidget(self.background_title)
        self._add_toggle(lay, "tray_enabled", N_("Show an icon in the notification area"),
                         N_("Open Upstream, check for updates and update everything from next to the clock."), False)
        self._add_toggle(lay, "close_to_tray", N_("Keep running when the window is closed"),
                         N_("Closing the window hides it. Quit from the icon's menu."), False)
        self._add_toggle(lay, "start_with_windows", N_("Start with Windows"),
                         N_("Starts minimized, so scheduled checks run without opening the window."), False,
                         checked=background.is_autostart())
        self.autostart_note = QLabel()
        self.autostart_note.setProperty("tone", "danger")
        self.autostart_note.setWordWrap(True)
        self.autostart_note.hide()
        lay.addWidget(self.autostart_note)
        saved_hours = _count(settings.get("check_interval_hours", 0), 0)
        self.check_hours = _Combo()
        for hours in sorted({*CHECK_HOURS, saved_hours}):
            self.check_hours.addItem("", hours)
        self.check_hours.setCurrentIndex(self.check_hours.findData(saved_hours))
        self.check_hours.currentIndexChanged.connect(
            lambda _=0: self._on_toggle("check_interval_hours", self.check_hours.currentData()))
        self._add_row(lay, _Setting(N_("Check for updates automatically"),
                                    N_("How often Upstream looks in the background while it is running."), self.check_hours))
        self._add_toggle(lay, "notify_updates", N_("Tell me about new updates"),
                         N_("A message from the notification area when a check finds updates, once for each version."), True)
        self.tray_note = QLabel()
        self.tray_note.setObjectName("hint")
        self.tray_note.setWordWrap(True)
        self.tray_note.setVisible(not QSystemTrayIcon.isSystemTrayAvailable())
        lay.addWidget(self.tray_note)
        lay.addSpacing(10)

        self.history_title = QLabel()
        self.history_title.setObjectName("section")
        lay.addWidget(self.history_title)
        self._add_toggle(lay, "history_enabled", N_("Keep a history"),
                         N_("A list of what Upstream installed, updated and removed. It stays on this PC."), True)
        self.clear_history_btn = QPushButton()
        self.clear_history_btn.clicked.connect(self._clear_history)
        self._add_row(lay, _Setting(N_("Clear history"), N_("Deletes the whole list. Your apps and files are not touched."),
                                    self.clear_history_btn))
        lay.addSpacing(10)

        self.badges_title = QLabel()
        self.badges_title.setObjectName("section")
        lay.addWidget(self.badges_title)
        self._add_toggle(lay, "security_badges", N_("Show security and end-of-life badges"),
                         N_("Marks updates whose notes mention security fixes, and versions that no longer get fixes."), True)
        lay.addSpacing(10)

        self.cleanup_title = QLabel()
        self.cleanup_title.setObjectName("section")
        lay.addWidget(self.cleanup_title)
        self._add_toggle(lay, "cleanup_restore_point", N_("Create a restore point first"),
                         N_("Asks Windows for a restore point before leftovers are removed. It needs System Protection "
                            "and administrator rights, and Windows allows only one a day."), True)
        saved_days = _count(settings.get("cleanup_backup_days", DEFAULT_BACKUP_DAYS), DEFAULT_BACKUP_DAYS)
        self.backup_days = _Combo()
        for days in sorted({*BACKUP_DAYS, saved_days}):
            self.backup_days.addItem("", days)
        self.backup_days.setCurrentIndex(self.backup_days.findData(saved_days))
        self.backup_days.currentIndexChanged.connect(
            lambda _=0: self._on_toggle("cleanup_backup_days", self.backup_days.currentData()))
        self._add_row(lay, _Setting(N_("Keep backups for"),
                                    N_("Leftovers that were removed are kept this long, so a cleanup can be undone."),
                                    self.backup_days))
        self.backups_btn = QPushButton()
        self.backups_btn.clicked.connect(self.backups_requested)
        self._add_row(lay, _Setting(N_("Backups"), N_("Restore or delete what earlier cleanups removed."), self.backups_btn))
        lay.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidget(content)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.save_error = QLabel()
        self.save_error.setProperty("tone", "danger")
        self.save_error.setWordWrap(True)
        self.save_error.hide()
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.done_btn = QPushButton()
        self.done_btn.setObjectName("primary")
        self.done_btn.setDefault(True)
        self.done_btn.clicked.connect(self.accept)
        buttons.addWidget(self.done_btn)
        self.bar = QWidget()
        bar = QVBoxLayout(self.bar)
        bar.setContentsMargins(26, 12, 26, 18)
        bar.addWidget(self.save_error)
        bar.addLayout(buttons)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(_Rule())   # shows that the settings continue when they scroll
        outer.addWidget(self.bar)
        self.retranslate()
        self._fit(content)

    def _add_row(self, lay: QVBoxLayout, row: _Setting):
        self.rows.append(row)
        lay.addLayout(row.layout)

    def _fit(self, content: QWidget):
        """Tall enough for everything when the screen allows it; on a small screen the settings scroll."""
        width = max(self.minimumWidth(), self.sizeHint().width())
        content.layout().activate()   # height-for-width is unknown until the layout has been set up
        wanted = content.layout().totalHeightForWidth(width) + self.bar.sizeHint().height() + 1
        screen = (self.parentWidget() or self).screen() or QGuiApplication.primaryScreen()
        room = int(screen.availableGeometry().height() * 0.92) if screen else wanted
        self.resize(width, min(wanted, room))

    def retranslate(self):
        self.setWindowTitle(tr("Settings"))
        self.lang_title.setText(tr("Language"))
        self.theme_title.setText(tr("Theme"))
        self.src_title.setText(tr("Sources"))
        self.use_gh.setText(tr("Use the GitHub CLI (gh) token when signed in"))
        self.pre.setText(tr("Offer pre-releases (beta, rc)"))
        self.src_hint.setText(tr("Source changes apply on the next check."))
        self.safety_title.setText(tr("Install safety"))
        self.safety_hint.setText(tr("These apply from the next update."))
        self.background_title.setText(tr("Background"))
        self.history_title.setText(tr("History"))
        self.badges_title.setText(tr("Security badges"))
        self.cleanup_title.setText(tr("Leftover cleanup"))
        self.tray_note.setText(tr("This PC shows no notification area, so the icon and the messages are not available."))
        self.clear_history_btn.setText(tr("Clear history…"))
        self.backups_btn.setText(tr("Backups…"))
        self._show_autostart_note()
        for toggle in self.toggles.values():
            toggle.retranslate()
        for row in self.rows:
            row.retranslate()
        for i in range(self.check_hours.count()):
            hours = self.check_hours.itemData(i)
            self.check_hours.setItemText(i, trn("Every {n} hour", "Every {n} hours", hours) if hours else tr("Off"))
        for i in range(self.backup_days.count()):
            days = self.backup_days.itemData(i)
            self.backup_days.setItemText(i, trn("{n} day", "{n} days", days))
        for key, _title, _hint, options in POLICIES:
            for i, (_stored, label) in enumerate(options):
                self.policy[key].setItemText(i, tr(label))
        for i, (_mode, label) in enumerate(PROXY_MODES):
            self.proxy_mode.setItemText(i, tr(label))
        for i in range(self.timeout.count()):
            minutes = self.timeout.itemData(i)
            self.timeout.setItemText(i, trn("{n} minute", "{n} minutes", minutes) if minutes else tr("No time limit"))
        self.proxy_label.setText(tr("Proxy address"))
        self.proxy_url.setAccessibleName(tr("Proxy address"))
        self._show_note(*self._note)
        self._show_save_error(self._save_error)
        self.done_btn.setText(tr("Done"))
        for card in self.cards:
            card.setAccessibleName(card.label())
            card.update()

    def _on_language(self, index: int):
        self.language_changed.emit(self.lang.itemData(index))
        self.retranslate()

    def _on_theme(self, key: str):
        self.theme_changed.emit(key)
        self._style_proxy_field()
        for card in self.cards:
            card.update()

    def _style_proxy_field(self):
        """The field reads as off in every theme: no fill, muted text (the theme's own rule keeps full contrast)."""
        self.proxy_url.setStyleSheet(f"QLineEdit:disabled {{ background: transparent; color: {current()['muted']}; }}")

    def _set_source(self, key: str, value: bool):
        self._store(key, value)
        self.sources_dirty = True

    # -- background, history, badges, cleanup ---------------------------
    def _add_toggle(self, lay: QVBoxLayout, key: str, title: str, hint: str, default: bool, checked: bool | None = None):
        toggle = self.toggles[key] = _Toggle(title, hint)
        toggle.box.setChecked(bool(self.settings.get(key, default)) if checked is None else checked)
        toggle.box.toggled.connect(lambda on, k=key: self._on_toggle(k, on))
        lay.addLayout(toggle.layout)

    def _on_toggle(self, key: str, value):
        """A switch or a number in these sections: store it, then tell whoever has to act on it."""
        if key == "start_with_windows":
            self._set_autostart(bool(value))
            return
        self._store(key, value)
        if key in ("tray_enabled", "close_to_tray", "check_interval_hours", "notify_updates"):
            self.background_changed.emit()
        elif key == "security_badges":
            self.badges_changed.emit(bool(value))

    def _set_autostart(self, on: bool):
        """The Run entry is the truth: the box shows what Windows will really do, and the setting follows it."""
        background.set_autostart(on)
        actual = background.is_autostart()
        self._autostart_failed = actual != on
        if self._autostart_failed:
            box = self.toggles["start_with_windows"].box
            box.blockSignals(True)
            box.setChecked(actual)
            box.blockSignals(False)
        self._show_autostart_note()
        self._store("start_with_windows", actual)
        self.background_changed.emit()

    def _show_autostart_note(self):
        self.autostart_note.setText(tr("Couldn't change the Windows startup entry.") if self._autostart_failed else "")
        self.autostart_note.setVisible(self._autostart_failed)

    def _clear_history(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(tr("Settings"))
        box.setText(tr("Clear the whole history?"))
        box.setInformativeText(tr("This deletes the record of what Upstream installed, updated and removed. "
                                  "Your apps and files are not touched."))
        clear = box.addButton(tr("Clear history"), QMessageBox.AcceptRole)
        keep = box.addButton(tr("Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is clear:
            history.clear()
            self.history_cleared.emit()

    def _store(self, key: str, value):
        self.settings.set(key, value)
        try:
            self.settings.save()
        except OSError as e:
            log.warning("Could not save the settings: %s", e)
            self._show_save_error(str(e))
        else:
            self._show_save_error("")

    def _show_save_error(self, error: str):
        self._save_error = error
        self.save_error.setText(tr("Couldn't save the settings: {error}", error=error) if error else "")
        self.save_error.setVisible(bool(error))

    # -- proxy ---------------------------------------------------------
    def _on_proxy_mode(self, store: bool = True):
        mode = self.proxy_mode.currentData()
        self.proxy_label.setEnabled(mode == "custom")
        self.proxy_url.setEnabled(mode == "custom")
        if mode == "custom":
            if store:
                self._commit_proxy()
            else:
                self._checked_proxy()
        else:
            self._show_note("", "")
            if store:
                self._store("proxy", {"mode": mode, "url": self.proxy_url.text().strip()})

    def _checked_proxy(self) -> str | None:
        """The typed address as a full URL, or None with a note saying what is missing."""
        text = self.proxy_url.text().strip()
        url = proxy_address(text)
        if url is None:
            self._show_note(N_("Enter the proxy as http://host:port. The previous setting stays in use until then."),
                            "danger" if text else "muted")
        else:
            self._show_note("", "")
        return url

    def _commit_proxy(self):
        """Saves a custom proxy once its address is usable; until then the earlier setting stays in force."""
        if self.proxy_mode.currentData() != "custom":
            return
        url = self._checked_proxy()
        if url is None:
            return
        self.proxy_url.setText(url)
        value = {"mode": "custom", "url": url}
        if self.settings.get("proxy") != value:
            self._store("proxy", value)

    def _show_note(self, template: str, tone: str):
        self._note = (template, tone)
        self.proxy_note.setText(tr(template) if template else "")
        self.proxy_note.setProperty("tone", tone)
        repolish(self.proxy_note)
        self.proxy_note.setVisible(bool(template))

    def done(self, result: int):
        self._commit_proxy()
        super().done(result)
