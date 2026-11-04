"""Settings: language, theme (live preview cards) and source options."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from ..i18n import LANGUAGES, tr
from .theme import THEME_ORDER, THEMES, current


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


class SettingsDialog(QDialog):
    language_changed = Signal(str)
    theme_changed = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.sources_dirty = False
        self.setMinimumWidth(640)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        self.lang_title = QLabel()
        self.lang_title.setObjectName("section")
        self.lang = QComboBox()
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

        lay.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.done_btn = QPushButton()
        self.done_btn.setObjectName("primary")
        self.done_btn.setDefault(True)
        self.done_btn.clicked.connect(self.accept)
        buttons.addWidget(self.done_btn)
        lay.addLayout(buttons)
        self.retranslate()

    def retranslate(self):
        self.setWindowTitle(tr("Settings"))
        self.lang_title.setText(tr("Language"))
        self.theme_title.setText(tr("Theme"))
        self.src_title.setText(tr("Sources"))
        self.use_gh.setText(tr("Use the GitHub CLI (gh) token when signed in"))
        self.pre.setText(tr("Offer pre-releases (beta, rc)"))
        self.src_hint.setText(tr("Source changes apply on the next check."))
        self.done_btn.setText(tr("Done"))
        for card in self.cards:
            card.setAccessibleName(card.label())
            card.update()

    def _on_language(self, index: int):
        self.language_changed.emit(self.lang.itemData(index))
        self.retranslate()

    def _on_theme(self, key: str):
        self.theme_changed.emit(key)
        for card in self.cards:
            card.update()

    def _set_source(self, key: str, value: bool):
        self.settings.set(key, value)
        self.settings.save()
        self.sources_dirty = True
