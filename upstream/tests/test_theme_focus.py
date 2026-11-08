"""Theme rules that decide whether a control can be seen: the keyboard focus ring on the controls of the rows, the
library and the tab bar, the selected row of the Roll back list, and disabled buttons in the High contrast theme.
Each test draws the real widgets under the real stylesheet and compares pixels, in a light, a dark and the
high-contrast theme."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget,
)

from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update
from upstream.ui import theme
from upstream.ui.locale import apply_language
from upstream.ui.widgets import AppRow

app = QApplication.instance() or QApplication([])

THEMES = ("paper", "graphite", "contrast")
RING_MIN = 40       # pixels of a 2 px ring around even the smallest control are many more than this


def settle(times: int = 4):
    for _ in range(times):
        app.processEvents()


def colour_count(image: QImage, colour: QColor) -> int:
    rgb = colour.rgb()
    return sum(1 for y in range(image.height()) for x in range(image.width()) if image.pixel(x, y) & 0xFFFFFF == rgb & 0xFFFFFF)


def nearer_to(image: QImage, colour: QColor, other: QColor) -> int:
    """Pixels closer to `colour` than to `other`: the glyphs of text, whatever the font and its anti-aliasing."""
    def distance(c: QColor, to: QColor) -> int:
        return (c.red() - to.red()) ** 2 + (c.green() - to.green()) ** 2 + (c.blue() - to.blue()) ** 2

    return sum(1 for y in range(image.height()) for x in range(image.width())
               if distance(image.pixelColor(x, y), colour) < distance(image.pixelColor(x, y), other))


def changed(a: QImage, b: QImage) -> int:
    return sum(1 for y in range(a.height()) for x in range(a.width()) if a.pixel(x, y) != b.pixel(x, y))


class ThemeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()      # the theme draws its check mark and chevron PNGs here
        patch = mock.patch.object(theme, "DATA_DIR", Path(self._tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        app.setStyleSheet("")
        theme.apply_theme(app, "paper")
        apply_language(app, "en")

    def use(self, key: str):
        apply_language(app, "en")
        theme.apply_theme(app, key)


class FocusRingTest(ThemeCase):
    def surface(self):
        """A window with a text field (where the focus rests) and one of each control the issue lists."""
        win = QWidget()
        win.setObjectName("root")
        lay = QHBoxLayout(win)
        status = AppStatus(InstalledApp(r"HKLM64\{1}", "Alpha", "1.0", "Acme", "machine", "x64"),
                           update=Update("github", "2.0", "o/r"))
        row = AppRow(status, False, False)
        row.set_selectable(True)
        link = QPushButton("Select all")
        link.setObjectName("link")
        tab = QPushButton("Updates")
        tab.setObjectName("seg")
        tab.setCheckable(True)
        tab_on = QPushButton("All apps")
        tab_on.setObjectName("seg")
        tab_on.setCheckable(True)
        tab_on.setChecked(True)
        self.anchor = QLineEdit()
        for w in (self.anchor, row, link, tab, tab_on):
            lay.addWidget(w)
        win.resize(1300, 110)
        win.show()
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.close)
        win.activateWindow()
        settle()
        row.build()
        settle()
        row._check.setChecked(False)
        return win, {"update button": row.button, "more button": row.more, "tick box": row._check,
                     "link button": link, "tab": tab, "selected tab": tab_on}, row

    @staticmethod
    def look(win: QWidget, w: QWidget) -> QImage:
        top = w.mapTo(win, w.rect().topLeft())
        return win.grab(QRect(top.x() - 5, top.y() - 5, w.width() + 10, w.height() + 10)).toImage()

    def focus(self, w: QWidget):
        w.setFocus(Qt.TabFocusReason)
        settle()
        self.assertTrue(w.hasFocus(), "the window must be active for :focus to apply")

    def test_every_listed_control_looks_different_when_it_has_the_keyboard_focus(self):
        for key in THEMES:
            self.use(key)
            win, controls, _ = self.surface()
            accent = QColor(theme.THEMES[key]["accent"])
            for name, w in controls.items():
                with self.subTest(theme=key, control=name):
                    self.anchor.setFocus()
                    settle()
                    before, size = self.look(win, w), w.size()
                    self.focus(w)
                    after = self.look(win, w)
                    self.assertGreater(changed(before, after), RING_MIN)
                    self.assertGreater(colour_count(after, accent) - colour_count(before, accent), RING_MIN,
                                       "the ring is in the accent colour")
                    self.assertEqual(w.size(), size, "the ring must not move the layout")

    def test_a_ticked_box_gets_a_ring_that_does_not_vanish_into_its_accent_fill(self):
        for key in THEMES:
            self.use(key)
            win, controls, row = self.surface()
            box = controls["tick box"]
            box.setChecked(True)
            settle()
            self.anchor.setFocus()
            settle()
            before = self.look(win, box)
            self.focus(box)
            after = self.look(win, box)
            ring = QColor(theme.THEMES[key]["text"])
            with self.subTest(theme=key):
                self.assertGreater(colour_count(after, ring) - colour_count(before, ring), RING_MIN // 2)

    def test_the_update_button_keeps_its_ring_under_the_mouse(self):
        """Hover turns the fill into the accent colour: a ring in that colour would be gone, so it switches to on-accent."""
        for key in THEMES:
            self.use(key)
            win, controls, row = self.surface()
            button, t = controls["update button"], theme.THEMES[key]
            self.focus(button)
            QTest.mouseMove(win, QPoint(2, 2))      # a move to where the cursor already is would not be delivered
            QTest.mouseMove(button, button.rect().center())
            settle()
            image = self.look(win, button)
            top = image.pixelColor(image.width() // 2, 5)       # the button's own top edge (the crop adds 5 px)
            with self.subTest(theme=key):
                self.assertEqual(button.property("tone"), "accent")
                self.assertEqual(top.name().upper(), t["on_accent"].upper())
            win.hide()      # the next window opens in the same place: this one must not take its mouse events

    def test_the_tab_ring_keeps_its_underline_when_the_tab_is_selected(self):
        self.use("paper")
        win, controls, _ = self.surface()
        selected = controls["selected tab"]
        self.focus(selected)
        image = self.look(win, selected)
        accent = QColor(theme.THEMES["paper"]["accent"])
        bottom = image.height() - 5 - 1     # the last row of the button itself
        self.assertTrue(all(image.pixelColor(x, bottom).rgb() == accent.rgb() for x in range(8, image.width() - 8)))

    def test_the_persian_window_gets_the_same_rings(self):
        for key in THEMES:
            self.use(key)
            apply_language(app, "fa")
            win, controls, _ = self.surface()
            for name in ("update button", "tick box", "tab"):
                w = controls[name]
                with self.subTest(theme=key, control=name):
                    self.anchor.setFocus()
                    settle()
                    before = self.look(win, w)
                    self.focus(w)
                    self.assertGreater(changed(before, self.look(win, w)), RING_MIN)


class RollbackListTest(ThemeCase):
    """The Roll back dialog names its list "versions"; its selected row has to show."""

    def make_list(self):
        box = QWidget()
        box.setObjectName("root")
        lay = QVBoxLayout(box)
        lst = QListWidget()
        lst.setObjectName("versions")
        lst.addItems(["1.93.1", "1.93.0", "1.92.2"])
        lay.addWidget(lst)
        box.resize(320, 240)
        box.show()
        self.addCleanup(box.deleteLater)
        self.addCleanup(box.close)
        settle()
        return box, lst

    def test_the_selected_version_is_drawn_in_the_accent_colour_in_every_theme(self):
        for key, t in theme.THEMES.items():
            with self.subTest(theme=key):
                self.use(key)
                box, lst = self.make_list()
                lst.setCurrentRow(1)
                settle()
                rect = lst.visualItemRect(lst.item(1))
                other = lst.visualItemRect(lst.item(2))
                image = lst.viewport().grab().toImage()
                x = rect.right() - 6      # right of the text, inside the row
                self.assertEqual(image.pixelColor(x, rect.center().y()).name().upper(), t["accent"].upper())
                self.assertNotEqual(image.pixelColor(x, other.center().y()).name().upper(), t["accent"].upper())

    def test_the_text_of_the_selected_version_can_be_read(self):
        for key, t in theme.THEMES.items():
            with self.subTest(theme=key):
                self.use(key)
                box, lst = self.make_list()
                lst.setCurrentRow(0)
                settle()
                rect = lst.visualItemRect(lst.item(0))
                image = lst.viewport().grab(rect).toImage()
                accent, on_accent = QColor(t["accent"]), QColor(t["on_accent"])
                self.assertGreater(nearer_to(image, on_accent, accent), 8, "the text is in the on-accent colour")
                self.assertGreater(colour_count(image, accent), nearer_to(image, on_accent, accent), "on the accent fill")

    def test_the_main_lists_keep_their_transparent_rows(self):
        self.use("paper")
        box, lst = self.make_list()
        lst.setObjectName("")           # not the versions list: rows are widgets there, and stay unpainted
        lst.style().unpolish(lst)
        lst.style().polish(lst)
        lst.setCurrentRow(1)
        settle()
        rect = lst.visualItemRect(lst.item(1))
        image = lst.viewport().grab().toImage()
        self.assertNotEqual(image.pixelColor(rect.right() - 6, rect.center().y()).name().upper(),
                            theme.THEMES["paper"]["accent"].upper())


class DisabledButtonTest(ThemeCase):
    def buttons(self):
        box = QWidget()
        box.setObjectName("root")
        lay = QHBoxLayout(box)
        made = {}
        for name, object_name in (("plain", ""), ("primary", "primary"), ("danger", "danger")):
            on, off = QPushButton("Repair"), QPushButton("Repair")
            for b, enabled in ((on, True), (off, False)):
                if object_name:
                    b.setObjectName(object_name)
                b.setEnabled(enabled)
                lay.addWidget(b)
            made[name] = (on, off)
        box.show()
        self.addCleanup(box.deleteLater)
        self.addCleanup(box.close)
        settle()
        return box, made

    @staticmethod
    def edge_runs(button: QPushButton) -> int:
        """How many times the colour changes along the straight part of the button's top edge: 0 for a solid line."""
        image = button.grab().toImage()
        row = [image.pixel(x, 0) for x in range(12, image.width() - 12)]
        row2 = [image.pixel(x, 1) for x in range(12, image.width() - 12)]
        return max(sum(1 for a, b in zip(r, r[1:]) if a != b) for r in (row, row2))

    @staticmethod
    def brightest(button: QPushButton) -> int:
        image = button.grab().toImage()
        inner = range(12, image.width() - 12), range(8, image.height() - 8)
        return max(image.pixelColor(x, y).value() for x in inner[0] for y in inner[1])

    def test_a_disabled_button_is_dim_and_dashed_in_high_contrast(self):
        self.use("contrast")
        box, made = self.buttons()
        for name, (on, off) in made.items():
            with self.subTest(button=name):
                self.assertGreater(self.edge_runs(off), 4, "a dashed border")
                self.assertLess(self.brightest(off), 200, "text and border are dimmer than the enabled button's 255")
        self.assertEqual(self.edge_runs(made["plain"][0]), 0, "an enabled button keeps its solid border")
        self.assertEqual(self.brightest(made["plain"][0]), 255)

    def test_a_disabled_primary_button_does_not_change_size_in_high_contrast(self):
        self.use("contrast")
        box, made = self.buttons()
        for name in ("primary", "danger"):
            on, off = made[name]
            with self.subTest(button=name):
                self.assertEqual(on.sizeHint(), off.sizeHint())

    def test_the_other_themes_keep_their_solid_disabled_look(self):
        for key in ("paper", "graphite", "midnight", "arctic", "dune", "moss"):
            with self.subTest(theme=key):
                self.use(key)
                box, made = self.buttons()
                self.assertEqual(self.edge_runs(made["plain"][1]), 0)
                self.assertEqual(theme.THEMES[key].get("disabled", theme.THEMES[key]["muted"]), theme.THEMES[key]["muted"])


if __name__ == "__main__":
    unittest.main()
