"""The app row and the 'update source' dialog."""
from __future__ import annotations

import os
import re
import time
import unicodedata

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
    QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import pins
from ..details import format_size
from ..github import repo_from_urls
from ..i18n import SOURCE_LABELS, isolate_lines, num, render, tr
from ..scanner import AppStatus
from .theme import current, repolish

# Update, Cancel and Retry sit in the same spot of the row. For this long after one replaces another a click on
# the newcomer is ignored: it is the second half of a double-click aimed at the old control.
CLICK_GUARD = 0.4

GAP = 6                  # between the pieces of the publisher line
CHIP_PAD = 6
CHIP_MAX = 170           # a longer chip is elided
PUBLISHER_MIN = 96       # chips give way before the publisher is squeezed below this
CHIP_ORDER = ("security", "eol", "outdated", "pinned", "duplicate")
LOUD = ("security", "eol", "more")     # drawn in a heavier weight; 'more' (the +N chip) takes the look of what it hides

# The row's columns. Below NARROW_ROW px the size and date columns go and the row packs tighter; whatever the width,
# the name keeps NAME_MIN px by taking them back from the two versions, each of which is elided past VERSION_MAX.
NARROW_ROW = 900
NAME_MIN = 160
VERSION_MAX = 150
VERSION_MIN = 40
ACTIONS_WIDE, ACTIONS_NARROW = 240, 200
GAP_WIDE, GAP_NARROW = 14, 10
VERSION_GAP = 8          # between the old version, the arrow and the new version


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


class VersionLabel(ElidedLabel):
    """A version: left-to-right in any UI direction, as wide as its text up to the maximum width the row grants it,
    then cut with an ellipsis (the whole text is the tooltip)."""

    def __init__(self, name: str = ""):
        super().__init__("", name)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)     # Ignored would ask the layout for no room

    def set_full_text(self, text: str):
        super().set_full_text(text)
        self.setLayoutDirection(Qt.LeftToRight)
        self.setAccessibleName(text)        # a QLabel with text is read aloud; this one paints its text itself


