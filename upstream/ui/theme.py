"""Themes, stylesheet and drawn icons.

Each theme is a set of colour tokens; apply_theme() turns them into both a
QPalette (for native parts such as message boxes and combo popups) and the
stylesheet. "system" follows the Windows light/dark setting."""
from __future__ import annotations

import hashlib

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap

from ..config import DATA_DIR

BRAND = "#1E6B57"

THEMES: dict[str, dict] = {
    "paper": dict(
        label="Paper", dark=False,
        bg="#F3F1EC", surface="#FFFFFF", raised="#EAE7E0", text="#1B1A17", muted="#6F6B63", line="#E0DCD3",
        accent="#1E6B57", accent_hover="#175746", on_accent="#FFFFFF", accent_soft="#DDEDE6",
        danger="#A8412C", gh_bg="#24292F", gh_fg="#FFFFFF", wg_bg="#E2ECF6", wg_fg="#1D4C80",
        site_bg="#F3E5C6", site_fg="#6F4C0E",
    ),
    "graphite": dict(
        label="Graphite", dark=True,
        bg="#131312", surface="#1C1C1A", raised="#272724", text="#ECE9E2", muted="#9C978D", line="#2F2E2B",
        accent="#4BB393", accent_hover="#62C6A6", on_accent="#0D1A15", accent_soft="#1C3A30",
        danger="#E2806A", gh_bg="#E8E8E6", gh_fg="#1B1B19", wg_bg="#1E3550", wg_fg="#AACBF0",
        site_bg="#45361B", site_fg="#F0CF8E",
    ),
    "midnight": dict(
        label="Midnight", dark=True,
        bg="#0D1220", surface="#141A2A", raised="#1D2537", text="#E5E8EF", muted="#8B93A7", line="#242D41",
        accent="#E3A44A", accent_hover="#F0B865", on_accent="#1A1206", accent_soft="#3A2D18",
        danger="#F07A6A", gh_bg="#E5E8EF", gh_fg="#0D1220", wg_bg="#1B3050", wg_fg="#A8C8F0",
        site_bg="#3B2F17", site_fg="#F0D08E",
    ),
    "arctic": dict(
        label="Arctic", dark=False,
        bg="#EDF1F5", surface="#FFFFFF", raised="#E1E7EE", text="#15202B", muted="#5C6978", line="#D4DCE5",
        accent="#2461A6", accent_hover="#1C4E86", on_accent="#FFFFFF", accent_soft="#DBE7F4",
        danger="#B0392D", gh_bg="#1F2933", gh_fg="#FFFFFF", wg_bg="#DCF0EC", wg_fg="#1D5E55",
        site_bg="#F4E8CB", site_fg="#6A4C10",
    ),
    "dune": dict(
        label="Dune", dark=False,
        bg="#F1E9DD", surface="#FBF7F1", raised="#E7DCCB", text="#2A2119", muted="#716252", line="#DFD2BF",
        accent="#9C4626", accent_hover="#7F3A1D", on_accent="#FFFFFF", accent_soft="#F0D9CB",
        danger="#982C3A", gh_bg="#2A2119", gh_fg="#FBF7F1", wg_bg="#E1E8EB", wg_fg="#2E5365",
        site_bg="#EADCB8", site_fg="#684A11",
    ),
    "moss": dict(
        label="Moss", dark=True,
        bg="#101411", surface="#171C18", raised="#212822", text="#E3E9DE", muted="#929F8F", line="#29312A",
        accent="#A2C679", accent_hover="#B7D791", on_accent="#141F0F", accent_soft="#283821",
        danger="#E0846A", gh_bg="#E3E9DE", gh_fg="#101411", wg_bg="#1E3245", wg_fg="#A8C8E7",
        site_bg="#3D3418", site_fg="#E9CF89",
    ),
    "contrast": dict(
        label="High contrast", dark=True,
        bg="#000000", surface="#000000", raised="#1F1F1F", text="#FFFFFF", muted="#DADADA", line="#FFFFFF",
        accent="#FFD400", accent_hover="#FFE45C", on_accent="#000000", accent_soft="#332B00",
        danger="#FF7070", gh_bg="#FFFFFF", gh_fg="#000000", wg_bg="#00314A", wg_fg="#9FE0FF",
        site_bg="#332B00", site_fg="#FFD400",
        disabled="#9A9A9A", disabled_dashed=True,    # muted is nearly the text colour here: dim, and dash the border
    ),
}
THEME_ORDER = ["system", "paper", "graphite", "midnight", "arctic", "dune", "moss", "contrast"]

