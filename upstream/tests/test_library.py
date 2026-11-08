"""Rows and the All apps library: chips, the size and date columns, the download size, selection, sorting, filters,
duplicates, the language switch and the speed of a 150-app list."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import itertools
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from upstream import i18n
from upstream.badges import Badge
from upstream.details import format_date, format_size
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update
from upstream.ui import theme
from upstream.ui.library import SORTS, FILTERS, LibraryPage, find_duplicates
from upstream.ui.locale import apply_language
from upstream.ui.widgets import (
    ACTIONS_NARROW, ACTIONS_WIDE, GAP, NAME_MIN, NARROW_ROW, PUBLISHER_MIN, VERSION_MAX, VERSION_MIN, AppRow,
    VersionLabel, chip_colors, share_room,
)

_ids = itertools.count(1)


def make(name="App", version="1.0", publisher="Acme", scope="machine", arch="x64", size_kb=0, date="",
         **kw) -> AppStatus:
    app = InstalledApp(rf"HKLM64\{{{next(_ids):08d}}}", name, version, publisher, scope, arch, size_kb=size_kb,
                       install_date=date)
    return AppStatus(app, **kw)


def update(version="2.0", **kw) -> Update:
    return Update("github", version, "o/r", **kw)


SECURITY = Badge("security", "Security fix", "The release notes of this update mention {ids}.",
                 {"ids": "CVE-2026-1234"})
EOL = Badge("eol", "End of life", "Support for {product} {cycle} has ended. It no longer receives security fixes.",
            {"product": "Python", "cycle": "3.8"})
OUTDATED = Badge("outdated", "Outdated", "The installed version ({installed}) is at least two major versions "
                 "behind {latest}.", {"installed": "1.0", "latest": "4.2"})


class QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._dir = tempfile.TemporaryDirectory()      # the theme draws its check mark and chevron PNGs here
        cls.addClassCleanup(cls._dir.cleanup)
        patch = mock.patch.object(theme, "DATA_DIR", Path(cls._dir.name))
        patch.start()
        cls.addClassCleanup(patch.stop)
        theme.apply_theme(cls.app, "paper")

    def setUp(self):
        self.language = "en"
        self.addCleanup(self._restore)

    def _restore(self):
        """Leftover widgets would be re-styled by every later language or theme switch: delete them."""
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if i18n.language() != "en":
            apply_language(self.app, "en")

    def show(self, widget, width=1000, height=600):
        widget.resize(width, height)
        widget.show()
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        self.app.processEvents()
        return widget

    @staticmethod
    def pos_in(widget, parent) -> QPoint:
        return widget.mapTo(parent, QPoint(0, 0))


def action_keys(row: AppRow) -> list[str]:
    """The keys the row's ⋯ menu emits, in menu order (every entry is triggered once)."""
    row.build()
    row._build_menu()
    seen: list[str] = []
    row.menu_action.connect(lambda uid, key: seen.append(key))
    for action in row.menu.actions():
        if not action.isSeparator():
            action.trigger()
    return seen


class RowShellTest(QtCase):
    def test_a_row_builds_when_something_needs_it(self):
        row = AppRow(make("Alpha", update=update()), False, False)
        row.set_icon(QPixmap(36, 36))
        row.set_badges([SECURITY])
        row.set_pin({"mode": "hold", "version": "1.0"})
        row.set_meta("1.5 GB", "2024-03-04")
        row.set_selectable(True)
        row.set_selected(True)
        row.set_auto_update(True)
        row.set_duplicate("x")
        self.assertFalse(row._built, "setters only keep what they are given")
        self.assertEqual(row.uid, row.status.uid)
        self.assertTrue(row.selected)
        self.assertEqual(row.button.text(), "Update")      # reading a child builds the row
        self.assertTrue(row._built)
        self.assertEqual(len(row.meta_line.chips()), 3)
        self.assertEqual(row._size_label.text(), "1.5 GB")
        self.assertTrue(row._check.isChecked())

    def test_size_hint_and_job_states_build_the_row(self):
        self.assertGreater(AppRow(make(), False, False).sizeHint().height(), 40)
        row = AppRow(make(update=update()), False, False)
        row.set_queued()
        self.assertTrue(row._built)
        self.assertEqual(row.phase.full_text, "Queued")

    def test_unknown_attributes_still_raise(self):
        with self.assertRaises(AttributeError):
            AppRow(make(), False, False).nothing_like_this