def share_room(wants: list[int], room: int) -> list[int]:
    """Split `room` px between labels that want `wants` px each: a short one keeps its whole width and the longer
    ones share what is left. Never above VERSION_MAX, and never below VERSION_MIN (or the width they want, if less)."""
    wants = [min(w, VERSION_MAX) for w in wants]
    out, left = [0] * len(wants), room
    for done, i in enumerate(sorted(range(len(wants)), key=wants.__getitem__)):
        out[i] = min(wants[i], left // (len(wants) - done))
        left -= out[i]
    return [max(min(VERSION_MIN, want), w) for want, w in zip(wants, out)]


# -- chips: the small tags on the publisher line ----------------------------------------------------

def _mix(a: str, b: str, amount: float) -> QColor:
    """`amount` of colour a over colour b."""
    ca, cb = QColor(a), QColor(b)
    return QColor(*(round(x * amount + y * (1 - amount)) for x, y in
                    ((ca.red(), cb.red()), (ca.green(), cb.green()), (ca.blue(), cb.blue()))))


def chip_colors(kind: str, t: dict) -> tuple[QColor, QColor, QColor]:
    """(background, text, border) of a chip, from the theme tokens: a security note borrows the amber of the
    'Official site' tag but is outlined, an end-of-life note is danger-tinted, everything else stays quiet."""
    if kind == "security":
        return _mix(t["site_bg"], t["surface"], 0.55), QColor(t["site_fg"]), _mix(t["site_fg"], t["surface"], 0.6)
    if kind == "eol":
        return _mix(t["danger"], t["surface"], 0.14), QColor(t["danger"]), _mix(t["danger"], t["surface"], 0.5)
    if kind == "pinned":
        return QColor(t["surface"]), QColor(t["text"]), QColor(t["muted"])
    return QColor(t["surface"]), QColor(t["muted"]), QColor(t["line"])


class Chip(QWidget):
    """A small status tag painted from the current theme at paint time, so a theme switch needs no rebuild."""

    def __init__(self, kind: str = "", text: str = "", tooltip: str = "", parent=None):
        super().__init__(parent)
        self.set_content(kind, text, tooltip)

    def set_content(self, kind: str, text: str, tooltip: str = ""):
        self.kind, self.text = kind, text
        self.setToolTip(tooltip)
        self.setAccessibleName(text)
        self.update()

    def _font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(8.5)
        font.setWeight(QFont.DemiBold if self.kind in LOUD else QFont.Normal)
        return font

    def height_hint(self) -> int:
        return QFontMetrics(self._font()).height() + 2

    def width_for(self, text: str = "") -> int:
        return min(CHIP_MAX, QFontMetrics(self._font()).horizontalAdvance(text or self.text) + 2 * CHIP_PAD + 2)

    def paintEvent(self, event):
        bg, fg, border = chip_colors(self.kind, current())
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(bg)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        font = self._font()
        p.setFont(font)
        p.setPen(fg)
        room = max(0, self.width() - 2 * CHIP_PAD)
        force = Qt.TextForceRightToLeft if direction_of(self.text) == Qt.RightToLeft else Qt.TextForceLeftToRight
        flags = Qt.AlignCenter.value | force.value     # '+4' must not turn into '4+' in an RTL window
        p.drawText(self.rect(), flags, QFontMetrics(font).elidedText(self.text, Qt.ElideRight, room))


class MetaLine(QWidget):
    """The line under an app's name: source tag, publisher and the chips. Placed by hand so that the chips can
    never push the rest of the row: the publisher keeps at least PUBLISHER_MIN, the chips that do not fit
    collapse into one '+N' chip whose tooltip lists them."""

    def __init__(self, badge: QLabel, publisher: ElidedLabel, parent=None):
        super().__init__(parent)
        self.badge, self.publisher = badge, publisher
        badge.setParent(self)
        publisher.setParent(self)
        self._chips: list[Chip] = []
        self._more = Chip("more", parent=self)
        self._more.hide()
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_chips(self, items: list[tuple[str, str, str]]):
        """items: (kind, text, tooltip), in the order to show them."""
        while len(self._chips) > len(items):
            self._chips.pop().deleteLater()
        for i, (kind, text, tip) in enumerate(items):
            if i < len(self._chips):
                self._chips[i].set_content(kind, text, tip)
            else:
                self._chips.append(Chip(kind, text, tip, self))
        self.updateGeometry()
        self.relayout()

    def chips(self) -> list[Chip]:
        return list(self._chips)

    def sizeHint(self) -> QSize:
        height = max(self.badge.sizeHint().height(), self.publisher.sizeHint().height(), self._more.height_hint())
        return QSize(0, height)

    def minimumSizeHint(self) -> QSize:
        return QSize(PUBLISHER_MIN, self.sizeHint().height())

    def event(self, e):
        if e.type() == QEvent.LayoutRequest:
            self.relayout()
        return super().event(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.relayout()

    def _put(self, widget: QWidget, x: int, width: int, height: int | None = None):
        """Place `widget` `x` px from the leading edge (the right edge in an RTL UI)."""
        height = self.height() if height is None else height
        px = self.width() - x - width if self.layoutDirection() == Qt.RightToLeft else x
        widget.setGeometry(px, (self.height() - height) // 2, max(0, width), height)
        widget.show()

    def _more_width(self, count: int) -> int:
        return self._more.width_for("+" + num(count))

    def _fit(self, widths: list[int], budget: int) -> tuple[list[int], int, int]:
        """(indexes shown, number hidden, width used) for chips squeezed into `budget` px. A chip is shown whole or
        not at all (the '+N' chip, whose room is reserved while chips are left over, stands for those that do not
        fit): a first chip cut to 'Securi…' tells nothing."""
        shown, used = [], 0
        for i, width in enumerate(widths):
            left = len(widths) - i - 1
            reserve = GAP + self._more_width(left) if left else 0
            need = width + (GAP if shown else 0)
            if used + need + reserve > budget:
                break
            shown.append(i)
            used += need
        hidden = len(widths) - len(shown)
        if hidden:
            more = self._more_width(hidden)
            if shown:
                used += GAP + more
            elif more <= budget:
                used = more
            else:
                used = 0
        return shown, hidden, used

    def relayout(self):
        full, height = self.width(), self.height()
        if full <= 0 or height <= 0:
            return
        x = 0
        if not self.badge.isHidden():
            width = self.badge.sizeHint().width()
            self._put(self.badge, 0, width)
            x = width + GAP
        rest = max(0, full - x)
        publisher = self.publisher.sizeHint().width()
        widths = [chip.width_for() for chip in self._chips]
        total = sum(widths) + GAP * max(0, len(widths) - 1)
        if not widths or publisher + GAP + total <= rest:
            shown, hidden, strip = list(range(len(widths))), 0, total
        else:
            budget = rest - min(publisher, PUBLISHER_MIN) - GAP
            shown, hidden, strip = self._fit(widths, budget)
        publisher_w = min(publisher, rest - (strip + GAP if strip else 0))
        self._put(self.publisher, x, publisher_w)
        x += publisher_w + GAP
        for i, chip in enumerate(self._chips):
            if i in shown:
                self._put(chip, x, widths[i], chip.height_hint())
                x += widths[i] + GAP
            else:
                chip.hide()
        if hidden and strip:
            names = [self._chips[i] for i in range(len(self._chips)) if i not in shown]
            kind = next((k for k in ("security", "eol") if any(c.kind == k for c in names)), "more")
            self._more.set_content(kind, "+" + num(hidden),
                                   "\n\n".join(f"{c.text}\n{c.toolTip()}".strip() for c in names))
            self._put(self._more, x, self._more_width(hidden), self._more.height_hint())
        else:
            self._more.hide()


def pin_texts(pin: dict, installed: str) -> tuple[str, str]:
    """(chip text, tooltip) of a pin: 'hold' keeps the installed version, 'major' stays on its major line."""
    line = pins.major_line(pin, installed)
    if pin.get("mode") == "major" and line is not None:
        label = f"{line}.x"
        return (tr("Staying on version {line}", line=label),
                tr("Only updates inside version {line} are offered.", line=label))
    return (tr("Held at {version}", version=pin.get("version") or installed or "?"),
            tr("No update is offered for this app until you remove the pin."))


# Children built on first use (see AppRow): reading one of these from a row that is still a shell builds it.
_CHILDREN = frozenset({
    "icon", "name", "badge", "publisher", "meta_line", "old_ver", "arrow", "new_ver", "dl_size", "actions", "button",
    "note", "phase", "pct", "bar", "cancel_btn", "state", "more", "menu",
})


class AppRow(QFrame):
    """One app. Cheap until it is needed: __init__ only keeps the data, and the widgets are built the first time the
    row is shown, measured or told to show a job state, so a library of 150 rows costs what its visible part costs."""
    update_clicked = Signal(str)
    cancel_clicked = Signal(str)
    menu_action = Signal(str, str)
    selection_changed = Signal(str, bool)     # uid, ticked (only in selectable mode)

    def __init__(self, status: AppStatus, skipped: bool, ignored: bool):
        super().__init__()
        self.setObjectName("row")
        self.setAttribute(Qt.WA_Hover)
        self.uid = status.uid
        self._built = False
        self._page_since = float("-inf")    # when the action area last switched controls
        self._pixmap = None                 # the icon, until the label exists
        self._auto = False                  # state of the Update automatically menu entry
        self._dup = ""                      # tooltip of the Possible duplicate chip ("" = none)
        self._meta: tuple[str, str] | None = None    # size and date texts of the All apps columns
        self._selectable = False
        self._selected = False
        self._check: QCheckBox | None = None
        self._meta_col: QVBoxLayout | None = None
        self.refresh(status, skipped, ignored)

    # -- building --------------------------------------------------------
    def build(self):
        """Create the widgets now (the library does it for the rows it is about to show)."""
        if not self._built:
            self._built = True
            self._build()

    def __getattr__(self, name):
        if name in _CHILDREN and not self.__dict__.get("_built", True):
            self.build()
            return getattr(self, name)
        raise AttributeError(name)

    def sizeHint(self):
        self.build()
        return super().sizeHint()

    def minimumSizeHint(self):
        self.build()
        return super().minimumSizeHint()

    def showEvent(self, event):
        self.build()
        super().showEvent(event)

    def _build(self):
        lay = self._lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(GAP_WIDE)

        self.icon = QLabel()
        self.icon.setFixedSize(36, 36)
        lay.addWidget(self.icon)

        self._text_col = text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self.name = ElidedLabel(self.status.app.name, "appName")
        text_col.addWidget(self.name)
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        self.publisher = ElidedLabel(self.status.app.publisher or tr("Unknown publisher"), "meta")
        self.meta_line = MetaLine(self.badge, self.publisher)
        text_col.addWidget(self.meta_line)
        lay.addLayout(text_col, 1)

        # The versions, with the size of the download muted underneath the new one.
        versions = QVBoxLayout()
        versions.setSpacing(1)
        versions.addStretch(1)
        line = QHBoxLayout()
        line.setSpacing(VERSION_GAP)
        self.old_ver = VersionLabel("oldVer")
        self.arrow = QLabel("←" if ui_is_rtl() else "→")
        self.arrow.setObjectName("arrow")
        self.new_ver = VersionLabel("newVer")
        for w in (self.old_ver, self.arrow, self.new_ver):
            line.addWidget(w)
        versions.addLayout(line)
        self.dl_size = ltr_label("", "meta")
        self.dl_size.hide()
        # under the new version, which ends the row on the trailing side (the right, or the left when mirrored)
        versions.addWidget(self.dl_size, 0, Qt.AlignLeft if ui_is_rtl() else Qt.AlignRight)
        versions.addStretch(1)
        lay.addLayout(versions)

        self.actions = QStackedWidget()
        self.actions.setFixedWidth(ACTIONS_WIDE)
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

        # What the scan has to say about an app that offers no update: one line, elided, the whole text in the tooltip.
        # (A wrapped label would make this row, and so every row sized from it, taller than the others.)
        self.state = ElidedLabel(trailing=True)
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

        if self._pixmap is not None:
            self.icon.setPixmap(self._pixmap)
        self._apply()
        self._apply_meta()
        self._apply_selectable()
        self._fit()

    # -- columns ---------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        """Fit the columns to the row's width: below NARROW_ROW px the size and date columns are hidden and the gaps
        and the action area shrink; then the versions get what is left once the name has NAME_MIN px."""
        if not self._built:
            return
        for widget in (self, self.more, self.arrow, self.old_ver, self.new_ver):
            widget.ensurePolished()     # the stylesheet sets the borders, paddings and fonts that the sizes come from
        narrow = self.width() < NARROW_ROW
        gap = GAP_NARROW if narrow else GAP_WIDE
        self._lay.setSpacing(gap)
        self.actions.setFixedWidth(ACTIONS_NARROW if narrow else ACTIONS_WIDE)
        columns = [self.icon.width(), ACTIONS_NARROW if narrow else ACTIONS_WIDE, self.more.sizeHint().width()]
        if self._meta_col is not None:
            for label in (self._size_label, self._date_label):
                label.setVisible(not narrow)
            if not narrow:
                columns.append(self._size_label.minimumWidth())
        if self._check is not None and not self._check.isHidden():
            self._check.ensurePolished()
            columns.append(self._check.sizeHint().width())
        margins = self._lay.contentsMargins()
        # the name column and the versions are the two that flex: every other column is in `columns`
        room = self.contentsRect().width() - margins.left() - margins.right() - sum(columns) - gap * (len(columns) + 1) - NAME_MIN
        versions = [self.old_ver] + ([] if self.new_ver.isHidden() else [self.new_ver])
        if len(versions) > 1:
            room -= self.arrow.sizeHint().width() + 2 * VERSION_GAP
        for label, width in zip(versions, share_room([v.sizeHint().width() for v in versions], room)):
            label.setMaximumWidth(width)

    # -- state ---------------------------------------------------------
    def refresh(self, status: AppStatus, skipped: bool, ignored: bool):
        self.status, self.skipped, self.ignored = status, skipped, ignored
        self._badges = list(status.badges or [])
        self._pin = status.pin
        if self._built:
            self._apply()
            self._fit()

    def _apply(self):
        status, skipped, ignored = self.status, self.skipped, self.ignored
        app, upd = status.app, status.update
        self.name.set_full_text(app.name)
        self.publisher.set_full_text(app.publisher or tr("Unknown publisher"))

        source = upd.source if upd else ("github" if status.repo else "winget" if status.winget_id else "")
        self.badge.setVisible(bool(source))
        self.badge.setText(tr(SOURCE_LABELS.get(source, "")) if source else "")
        self.badge.setProperty("source", source)
        repolish(self.badge)
        self._render_chips()

        if upd and not skipped:
            self.old_ver.set_full_text(app.version or "?")
            self.new_ver.set_full_text(upd.version)
            self.arrow.show()
            self.new_ver.show()
            size = getattr(upd, "size", 0)
            self.dl_size.setText(num(format_size(size)) if size > 0 else "")
            self.dl_size.setToolTip(tr("Download size") if size > 0 else "")
            self.dl_size.setVisible(size > 0)
            manual = upd.source == "site" and not upd.download_url and not status.fallbacks
            self.button.setText(tr("Open website") if manual else tr("Update"))
            self._show_page(0)
            tip = [tr("Source: {label} — {origin}", label=tr(SOURCE_LABELS[upd.source]), origin=upd.origin)]
            if upd.notes:
                tip.append(isolate_lines(upd.notes[:600]))
            self.setToolTip("\n\n".join(tip))
        else:
            self.old_ver.set_full_text(app.version or "")
            self.arrow.hide()
            self.new_ver.hide()
            self.dl_size.hide()
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
        self.state.set_full_text(" ".join(text.split()))
        self.state.setToolTip(text)
        self.state.setAccessibleName(text)
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
        self._pixmap = pixmap
        if self._built:
            self.icon.setPixmap(pixmap)

    def _cancel_control(self, label: str = "", tip: str = ""):
        """The link beside the progress bar; no label hides it (a recheck, or a stop already under way)."""
        self.cancel_btn.setText(label)
        self.cancel_btn.setToolTip(tip)
        self.cancel_btn.setVisible(bool(label))

    # -- chips: badges, pin and duplicate marker --------------------------------------------------
    def set_badges(self, badges) -> None:
        """Show one chip per badges.Badge (text and tooltip through render(b.text, b.values))."""
        self._badges = list(badges or [])
        if self._built:
            self._render_chips()

    def set_pin(self, pin) -> None:
        """Show a pin chip for status.pin ({"mode": "hold" or "major", "version": ...}) or hide it (None)."""
        self._pin = pin or None
        if self._built:
            self._render_chips()

    def set_duplicate(self, tooltip: str) -> None:
        """Mark the app as a possible duplicate (the tooltip says what else is installed); "" clears the mark."""
        self._dup = tooltip or ""
        if self._built:
            self._render_chips()

    def _render_chips(self):
        items = [(b.kind, render(b.text, b.values), render(b.tooltip, b.values)) for b in self._badges]
        if self._pin:
            items.append(("pinned", *pin_texts(self._pin, self.status.app.version)))
        if self._dup:
            items.append(("duplicate", tr("Possible duplicate"), self._dup))
        items.sort(key=lambda it: CHIP_ORDER.index(it[0]) if it[0] in CHIP_ORDER else len(CHIP_ORDER))
        self.meta_line.set_chips(items)

    def retranslate(self) -> None:
        """Texts that carry no job state: the chips, the size and date columns, the names read aloud."""
        if self._built:
            self._render_chips()
            self._apply_meta()
            self._apply_selectable()
            self.more.setAccessibleName(tr("More actions"))
            self.more.setToolTip(tr("More actions"))

    # -- All apps columns ---------------------------------------------------------------------------
    def set_meta(self, size_text: str, date_text: str) -> None:
        """Extra columns for the All apps tab (already formatted, empty = not shown)."""
        self._meta = (size_text or "", date_text or "")
        if self._built:
            self._apply_meta()
            self._fit()

    def _apply_meta(self):
        if self._meta is None:
            return
        if self._meta_col is None:
            self._size_label, self._date_label = ltr_label("", "meta"), ltr_label("", "meta")
            self._size_label.ensurePolished()      # the stylesheet sets the font size, so measure after it applies
            fm = self._size_label.fontMetrics()
            width = max(fm.horizontalAdvance(t) for t in ("1023.9 MB", "2026-11-02")) + 8
            self._meta_col = QVBoxLayout()
            self._meta_col.setSpacing(3)
            for label in (self._size_label, self._date_label):
                label.setFixedWidth(width)
                self._meta_col.addWidget(label)
            self._meta_col.addStretch(1)
            self._lay.insertLayout(self._lay.indexOf(self._text_col) + 1, self._meta_col)
        side = Qt.AlignLeft if ui_is_rtl() else Qt.AlignRight      # numbers line up on the side nearer the versions
        for label, text, tip in ((self._size_label, self._meta[0], tr("Size on disk")),
                                 (self._date_label, self._meta[1], tr("Installed on"))):
            label.setText(text)
            label.setToolTip(tip if text else "")
            label.setAlignment(side | Qt.AlignVCenter)

    # -- selection (the All apps tab) ------------------------------------------------------------------
    def set_selectable(self, on: bool) -> None:
        """Show a tick box at the leading edge and emit selection_changed when it changes."""
        self._selectable = bool(on)
        if not on:
            self._selected = False
        if self._built:
            self._apply_selectable()
            self._fit()

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        """Set the tick without emitting selection_changed."""
        self._selected = bool(on)
        if self._built:
            self._apply_selectable()
        self.update()

    def _apply_selectable(self):
        if self._selectable and self._check is None:
            self._check = QCheckBox()
            self._check.toggled.connect(self._ticked)
            self._lay.insertWidget(0, self._check)
        if self._check is not None:
            self._check.setAccessibleName(tr("Select {name}", name=self.status.app.name))
            self._check.setVisible(self._selectable)
            was = self._check.blockSignals(True)
            self._check.setChecked(self._selected)
            self._check.blockSignals(was)

    def _ticked(self, on: bool):
        self._selected = on
        self.update()
        self.selection_changed.emit(self.uid, on)

    def focus_widgets(self) -> list[QWidget]:
        """The row's controls in Tab order (the library chains them in the order the rows are shown)."""
        if not self._built:
            return []
        found = [self._check] if self._check is not None and self._selectable else []
        return found + [self.button, self.cancel_btn, self.more]

    def mouseReleaseEvent(self, event):
        if self._selectable and self._check is not None and event.button() == Qt.LeftButton \
                and self.rect().contains(event.position().toPoint()):
            self._check.toggle()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._selected and self._selectable:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(current()["accent"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)

    # -- job states ------------------------------------------------------------------------------------
    def set_queued(self):
        self.build()
        self.note.hide()
        self.phase.set_full_text(tr("Queued"))
        self.pct.clear()
        self.bar.setRange(0, 0)
        self._cancel_control(tr("Cancel"))
        self._show_page(1)

    def set_progress(self, text: str, pct: int, cancel: str = ""):
        """`cancel` is "cancel" (the work stops), "wait" (the installer keeps running; only the waiting
        stops) or "" (nothing to cancel)."""
        self.build()
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
        self.build()
        self.note.hide()
        self.phase.set_full_text(tr("Cancelling…"))
        self.pct.clear()
        self.bar.setRange(0, 0)
        self._cancel_control()
        self._show_page(1)

    def set_cancelled(self, message: str):
        """Not a failure: the user stopped it, so the row goes back to offering the update."""
        self.build()
        self.note.set_full_text(tr("Cancelled"))
        self.note.setToolTip(message)
        self.note.show()
        self.button.setText(tr("Update"))
        self._show_page(0)
        self.setToolTip(message)
        self.state.setToolTip(message)

    def set_result(self, ok: bool, message: str, version: str):
        self.build()
        self.note.hide()
        if ok:
            self.old_ver.set_full_text(version)
            self.arrow.hide()
            self.new_ver.hide()
            self.dl_size.hide()
            self._state(message, "accent")
            self._fit()
        else:
            self.note.set_full_text(" ".join(message.split()))
            self.note.setToolTip(message)
            self.note.show()
            self.button.setText(tr("Retry"))
            self._show_page(0)
            self.setToolTip(message)
        self.state.setToolTip(message)

    def set_auto_update(self, on: bool) -> None:
        """State of the checkable Update automatically menu entry (the window owns settings auto_update)."""
        self._auto = bool(on)

    # -- menu ----------------------------------------------------------
    def _build_menu(self):
        m, st, upd = self.menu, self.status, self.status.update
        m.clear()

        def add(text, key):
            m.addAction(text, lambda: self.menu_action.emit(self.uid, key))

        add(tr("Details…"), "details")
        m.addSeparator()
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
        add(tr("Remove pin") if self._pin else tr("Pin version…"), "unpin" if self._pin else "pin")
        if st.winget_id:
            add(tr("Roll back…"), "rollback")
        auto = m.addAction(tr("Update automatically"))
        auto.setCheckable(True)
        auto.setChecked(self._auto)
        auto.triggered.connect(self._auto_toggled)
        if st.app.install_location and os.path.isdir(st.app.install_location):
            m.addSeparator()
            add(tr("Open install folder"), "folder")
        if m.actions() and not m.actions()[-1].isSeparator():
            m.addSeparator()
        add(tr("Uninstall…"), "uninstall")

    def _auto_toggled(self, on: bool):
        self._auto = on
        self.menu_action.emit(self.uid, "auto")


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