_current: dict = THEMES["paper"]


def system_is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return False


def resolve(key: str) -> dict:
    if key == "system" or key not in THEMES:
        return THEMES["graphite" if system_is_dark() else "paper"]
    return THEMES[key]


def current() -> dict:
    return _current


def palette_for(t: dict) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: t["bg"], QPalette.WindowText: t["text"], QPalette.Base: t["surface"],
        QPalette.AlternateBase: t["raised"], QPalette.Text: t["text"], QPalette.Button: t["surface"],
        QPalette.ButtonText: t["text"], QPalette.Highlight: t["accent"], QPalette.HighlightedText: t["on_accent"],
        QPalette.ToolTipBase: t["surface"], QPalette.ToolTipText: t["text"], QPalette.PlaceholderText: t["muted"],
        QPalette.Link: t["accent"], QPalette.BrightText: t["danger"],
    }
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t["muted"]))
    return p


def apply_theme(app, key: str) -> dict:
    global _current
    _current = resolve(key)
    app.setPalette(palette_for(_current))
    app.setStyleSheet(stylesheet(_current, _glyphs(_current)))
    return _current


def _glyphs(t: dict) -> dict:
    """Check mark and dropdown chevron in the theme's colours, drawn once to PNG
    because stylesheets can only take images by file path."""
    folder = DATA_DIR / "theme"
    shapes = {
        "check": (t["on_accent"], [(7, 16.5), (13, 22.5), (25, 10)]),
        "chevron": (t["muted"], [(9, 12), (16, 19), (23, 12)]),
    }
    paths = {}
    for name, (colour, points) in shapes.items():
        path = folder / f"{name}-{colour.lstrip('#')}.png"
        if not path.exists():
            folder.mkdir(parents=True, exist_ok=True)
            pm = QPixmap(32, 32)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(colour), 3.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            line = QPainterPath()
            line.moveTo(*points[0])
            for x, y in points[1:]:
                line.lineTo(x, y)
            p.drawPath(line)
            p.end()
            pm.save(str(path))
        paths[name] = path.as_posix()
    return paths


def _shade(colour: str, dark: bool) -> str:
    """A hover shade of a token: lighter on dark themes, darker on light ones."""
    c = QColor(colour)
    return (c.lighter(118) if dark else c.darker(118)).name()