class ChipTest(QtCase):
    def chips(self, row):
        return [(c.kind, c.text) for c in row.meta_line.chips()]

    def test_badges_pin_and_duplicate_become_chips_in_priority_order(self):
        s = make(update=update(), badges=[OUTDATED, SECURITY], pin={"mode": "hold", "version": "1.0"})
        row = AppRow(s, False, False)
        row.set_duplicate("Also installed: 0.9")
        row.build()
        self.assertEqual(self.chips(row), [("security", "Security fix"), ("outdated", "Outdated"),
                                           ("pinned", "Held at 1.0"), ("duplicate", "Possible duplicate")])
        security = row.meta_line.chips()[0]
        self.assertIn("CVE-2026-1234", security.toolTip())
        self.assertNotIn("{", security.toolTip())

    def test_a_major_pin_names_its_line_and_unpinning_removes_the_chip(self):
        s = make("Pyish", version="3.12.4", pin={"mode": "major", "version": "3.12.4"})
        row = AppRow(s, False, False)
        row.build()
        self.assertEqual(self.chips(row), [("pinned", "Staying on version 3.x")])
        self.assertIn("3.x", row.meta_line.chips()[0].toolTip())
        row.set_pin(None)
        self.assertEqual(self.chips(row), [])

    def test_texts_follow_the_language(self):
        apply_language(self.app, "fa")
        row = AppRow(make(badges=[EOL], pin={"mode": "hold", "version": "1.0"}), False, False)
        row.build()
        texts = [c.text for c in row.meta_line.chips()]
        self.assertEqual(texts[0], i18n.FA["End of life"])
        self.assertIn(i18n.FA["Held at {version}"].split("{")[0].strip(), texts[1])
        self.assertIn("Python", row.meta_line.chips()[0].toolTip())

    def test_every_theme_keeps_chip_text_readable(self):
        for key, t in theme.THEMES.items():
            for kind in ("security", "eol", "pinned", "outdated", "duplicate", "more"):
                bg, fg, _ = chip_colors(kind, t)
                lum = [self._luminance(c) for c in (bg, fg)]
                ratio = (max(lum) + 0.05) / (min(lum) + 0.05)
                self.assertGreaterEqual(ratio, 4.5, f"{key}/{kind}: {ratio:.2f}")

    @staticmethod
    def _luminance(c: QColor) -> float:
        def lin(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * lin(c.red()) + 0.7152 * lin(c.green()) + 0.0722 * lin(c.blue())

    def test_chips_follow_a_theme_switch_without_a_rebuild(self):
        self.addCleanup(theme.apply_theme, self.app, "paper")
        row = AppRow(make(badges=[SECURITY, OUTDATED], update=update()), False, False)
        self.show(row, 1000, 70)
        for key in ("paper", "graphite", "contrast"):
            theme.apply_theme(self.app, key)
            self.app.processEvents()
            for chip in row.meta_line.chips():
                want = chip_colors(chip.kind, theme.THEMES[key])[0]
                got = chip.grab().toImage().pixelColor(3, chip.height() // 2)
                self.assertLessEqual(max(abs(got.red() - want.red()), abs(got.green() - want.green()),
                                         abs(got.blue() - want.blue())), 2, (key, chip.kind))

    def test_chips_never_move_the_version_or_the_button(self):
        for width in (820, 1000):
            plain = self.show(AppRow(make("Photoshop Elements", update=update()), False, False), width, 70)
            crowded_status = make("Photoshop Elements", update=update(),
                                  badges=[SECURITY, EOL, OUTDATED], pin={"mode": "major", "version": "1.0"})
            crowded = AppRow(crowded_status, False, False)
            crowded.set_duplicate("Also installed: 0.9 (All users)")
            self.show(crowded, width, 70)
            for name in ("old_ver", "new_ver", "button", "more", "actions"):
                a, b = getattr(plain, name), getattr(crowded, name)
                self.assertEqual((self.pos_in(a, plain), a.size()), (self.pos_in(b, crowded), b.size()), (width, name))

    def test_chips_that_do_not_fit_collapse_into_a_counter(self):
        s = make("Photoshop Elements", publisher="Acme Software Development Corporation", update=update(),
                 badges=[SECURITY, EOL, OUTDATED], pin={"mode": "major", "version": "1.0"})
        row = AppRow(s, False, False)
        row.set_duplicate("Also installed: 0.9 (All users)")
        row.build()
        line = row.meta_line
        chips = line.chips()
        # a line with room for the tag, the minimum publisher, the first chip and the counter, and nothing more:
        # sized from the real metrics, so it holds with any font
        room = (line.badge.sizeHint().width() + GAP + PUBLISHER_MIN + GAP + chips[0].width_for() + GAP
                + line._more_width(len(chips) - 1) + 2)
        line.resize(room, line.sizeHint().height())
        line.relayout()
        shown = [c for c in chips if not c.isHidden()]
        hidden = [c for c in chips if c.isHidden()]
        self.assertEqual([c.kind for c in shown], ["security"])
        self.assertEqual(len(hidden), 4)
        self.assertGreaterEqual(line.publisher.width(), PUBLISHER_MIN)
        self.assertFalse(line._more.isHidden())
        self.assertEqual(line._more.text, "+4")
        self.assertEqual(line._more.kind, "eol", "a hidden warning keeps its colour on the counter")
        for widget in shown + [line._more, line.publisher, line.badge]:
            self.assertGreaterEqual(widget.x(), 0)
            self.assertLessEqual(widget.x() + widget.width(), line.width(), widget)
        for chip in hidden:
            self.assertIn(chip.text, line._more.toolTip())

    def test_a_chip_is_shown_whole_or_stands_behind_the_counter_never_cut_short(self):
        s = make("Photoshop Elements", publisher="Acme Software Development Corporation", update=update(),
                 badges=[SECURITY], repo="o/r")
        row = AppRow(s, False, False)
        row.build()
        line = row.meta_line
        chip = line.chips()[0]
        whole = chip.width_for()
        base = line.badge.sizeHint().width() + GAP + PUBLISHER_MIN + GAP
        outcomes = set()
        for room in range(base, base + whole + 60):       # every width from "no room" to "room to spare"
            line.resize(room, line.sizeHint().height())
            line.relayout()
            if chip.isHidden():
                outcomes.add("hidden")
                self.assertTrue(line._more.isHidden() or line._more.text == "+1", room)
            else:
                outcomes.add("whole")
                self.assertEqual(chip.width(), whole, f"a chip that is shown is shown in full ({room} px)")
        self.assertEqual(outcomes, {"hidden", "whole"})
        line.resize(base + whole - 10, line.sizeHint().height())      # too narrow for the chip, wide enough for '+1'
        line.relayout()
        self.assertTrue(chip.isHidden())
        self.assertFalse(line._more.isHidden())
        self.assertEqual((line._more.text, line._more.kind), ("+1", "security"))
        self.assertIn(chip.text, line._more.toolTip())

    def test_a_line_too_narrow_for_even_the_counter_shows_the_publisher_alone(self):
        s = make("Tiny", publisher="Acme Software Development Corporation", badges=[SECURITY, EOL], repo="o/r")
        row = AppRow(s, False, False)       # a repo means the source tag is shown
        row.build()
        line = row.meta_line
        line.resize(line.badge.sizeHint().width() + GAP + PUBLISHER_MIN + 4, line.sizeHint().height())
        line.relayout()
        self.assertTrue(all(c.isHidden() for c in line.chips()))
        self.assertTrue(line._more.isHidden())
        self.assertGreaterEqual(line.publisher.width(), PUBLISHER_MIN)

    def test_everything_fits_when_there_is_room(self):
        row = AppRow(make(badges=[SECURITY], update=update()), False, False)
        self.show(row, 1100, 70)
        self.assertTrue(row.meta_line._more.isHidden())
        self.assertFalse(row.meta_line.chips()[0].isHidden())

    def test_rtl_puts_the_chips_after_the_publisher_on_the_left(self):
        apply_language(self.app, "fa")
        row = AppRow(make(badges=[SECURITY], update=update()), False, False)
        self.show(row, 1000, 70)
        line = row.meta_line
        self.assertFalse(line.chips()[0].isHidden())
        self.assertLess(line.chips()[0].x(), line.publisher.x())
        self.assertGreater(line.publisher.x(), 0)
        # the icon is at the right edge and the update button at the left
        self.assertGreater(self.pos_in(row.icon, row).x(), self.pos_in(row.button, row).x())


class DownloadSizeTest(QtCase):
    def test_the_size_sits_under_the_new_version(self):
        row = AppRow(make(update=update(size=52_428_800)), False, False)
        self.show(row, 1000, 70)
        self.assertEqual(row.dl_size.text(), "50.0 MB")
        self.assertTrue(row.dl_size.isVisibleTo(row))
        self.assertEqual(row.dl_size.toolTip(), "Download size")
        self.assertGreater(self.pos_in(row.dl_size, row).y(), self.pos_in(row.new_ver, row).y())
        self.assertAlmostEqual(self.pos_in(row.dl_size, row).x() + row.dl_size.width(),
                               self.pos_in(row.new_ver, row).x() + row.new_ver.width(), delta=2)

    def test_unknown_size_shows_nothing(self):
        for upd in (update(size=0), update()):
            row = AppRow(make(update=upd), False, False)
            self.show(row, 1000, 70)
            self.assertTrue(row.dl_size.isHidden())
        row = AppRow(make(), False, False)       # no update at all
        row.build()
        self.assertTrue(row.dl_size.isHidden())

    def test_persian_digits_and_tooltip(self):
        apply_language(self.app, "fa")
        row = AppRow(make(update=update(size=52_428_800)), False, False)
        self.show(row, 1000, 70)
        self.assertEqual(row.dl_size.text(), i18n.num("50.0 MB"))
        self.assertNotIn("5", row.dl_size.text())
        self.assertEqual(row.dl_size.toolTip(), i18n.FA["Download size"])
        # the new version ends the row on the left here, and the size sits under it
        self.assertAlmostEqual(self.pos_in(row.dl_size, row).x(), self.pos_in(row.new_ver, row).x(), delta=2)

    def test_a_finished_update_drops_the_size(self):
        row = AppRow(make(update=update(size=1_048_576)), False, False)
        self.show(row, 1000, 70)
        row.set_result(True, "Updated", "2.0")
        self.assertTrue(row.dl_size.isHidden())

    def test_skipped_versions_show_no_size(self):
        row = AppRow(make(update=update(size=1_048_576)), True, False)
        row.build()
        self.assertTrue(row.dl_size.isHidden())

    def test_the_size_never_pushes_the_button(self):
        with_size = self.show(AppRow(make("Same", update=update(size=900 * 1024 * 1024)), False, False), 820, 70)
        without = self.show(AppRow(make("Same", update=update()), False, False), 820, 70)
        self.assertEqual(self.pos_in(with_size.button, with_size), self.pos_in(without.button, without))
        self.assertEqual(with_size.height(), without.height())


class RowMenuTest(QtCase):
    def test_new_entries_come_with_the_keys_the_window_handles(self):
        self.assertEqual(action_keys(AppRow(make(), False, False)),
                         ["details", "source", "ignore", "pin", "auto", "uninstall"])

    def test_pin_turns_into_unpin_and_rollback_needs_a_winget_id(self):
        s = make(winget_id="Acme.App", pin={"mode": "hold", "version": "1.0"})
        keys = action_keys(AppRow(s, False, False))
        self.assertIn("unpin", keys)
        self.assertNotIn("pin", keys)
        self.assertIn("rollback", keys)
        self.assertNotIn("rollback", action_keys(AppRow(make(), False, False)))

    def test_the_existing_entries_keep_their_keys_and_order(self):
        with tempfile.TemporaryDirectory() as folder:
            s = make(update=update(page_url="https://example.com/r"), repo="o/r")
            s.app.install_location = folder
            self.assertEqual(action_keys(AppRow(s, False, False)),
                             ["details", "notes", "repo", "source", "skip", "ignore", "pin", "auto", "folder",
                              "uninstall"])

    def test_update_automatically_is_a_check_that_follows_set_auto_update(self):
        row = AppRow(make(), False, False)
        row.build()
        row._build_menu()
        auto = next(a for a in row.menu.actions() if a.text() == "Update automatically")
        self.assertTrue(auto.isCheckable())
        self.assertFalse(auto.isChecked())
        row.set_auto_update(True)
        row._build_menu()
        auto = next(a for a in row.menu.actions() if a.text() == "Update automatically")
        self.assertTrue(auto.isChecked())
        seen = []
        row.menu_action.connect(lambda uid, key: seen.append((uid, key)))
        auto.trigger()
        self.assertEqual(seen, [(row.uid, "auto")])

    def test_menu_texts_are_translated(self):
        apply_language(self.app, "fa")
        row = AppRow(make(winget_id="A.B"), False, False)
        row.build()
        row._build_menu()
        texts = [a.text() for a in row.menu.actions() if not a.isSeparator()]
        for key in ("Details…", "Pin version…", "Roll back…", "Update automatically"):
            self.assertIn(i18n.FA[key], texts)


class RowColumnsAndSelectionTest(QtCase):
    def test_size_and_date_columns(self):
        row = AppRow(make(), False, False)
        row.set_meta("1.5 GB", "2024-03-04")
        self.show(row, 1000, 70)
        self.assertEqual((row._size_label.text(), row._date_label.text()), ("1.5 GB", "2024-03-04"))
        self.assertEqual(row._size_label.toolTip(), "Size on disk")
        self.assertEqual(row._date_label.toolTip(), "Installed on")
        self.assertGreaterEqual(row._date_label.width(), row._date_label.fontMetrics().horizontalAdvance("2024-03-04"))
        row.set_meta("", "")
        self.assertEqual((row._size_label.text(), row._size_label.toolTip()), ("", ""))
        self.assertFalse(row._size_label.isHidden(), "the column keeps its place so the rows line up")

    def test_a_row_without_set_meta_has_no_columns(self):
        row = AppRow(make(), False, False)
        row.build()
        self.assertIsNone(row._meta_col)

    def test_the_tick_box_emits_only_for_the_user(self):
        row = AppRow(make(), False, False)
        self.show(row, 1000, 70)
        got = []
        row.selection_changed.connect(lambda uid, on: got.append((uid, on)))
        self.assertIsNone(row._check)
        row.set_selectable(True)
        self.assertTrue(row._check.isVisibleTo(row))
        row._check.click()
        self.assertEqual((got, row.selected), ([(row.uid, True)], True))
        row.set_selected(False)
        self.assertEqual((got, row._check.isChecked()), ([(row.uid, True)], False))     # silent
        QTest.mouseClick(row, Qt.LeftButton, Qt.NoModifier, QPoint(300, 8))               # the whole row toggles
        self.assertEqual(got[-1], (row.uid, True))
        row.set_selectable(False)
        self.assertFalse(row._check.isVisibleTo(row))
        self.assertFalse(row.selected)

    def test_columns_and_tick_box_in_a_persian_window(self):
        apply_language(self.app, "fa")
        row = AppRow(make(), False, False)
        row.set_meta(i18n.num("1.5 GB"), i18n.num("2024-03-04"))
        row.set_selectable(True)
        self.show(row, 1000, 70)
        self.assertEqual(row._date_label.text(), i18n.num("2024-03-04"))
        self.assertGreater(self.pos_in(row._check, row).x(), self.pos_in(row.icon, row).x())     # leading edge = right


def fleet(n: int) -> list[AppStatus]:
    out = []
    for i in range(n):
        out.append(make(f"Application {i:03d}", version=f"{1 + i % 5}.{i % 9}", publisher=f"Publisher {i % 11}",
                        scope="user" if i % 4 == 0 else "machine", size_kb=(i * 997) % 5000 * 100,
                        date=f"20{20 + i % 7}{1 + i % 12:02d}{1 + i % 28:02d}",
                        update=update() if i % 5 == 0 else None, repo="o/r" if i % 2 else None))
    return out


class LibraryTestCase(QtCase):
    def page(self, statuses, skipped=(), ignored=(), width=1000, height=600) -> LibraryPage:
        self.factory_calls = 0

        def factory(s, skipped_, ignored_):
            self.factory_calls += 1
            return AppRow(s, skipped_, ignored_)

        page = LibraryPage(factory)
        holder = QWidget()
        QVBoxLayout(holder).addWidget(page)
        self.show(holder, width, height)
        page.set_statuses(statuses, set(skipped), set(ignored))
        self.app.processEvents()
        self.holder = holder
        return page

    @staticmethod
    def names(page: LibraryPage) -> list[str]:
        return [page._entries[uid].status.app.name for uid in page._order]

    @staticmethod
    def choose(box, key):
        box.setCurrentIndex(box.findData(key))


def awkward() -> list[AppStatus]:
    """Rows whose versions, names and chips all want more room than a minimum-width window has."""
    long_name = "Microsoft Visual Studio Community 2022 (64-bit) - Long Edition With A Remarkably Long Product Name"
    publisher = "Microsoft Corporation and Friends Of The Visual Studio Foundation International Ltd."
    return [
        make("Adobe Reader", "24.002.20933", publisher="Adobe Inc.", size_kb=500_000, date="20260518",
             winget_id="Adobe.Acrobat.Reader.64-bit",
             update=Update("winget", "24.003.20054", "Adobe.Acrobat.Reader.64-bit", size=300 * 1024 ** 2)),
        make("OBS Studio", "30.2.2", publisher="OBS Project", size_kb=1_200_000, date="20261101",
             update=update("31.0.0-beta1+very-long-prerelease-identifier.2026", size=1_288_490_188)),
        make(long_name, "17.11.35312.102", publisher=publisher, size_kb=9_500_000, date="20260101",
             badges=[SECURITY, EOL, OUTDATED], pin={"mode": "hold", "version": "17.11.35312.102"},
             update=update("2024.03.1.windows.4", size=1_800 * 1024 ** 2)),
        make("Spotify", "1.2.40.599.g0ccbc5ef", publisher="Spotify AB", scope="user", size_kb=280_000, date="20261015",
             update=update("1.2.52.442.g3c2f9b10", size=90 * 1024 ** 2)),
        make("Zoom Workplace", "6.1.0 (45123)", publisher="Zoom Communications, Inc.", size_kb=400_000, date="20261010",
             update=update("6.2.5 (47123)", size=180 * 1024 ** 2)),
        make("7-Zip", "23.01", size_kb=5_800, date="20250101", update=update("24.08", size=1_500_000)),
        make("Plain", "1.0", size_kb=100, date="20250101"),
    ]


class RowColumnsFitTest(LibraryTestCase):
    """The columns of a row give way in a fixed order, so that the name stays readable at the narrowest window.
    Rows are shown one by one at an exact width (the library gives its rows 722 px in a window of 800)."""

    def rows(self, width: int, select: bool = False) -> dict[str, AppRow]:
        out = {}
        for status in awkward():
            app = status.app
            row = AppRow(status, False, False)
            row.set_meta(i18n.num(format_size(app.size_kb * 1024)), i18n.num(format_date(app.install_date)))
            row.set_selectable(select)
            self.show(row, width, 70)
            out[app.name] = row
        return out

    def test_the_name_keeps_its_minimum_in_a_window_of_the_minimum_width(self):
        for lang in ("en", "fa"):
            apply_language(self.app, lang)
            for select in (False, True):
                for name, row in self.rows(722, select).items():
                    with self.subTest(lang=lang, select=select, app=name):
                        self.assertGreaterEqual(row.name.width(), NAME_MIN)

    def test_the_name_keeps_its_minimum_in_wider_windows_too(self):
        for width in (722, 899, 900, 1000, 1300):
            for name, row in self.rows(width, True).items():
                with self.subTest(width=width, app=name):
                    self.assertGreaterEqual(row.name.width(), NAME_MIN)

    def test_a_long_version_is_elided_and_the_tooltip_has_all_of_it(self):
        row = self.rows(722, True)["OBS Studio"]
        full = "31.0.0-beta1+very-long-prerelease-identifier.2026"
        self.assertIsInstance(row.new_ver, VersionLabel)
        self.assertEqual((row.new_ver.full_text, row.new_ver.toolTip()), (full, full))
        self.assertLessEqual(row.new_ver.width(), VERSION_MAX)
        self.assertLess(row.new_ver.width(), row.new_ver.fontMetrics().horizontalAdvance(full), "so it is elided")
        self.assertEqual(row.old_ver.toolTip(), "30.2.2")

    def test_versions_that_fit_are_never_cut(self):
        row = self.rows(1300)["7-Zip"]
        for label in (row.old_ver, row.new_ver):
            self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(label.full_text))

    def test_a_short_version_keeps_its_width_while_the_long_one_gives_way(self):
        self.assertEqual(share_room([40, 150], 100), [40, 60])
        self.assertEqual(share_room([150, 40], 100), [60, 40])
        self.assertEqual(share_room([100, 100], 120), [60, 60])
        self.assertEqual(share_room([30, 500], 1000), [30, VERSION_MAX], "never above the maximum")
        self.assertEqual(share_room([200], 90), [90])
        self.assertEqual(share_room([20, 500], 10), [20, VERSION_MIN], "never below the minimum, or the text's own width")

    def test_size_and_date_columns_are_hidden_below_900px_and_back_at_900(self):
        for width, visible in ((722, False), (899, False), (NARROW_ROW, True), (1100, True)):
            for name, row in self.rows(width).items():
                with self.subTest(width=width, app=name):
                    self.assertEqual(not row._size_label.isHidden() and not row._date_label.isHidden(), visible)

    def test_the_action_area_and_gaps_pack_tighter_in_a_narrow_row(self):
        narrow, wide = self.rows(722)["Spotify"], self.rows(1100)["Spotify"]
        self.assertEqual((narrow.actions.width(), wide.actions.width()), (ACTIONS_NARROW, ACTIONS_WIDE))
        self.assertLess(narrow._lay.spacing(), wide._lay.spacing())

    def test_the_columns_refit_when_the_row_is_widened_or_narrowed_again(self):
        row = self.rows(1100, True)["Spotify"]
        wide_name = row.name.width()
        row.resize(722, row.height())
        self.app.processEvents()
        self.assertTrue(row._size_label.isHidden())
        self.assertGreaterEqual(row.name.width(), NAME_MIN)
        row.resize(1100, row.height())
        self.app.processEvents()
        self.assertFalse(row._size_label.isHidden())
        self.assertEqual(row.name.width(), wide_name)

    def test_a_finished_update_refits_the_version_that_is_left(self):
        row = self.rows(722, True)["OBS Studio"]
        row.set_result(True, "Installed 31.0.0", "31.0.0-beta1+very-long-prerelease-identifier.2026")
        self.app.processEvents()
        self.assertTrue(row.new_ver.isHidden())
        self.assertGreaterEqual(row.name.width(), NAME_MIN)
        self.assertLessEqual(row.old_ver.width(), VERSION_MAX)

    def test_a_row_without_a_size_yet_still_builds(self):
        row = AppRow(awkward()[0], False, False)
        row.build()                              # no parent, no layout pass yet: the first fit must not fail
        row.set_meta("1.0 GB", "2024-03-04")
        row.set_selectable(True)
        self.assertGreaterEqual(row.old_ver.maximumWidth(), 1)

    def test_the_library_gives_its_rows_the_same_fit(self):
        page = self.page(awkward(), width=1100, height=900)    # the page's own minimum is wider than 800
        for uid in page._order:
            row = page.row_for(uid)
            with self.subTest(app=row.status.app.name):
                self.assertGreaterEqual(row.name.width(), NAME_MIN)
                self.assertLessEqual(row.old_ver.width(), VERSION_MAX)


class LongNoteTest(LibraryTestCase):
    NOTE = ("HTTPSConnectionPool(host='example.invalid', port=443): Max retries exceeded with url: /download "
            "(Caused by NameResolutionError)")

    def with_note(self, name="Alpha") -> AppStatus:
        s = make(name, repo="o/r")
        s.set_note("Site check failed: {error}", error=self.NOTE)
        return s

    def test_a_long_scan_note_is_one_elided_line_with_all_of_it_in_the_tooltip(self):
        row = AppRow(self.with_note(), False, False)
        self.show(row, 760, 90)
        full = "Site check failed: " + self.NOTE
        self.assertEqual((row.state.full_text, row.state.toolTip(), row.toolTip()), (full, full, full))
        self.assertGreater(row.state.fontMetrics().horizontalAdvance(full), row.state.width(), "so it is elided")
        self.assertLessEqual(row.state.sizeHint().height(), row.state.fontMetrics().height() + 4, "one line")

    def test_a_note_with_line_breaks_is_still_one_line(self):
        s = make("Alpha", repo="o/r")
        s.set_note("{error}", error="first line\nsecond line\n\nthird line")
        row = AppRow(s, False, False)
        self.show(row, 760, 90)
        self.assertEqual(row.state.full_text, "first line second line third line")
        self.assertEqual(row.state.toolTip(), "first line\nsecond line\n\nthird line")

    def test_a_long_note_does_not_make_its_row_taller(self):
        short = AppRow(make("Alpha", repo="o/r"), False, False)
        long_ = AppRow(self.with_note(), False, False)
        for lang in ("en", "fa"):
            apply_language(self.app, lang)
            self.assertEqual(long_.sizeHint().height(), short.sizeHint().height(), lang)

    def test_one_long_note_cannot_make_every_row_of_the_library_taller(self):
        plain = [make(f"App {i}", repo="o/r") for i in range(6)]
        baseline = self.page(plain)._row_h
        for statuses in ([self.with_note("Noted")] + plain, plain + [self.with_note("Noted")]):
            page = self.page(statuses)       # the first app of the list is the one the row height is measured on
            self.assertTrue(any(e.status.note for e in page._entries.values()))
            self.assertEqual(page._row_h, baseline)
            heights = {page.row_for(uid).height() for uid in page._order if not page.row_for(uid).isHidden()}
            self.assertEqual(heights, {baseline})

    def test_the_note_follows_a_language_switch_and_stays_elided(self):
        s = self.with_note()
        row = AppRow(s, False, False)
        self.show(row, 760, 90)
        apply_language(self.app, "fa")
        row.refresh(s, False, False)
        self.app.processEvents()
        self.assertEqual(row.state.toolTip(), s.note_text())
        self.assertNotEqual(row.state.toolTip(), "Site check failed: " + self.NOTE)
        self.assertLessEqual(row.state.sizeHint().height(), row.state.fontMetrics().height() + 4)


class SortTest(LibraryTestCase):
    def test_every_sort_the_plan_asks_for_exists(self):
        page = self.page([make()])
        self.assertEqual([page.sort_box.itemData(i) for i in range(page.sort_box.count())],
                         ["name", "size", "date", "publisher", "updates"])
        self.assertEqual(page.sort_box.itemText(1), "Size, largest first")
        self.assertEqual([k for k, _ in SORTS], [page.sort_box.itemData(i) for i in range(len(SORTS))])

    def test_by_name_ignores_case(self):
        page = self.page([make("beta"), make("Alpha"), make("charlie")])
        self.assertEqual(self.names(page), ["Alpha", "beta", "charlie"])

    def test_by_size_puts_the_largest_first_and_unknown_last(self):
        page = self.page([make("small", size_kb=20), make("none"), make("big", size_kb=9000), make("mid", size_kb=500)])
        self.choose(page.sort_box, "size")
        self.assertEqual(self.names(page), ["big", "mid", "small", "none"])

    def test_by_install_date_puts_the_newest_first_and_unknown_last(self):
        page = self.page([make("old", date="20230101"), make("none"), make("new", date="20250601"),
                          make("junk", date="soon"), make("mid", date="20240101")])
        self.choose(page.sort_box, "date")
        self.assertEqual(self.names(page), ["new", "mid", "old", "junk", "none"])

    def test_by_publisher_puts_unknown_last(self):
        page = self.page([make("a", publisher="beta"), make("b", publisher=""), make("c", publisher="Alpha")])
        self.choose(page.sort_box, "publisher")
        self.assertEqual(self.names(page), ["c", "a", "b"])

    def test_updates_first_does_not_count_a_skipped_version(self):
        a, b, c = make("a"), make("b", update=update()), make("c", update=update())
        page = self.page([a, b, c], skipped={c.uid})
        self.choose(page.sort_box, "updates")
        self.assertEqual(self.names(page), ["b", "a", "c"])

    def test_sorting_and_filtering_never_rebuild_rows(self):
        page = self.page(fleet(40))
        for key in ("size", "date", "publisher", "updates", "name"):
            self.choose(page.sort_box, key)
        for key, _ in FILTERS:
            self.choose(page.filter_box, key)
        page.set_filter_text("app")
        self.assertEqual(self.factory_calls, 40)


class FilterTest(LibraryTestCase):
    def test_filters(self):
        upd = make("with update", update=update())
        held = make("pinned", pin={"mode": "hold", "version": "1.0"})
        flagged = make("flagged", badges=[SECURITY])
        user = make("per user", scope="user")
        skipped = make("skipped", update=update())
        page = self.page([upd, held, flagged, user, skipped], skipped={skipped.uid})
        expect = {
            "all": 5, "updates": 1, "machine": 4, "user": 1, "duplicates": 0, "flagged": 1, "pinned": 1,
        }
        for key, count in expect.items():
            self.choose(page.filter_box, key)
            self.assertEqual(page.visible_count(), count, key)
        self.assertEqual(page.count(), 5)
        self.choose(page.filter_box, "updates")
        self.assertEqual(self.names(page), ["with update"])

    def test_search_text_matches_name_and_publisher_without_case(self):
        page = self.page([make("Notepad++", publisher="Don Ho"), make("Firefox", publisher="Mozilla"),
                          make("Thunderbird", publisher="Mozilla")])
        page.set_filter_text("  MOZ ")
        self.assertEqual(self.names(page), ["Firefox", "Thunderbird"])
        page.set_filter_text("note")
        self.assertEqual(self.names(page), ["Notepad++"])
        self.choose(page.filter_box, "updates")       # the search and the filter both apply
        self.assertEqual(page.visible_count(), 0)
        page.set_filter_text("")
        self.assertEqual(page.visible_count(), 0)
        self.choose(page.filter_box, "all")
        self.assertEqual(page.visible_count(), 3)

    def test_an_empty_result_says_so_inside_the_page(self):
        page = self.page([make("one")])
        page.set_filter_text("zzz")
        self.assertIs(page._stack.currentWidget(), page._empty)
        self.assertEqual(page._empty_title.text(), "Nothing matches")
        self.assertNotEqual(page._empty_text.text(), "")
        self.assertTrue(page.filter_box.isVisible(), "the toolbar stays, so the filter can be changed")
        page.set_filter_text("")
        self.assertIs(page._stack.currentWidget(), page._scroll)

    def test_an_empty_library(self):
        page = self.page([])
        self.assertEqual((page.visible_count(), page._empty_title.text(), page._empty_text.text()),
                         (0, "No apps found", ""))

    def test_the_summary_counts_apps_and_adds_up_the_sizes(self):
        page = self.page([make("alpha", size_kb=1024 * 1024, publisher="V"),
                          make("beta", size_kb=1024 * 1024, publisher="V"), make("gamma", publisher="V")])
        self.assertEqual(page.summary.text(), "3 apps · 2.0 GB")
        page.set_filter_text("alp")
        self.assertEqual(page.summary.text(), "1 of 3 apps · 1.0 GB")
        self.assertEqual(self.page([make("solo")]).summary.text(), "1 app")


class DuplicateTest(LibraryTestCase):
    def dup_names(self, *statuses) -> set[str]:
        found = find_duplicates(list(statuses))
        return {s.app.name for s in statuses if s.uid in found}

    def test_the_same_app_in_two_versions_or_two_scopes_is_a_possible_duplicate(self):
        a, b = make("7-Zip 24.07 (x64)", "24.07", "Igor Pavlov"), make("7-Zip 23.01 (x64)", "23.01", "Igor Pavlov")
        self.assertEqual(self.dup_names(a, b), {a.app.name, b.app.name})
        c, d = make("Notepad++", "8.6", "Don Ho", "machine"), make("Notepad++", "8.6", "Don Ho", "user")
        self.assertEqual(self.dup_names(c, d), {"Notepad++"})
        self.assertEqual(find_duplicates([c, d])[c.uid], [d])

    def test_one_product_in_two_architectures_is_not_a_duplicate(self):
        a = make("Tool 2.1.0 (x64)", "2.1.0", "Acme", arch="x64")
        b = make("Tool 2.1.0 (x86)", "2.1.0", "Acme", arch="x86")
        self.assertEqual(self.dup_names(a, b), set())

    def test_other_publishers_or_names_are_not_duplicates(self):
        self.assertEqual(self.dup_names(make("Tool", "1.0", "Acme"), make("Tool", "2.0", "Other Co")), set())
        self.assertEqual(self.dup_names(make("Tool", "1.0"), make("Tool Pro", "2.0")), set())
        self.assertEqual(self.dup_names(make("Solo", "1.0")), set())

    def test_side_by_side_families_are_left_alone(self):
        families = [
            ("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33135", "14.38.33135"),
            ("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.36.32532", "14.36.32532"),
            ("Microsoft .NET Runtime - 8.0.1 (x64)", "8.0.1"),
            ("Microsoft .NET Runtime - 6.0.25 (x64)", "6.0.25"),
            ("Microsoft ASP.NET Core 7.0.14 - Shared Framework (x64)", "7.0.14"),
            ("Microsoft Windows Desktop Runtime - 7.0.14 (x64)", "7.0.14"),
            ("Microsoft Windows App Runtime 1.4", "4000.1"), ("Microsoft Windows App Runtime 1.5", "5001.1"),
            ("Eclipse Temurin JDK with Hotspot 17.0.9+9 (x64)", "17.0.9.9"),
            ("Eclipse Temurin JDK with Hotspot 21.0.1+12 (x64)", "21.0.1.12"),
        ]
        statuses = [make(name, version, "Vendor") for name, version in families]
        # each pair shares a publisher and the version differs: only the family rule keeps them out
        self.assertEqual(self.dup_names(*statuses), set())

    def test_python_is_compared_per_minor_version(self):
        old, new = make("Python 3.11.5 (64-bit)", "3.11.5150.0", "Python Software Foundation"), \
            make("Python 3.12.4 (64-bit)", "3.12.4150.0", "Python Software Foundation")
        self.assertEqual(self.dup_names(old, new), set())
        again = make("Python 3.12.1 (64-bit)", "3.12.1150.0", "Python Software Foundation")
        self.assertEqual(self.dup_names(old, new, again), {"Python 3.12.4 (64-bit)", "Python 3.12.1 (64-bit)"})

    def test_the_page_marks_the_rows_and_filters_them(self):
        a, b = make("Tool 1.0", "1.0", "Acme"), make("Tool 2.0", "2.0", "Acme")
        page = self.page([a, b, make("Other")])
        self.choose(page.filter_box, "duplicates")
        self.assertEqual(page.visible_count(), 2)
        row = page.row_for(a.uid)
        row.build()
        chip = next(c for c in row.meta_line.chips() if c.kind == "duplicate")
        self.assertEqual(chip.text, "Possible duplicate")
        self.assertIn("2.0", chip.toolTip())
        self.assertIn("All users", chip.toolTip())
        self.choose(page.filter_box, "all")
        self.assertEqual(page.visible_count(), 3)


class SelectionTest(LibraryTestCase):
    def test_select_mode_shows_ticks_and_the_bar(self):
        page = self.page(fleet(8))
        self.assertFalse(page._bar.isVisible())
        page.select_btn.click()
        self.assertTrue(page.select_mode)
        self.assertTrue(page._bar.isVisible())
        self.assertEqual(page.select_btn.text(), "Done")
        first = page.row_for(page._order[0])
        self.assertTrue(first._check.isVisibleTo(first))
        for b in (page._uninstall_btn, page._export_btn, page._ignore_btn, page._clear_btn):
            self.assertFalse(b.isEnabled())

    def test_ticking_rows_emits_the_batch_signals_with_the_uids_in_display_order(self):
        page = self.page(fleet(8))
        page.set_select_mode(True)
        want = [page._order[3], page._order[1]]
        for uid in want:
            page.row_for(uid)._check.click()
        self.assertEqual(page.selected_uids(), [page._order[1], page._order[3]])
        self.assertEqual(page._count.text(), "2 selected")
        got = {}
        page.batch_uninstall_requested.connect(lambda uids: got.setdefault("uninstall", uids))
        page.batch_export_requested.connect(lambda uids: got.setdefault("export", uids))
        page.batch_ignore_requested.connect(lambda uids: got.setdefault("ignore", uids))
        for button in (page._uninstall_btn, page._export_btn, page._ignore_btn):
            self.assertTrue(button.isEnabled())
            button.click()
        self.assertEqual(got, {"uninstall": page.selected_uids(), "export": page.selected_uids(),
                               "ignore": page.selected_uids()})
        self.assertEqual(page.selected_uids(), [page._order[1], page._order[3]],
                         "the ticks stay for the window to act on")

    def test_ctrl_a_ticks_the_visible_rows_and_escape_leaves(self):
        page = self.page(fleet(20))
        page.set_filter_text("publisher 3")
        self.assertTrue(0 < page.visible_count() < 20)
        page._select_all_key.activated.emit()
        self.assertTrue(page.select_mode)
        self.assertEqual(len(page.selected_uids()), page.visible_count())
        self.assertFalse(page._all_btn.isEnabled())
        self.assertTrue(page._leave_key.isEnabled())
        page._leave_key.activated.emit()
        self.assertFalse(page.select_mode)
        self.assertEqual(page.selected_uids(), [])
        self.assertFalse(page._bar.isVisible())
        self.assertFalse(page.select_btn.isChecked())
        self.assertFalse(page._leave_key.isEnabled(), "Esc is left alone when there is nothing to leave")

    def test_a_tick_never_outlives_the_visibility_of_its_row(self):
        page = self.page(fleet(20))
        page._ctrl_a()
        self.assertEqual(len(page.selected_uids()), 20)
        page.set_filter_text("application 00")
        shown = page.visible_count()
        self.assertTrue(0 < shown < 20)
        self.assertEqual(len(page.selected_uids()), shown)
        self.assertEqual(page._count.text(), f"{shown} selected")
        page.set_filter_text("")
        self.assertEqual(len(page.selected_uids()), shown, "the hidden ticks are gone, not waiting to come back")

    def test_clear_selection_and_select_all_buttons(self):
        page = self.page(fleet(6))
        page.set_select_mode(True)
        page._all_btn.click()
        self.assertEqual(len(page.selected_uids()), 6)
        page._clear_btn.click()
        self.assertEqual(page.selected_uids(), [])
        self.assertTrue(page.select_mode)

    def test_set_statuses_keeps_sort_filter_mode_and_ticks_of_apps_that_remain(self):
        statuses = fleet(12)
        page = self.page(statuses)
        self.choose(page.sort_box, "size")
        self.choose(page.filter_box, "machine")
        page.set_select_mode(True)
        keep, drop = page._order[0], page._order[1]
        page.row_for(keep)._check.click()
        page.row_for(drop)._check.click()
        remaining = [s for s in statuses if s.uid != drop]
        page.set_statuses(remaining, set(), set())
        self.app.processEvents()
        self.assertEqual((page.sort_box.currentData(), page.filter_box.currentData(), page.select_mode),
                         ("size", "machine", True))
        self.assertEqual(page.selected_uids(), [keep])
        self.assertTrue(page.row_for(keep).selected)
        self.assertTrue(page.row_for(keep)._check.isChecked())


class LazyRowsAndSpeedTest(LibraryTestCase):
    def test_only_the_rows_near_the_viewport_are_built(self):
        page = self.page(fleet(150))
        built = [e for e in page._entries.values() if e.row._built]
        self.assertTrue(0 < len(built) < 40, len(built))
        last = page.row_for(page._order[-1])
        self.assertFalse(last._built)
        self.assertTrue(last.isHidden())
        bar = page._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.app.processEvents()
        self.assertTrue(last._built and not last.isHidden())
        self.assertTrue(page.row_for(page._order[0]).isHidden())
        self.assertLessEqual(last.geometry().bottom(), page._canvas.height())

    def test_150_rows_rebuild_in_under_a_second(self):
        statuses = fleet(150)
        page = self.page([])
        start = time.perf_counter()
        page.set_statuses(statuses, set(), set())
        self.app.processEvents()
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual(page.visible_count(), 150)
        start = time.perf_counter()
        page.set_statuses(statuses, set(), set())      # what the window does after every recheck or uninstall
        self.app.processEvents()
        self.assertLess(time.perf_counter() - start, 1.0)
        start = time.perf_counter()
        self.choose(page.sort_box, "size")
        self.choose(page.filter_box, "machine")
        self.app.processEvents()
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_the_scroll_position_survives_a_rebuild(self):
        statuses = fleet(60)
        page = self.page(statuses)
        bar = page._scroll.verticalScrollBar()
        bar.setValue(400)
        page.set_statuses(statuses, set(), set())
        self.app.processEvents()
        self.assertEqual(bar.value(), 400)

    def test_tab_visits_the_rows_in_the_order_they_are_shown(self):
        page = self.page(fleet(10))
        self.choose(page.sort_box, "size")
        self.app.processEvents()
        order = [page.row_for(uid) for uid in page._order if not page.row_for(uid).isHidden()]
        first, second = order[0].more, order[1].button
        self.assertIs(first.nextInFocusChain(), second)


class LanguageTest(LibraryTestCase):
    def test_a_language_switch_translates_the_page_and_its_rows(self):
        statuses = [make("Alpha", size_kb=1024 * 1024, date="20240304", update=update(size=1_048_576)),
                    make("Beta", size_kb=2048, date="20250101")]
        page = self.page(statuses)
        self.assertEqual(page.sort_box.itemText(0), "Name")
        apply_language(self.app, "fa")
        page.retranslate()
        page.set_statuses(statuses, set(), set())          # what the window does next
        self.app.processEvents()
        fa = i18n.FA
        self.assertEqual([page.filter_box.itemText(i) for i in (0, 1)], [fa["All"], fa["Updates available"]])
        self.assertEqual(page.sort_box.itemText(1), fa["Size, largest first"])
        self.assertEqual(page.select_btn.text(), fa["Select"])
        self.assertIn(i18n.num("2"), page.summary.text())
        self.assertNotIn("2", page.summary.text())
        row = page.row_for(statuses[0].uid)
        self.assertEqual(row._date_label.text(), i18n.num("2024-03-04"))
        self.assertEqual(page.layoutDirection(), Qt.RightToLeft)
        self.assertGreater(page._show_label.x(), page.sort_box.x(), "the toolbar runs right to left")
        apply_language(self.app, "en")
        page.retranslate()
        self.assertEqual(page.sort_box.itemText(0), "Name")
        self.assertEqual(row._date_label.text(), "2024-03-04")

    def test_every_new_string_has_a_persian_text_with_the_same_placeholders(self):
        from upstream.translations import library
        import string

        def fields(text):
            return {f for _, f, _, _ in string.Formatter().parse(text) if f}
        self.assertTrue(library.FA)
        for english, persian in library.FA.items():
            self.assertEqual(fields(english), fields(persian), english)
        for _, label in tuple(SORTS) + tuple(FILTERS):
            self.assertIn(label, i18n.FA)


if __name__ == "__main__":
    unittest.main()