def stylesheet(t: dict, glyphs: dict) -> str:
    danger_hover = _shade(t["danger"], t["dark"])
    # muted text (protected rows) keeps 4.5:1 on bg in every theme but not on raised; High contrast has bg == surface
    row_hover = t["bg"] if t["bg"] != t["surface"] else t["raised"]
    off = t.get("disabled", t["muted"])
    # where muted is too close to the text colour, a disabled button is also drawn with a dashed border
    dash = f"border-style: dashed; border-color: {off};" if t.get("disabled_dashed") else ""
    dash_flat = f"border: 1px dashed {off}; padding: 6px 15px;" if t.get("disabled_dashed") else ""
    return f"""
    QWidget {{ color: {t['text']}; font-family: "Segoe UI"; font-size: 10pt; }}
    QMainWindow, #root, QDialog {{ background: {t['bg']}; }}
    #title {{ font-size: 22pt; font-weight: 600; }}
    #subtitle, #footer, #hint {{ color: {t['muted']}; }}
    #section {{ color: {t['muted']}; font-size: 9pt; font-weight: 600; }}

    QPushButton {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 8px; padding: 7px 16px; }}
    QPushButton:hover {{ background: {t['raised']}; }}
    QPushButton:focus {{ border-color: {t['accent']}; }}
    QPushButton:disabled {{ color: {off}; {dash} }}
    QPushButton#primary {{ background: {t['accent']}; color: {t['on_accent']}; border: none; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
    QPushButton#primary:disabled {{ background: {t['raised']}; color: {off}; {dash_flat} }}
    QPushButton#danger {{ background: {t['danger']}; color: {t['on_accent']}; border: none; font-weight: 600; }}
    QPushButton#danger:hover {{ background: {danger_hover}; }}
    QPushButton#danger:disabled {{ background: {t['raised']}; color: {off}; {dash_flat} }}
    QPushButton#rowAction {{ padding: 6px 14px; min-width: 92px; }}
    QPushButton#rowAction[tone="accent"] {{ background: {t['accent_soft']}; color: {t['accent']}; border: 1px solid {t['accent_soft']}; font-weight: 600; }}
    QPushButton#rowAction[tone="accent"]:hover {{ background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']}; }}
    QPushButton#rowAction:focus, QPushButton#rowAction[tone="accent"]:focus {{ border: 2px solid {t['accent']}; padding: 5px 13px; }}
    QPushButton#rowAction[tone="accent"]:focus:hover {{ border-color: {t['on_accent']}; }}
    QPushButton#link {{ background: transparent; border: 2px solid transparent; border-radius: 6px; color: {t['muted']}; padding: 2px 4px; }}
    QPushButton#link:hover {{ color: {t['text']}; }}
    QPushButton#link:focus {{ border-color: {t['accent']}; color: {t['text']}; }}

    QPushButton#seg {{ background: transparent; border: 2px solid transparent; border-radius: 0; padding: 5px 4px; color: {t['muted']}; }}
    QPushButton#seg:hover {{ color: {t['text']}; }}
    QPushButton#seg:checked {{ color: {t['text']}; border-bottom-color: {t['accent']}; font-weight: 600; }}
    QPushButton#seg:focus {{ border-color: {t['accent']}; }}

    QLineEdit, QPlainTextEdit {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 8px; padding: 6px 10px;
                 selection-background-color: {t['accent']}; selection-color: {t['on_accent']};
                 placeholder-text-color: {t['muted']}; }}
    QLineEdit:focus {{ border-color: {t['accent']}; }}
    QLineEdit:read-only {{ font-family: Consolas, "Segoe UI"; font-size: 9pt; }}
    QPlainTextEdit#log {{ font-family: Consolas, "Segoe UI"; font-size: 9pt; color: {t['muted']}; }}

    QComboBox {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 8px; padding: 6px 10px; min-width: 170px; }}
    QComboBox:hover, QComboBox:focus {{ border-color: {t['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox::down-arrow {{ image: url("{glyphs['chevron']}"); width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['line']}; outline: none;
                                   selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; padding: 4px; }}
    QComboBox::item:selected {{ background: {t['accent']}; color: {t['on_accent']}; }}
    QCheckBox {{ spacing: 10px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t['muted']}; border-radius: 4px; background: {t['surface']}; }}
    QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
    QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url("{glyphs['check']}"); }}
    QCheckBox::indicator:focus {{ border: 2px solid {t['accent']}; }}
    QCheckBox::indicator:checked:focus {{ border-color: {t['text']}; }}

    QListWidget {{ background: transparent; border: none; outline: none; }}
    QListWidget::item, QListWidget::item:selected, QListWidget::item:hover {{ background: transparent; border: none; }}
    QListWidget#versions::item {{ padding: 6px 10px; border-radius: 6px; }}
    QListWidget#versions::item:hover {{ background: {t['raised']}; }}
    QListWidget#versions::item:selected, QListWidget#versions::item:selected:hover {{ background: {t['accent']}; color: {t['on_accent']}; }}

    #row {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 10px; }}
    #row:hover {{ border-color: {t['muted']}; }}
    #appName {{ font-size: 10.5pt; font-weight: 600; }}
    #meta {{ color: {t['muted']}; font-size: 9pt; }}
    #oldVer, #arrow {{ color: {t['muted']}; }}
    #newVer {{ color: {t['accent']}; font-weight: 600; }}
    QLabel[tone="muted"] {{ color: {t['muted']}; }}
    QLabel[tone="accent"] {{ color: {t['accent']}; font-weight: 600; }}
    QLabel[tone="danger"] {{ color: {t['danger']}; }}
    #dlgTitle {{ font-size: 14pt; font-weight: 600; }}
    #dlgTitle[tone="accent"] {{ color: {t['accent']}; }}
    #dlgTitle[tone="danger"] {{ color: {t['danger']}; }}
    QFrame#notice {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: 8px; }}
    QFrame#notice[tone="danger"] {{ border-color: {t['danger']}; }}
    #badge {{ border-radius: 4px; padding: 1px 6px; font-size: 8.5pt; }}
    #badge[source="github"] {{ background: {t['gh_bg']}; color: {t['gh_fg']}; }}
    #badge[source="winget"] {{ background: {t['wg_bg']}; color: {t['wg_fg']}; border: 1px solid {t['wg_bg']}; }}
    #badge[source="site"] {{ background: {t['site_bg']}; color: {t['site_fg']}; border: 1px solid {t['site_bg']}; }}

    QProgressBar {{ background: {t['raised']}; border: none; border-radius: 2px; }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 2px; }}

    QToolButton#more {{ border: 2px solid transparent; border-radius: 6px; padding: 0 6px; font-size: 14pt; color: {t['muted']}; }}
    QToolButton#more:hover {{ background: {t['raised']}; color: {t['text']}; }}
    QToolButton#more:focus {{ border-color: {t['accent']}; color: {t['text']}; }}
    QToolButton#more::menu-indicator {{ image: none; width: 0; }}
    QMenu {{ background: {t['surface']}; border: 1px solid {t['line']}; padding: 4px; }}
    QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t['accent']}; color: {t['on_accent']}; }}
    QMenu::separator {{ height: 1px; background: {t['line']}; margin: 4px 6px; }}

    QTreeWidget {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 10px; outline: none; padding: 2px; }}
    QTreeWidget::item {{ padding: 5px 6px; border: none; }}
    QTreeWidget::item:hover {{ background: {row_hover}; }}
    QTreeWidget::item:selected, QTreeWidget::item:selected:hover {{ background: {t['accent']}; color: {t['on_accent']}; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{ background: {t['surface']}; color: {t['muted']}; border: none; border-bottom: 1px solid {t['line']};
                            padding: 6px 8px; font-size: 9pt; font-weight: 600; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 4px; min-height: 36px; }}
    QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

    #emptyTitle {{ font-size: 17pt; font-weight: 600; }}
    #emptyText {{ color: {t['muted']}; }}
    QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['line']}; padding: 6px; }}
    """


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


_AVATAR_TONES = ["#6B7F5E", "#7A6A55", "#5D7480", "#80605E", "#6E6684", "#5F7C72", "#857A4E"]


def letter_avatar(name: str, size: int = 36, ratio: float = 2.0) -> QPixmap:
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    tone = _AVATAR_TONES[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(_AVATAR_TONES)]
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(tone))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), 9, 9)
    p.setPen(QColor("#FFFFFF"))
    font = QFont("Segoe UI", int(size * 0.36))
    font.setBold(True)
    p.setFont(font)
    letter = next((ch for ch in name if ch.isalnum()), "?").upper()
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, letter)
    p.end()
    return pm


def app_icon() -> QIcon:
    """A rounded square with a rising arrow."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(BRAND))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
        pen = QPen(QColor("#FFFFFF"), max(1.5, size * 0.1), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        c, top, bottom, wing = size / 2, size * 0.26, size * 0.74, size * 0.2
        path.moveTo(c, bottom)
        path.lineTo(c, top)
        path.moveTo(c - wing, top + wing)
        path.lineTo(c, top)
        path.lineTo(c + wing, top + wing)
        p.drawPath(path)
        p.end()
        icon.addPixmap(pm)
    return icon
