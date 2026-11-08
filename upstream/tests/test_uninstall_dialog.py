"""The uninstall dialogs offscreen: the ticked review, the confirmation, the cleanup and its undo, the batch dialog
and the backups dialog. Uninstallers, the leftover scan and the cleanup itself are fakes: nothing is removed."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import dataclasses
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from upstream import cleanup, i18n, installer, leftovers, uninstaller
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus
from upstream.ui import theme, uninstall as un
from upstream.ui.locale import apply_language

app = QApplication.instance() or QApplication([])
_glyphs: list = []


def setUpModule():
    folder = Path(tempfile.mkdtemp(prefix="upstream_ui_theme_"))     # theme PNGs stay out of the real data folder
    patch = mock.patch.object(theme, "DATA_DIR", folder)
    patch.start()
    _glyphs[:] = [patch, folder]
    apply_language(app, "en")
    theme.apply_theme(app, "paper")


def tearDownModule():
    patch, folder = _glyphs
    patch.stop()
    shutil.rmtree(folder, True)


def pump(until=None, limit: float = 10.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        app.processEvents()
        if until is None or until():
            return True
        time.sleep(0.005)
    return until is None


def make_app(name: str, key: str | None = None, **kw) -> InstalledApp:
    return InstalledApp(key or f"HKCU\\{name}", name, "1.0", "Vendor Inc", "user", "x64", **kw)


def item(kind: str, location: str, confidence: str = leftovers.CERTAIN, size: int | None = None, large: bool = False,
         reason: str = leftovers.R_NAMED) -> leftovers.Leftover:
    return leftovers.Leftover(kind, location, confidence, reason, {}, size, "", large)


ITEMS = [
    item("folder", "C:\\Data\\Foo", leftovers.CERTAIN, 5000),
    item("shortcut", "C:\\Data\\Foo.lnk", leftovers.CERTAIN, 1200),
    item("folder", "C:\\Data\\HugeGame", leftovers.CERTAIN, None, large=True),
    item("regkey", "HKCU\\Software\\Foo", leftovers.LIKELY),
    item("regvalue", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run|Foo", leftovers.LIKELY),
    item("folder", "C:\\Data\\FooMaybe", leftovers.POSSIBLE, 10),
    item("regkey", "HKLM\\SOFTWARE\\Foo", leftovers.CERTAIN),
    item("folder", "C:\\Windows", leftovers.PROTECTED, None, reason=leftovers.R_INSIDE_OTHER),
]


def pick(bar, key, reverse=False):
    """What a keyboard user does with a Sort by bar: choose the key, then tick or clear Reverse order."""
    bar.combo.setCurrentIndex(bar.combo.findData(key))
    bar.reverse.setChecked(reverse)


def fake_snapshot(items):
    """Stands in for cleanup.snapshot: the review makes it on its scan thread, and the real one reads the registry."""
    return {(i.kind, i.location.lower()): "state of " + i.location for i in items if i.kind in ("regkey", "regvalue", "service")}


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upstream_ui_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enter(mock.patch.object(uninstaller, "run", side_effect=AssertionError("a real uninstaller was started")))
        self.enter(mock.patch.object(installer, "_shell_run", side_effect=AssertionError("a real uninstaller was started")))
        self.enter(mock.patch.object(cleanup, "is_admin", lambda: False))
        self.enter(mock.patch.object(un.cleanup, "snapshot", fake_snapshot))
        un._RUNNING.clear()
        self.dialogs = []
        self.addCleanup(self.close_dialogs)

    def enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def close_dialogs(self):
        for d in self.dialogs:
            d.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        left = un.running()
        un._RUNNING.clear()
        self.assertEqual(left, 0, "a sentinel was left in the running set")

    def keep(self, dialog):
        self.dialogs.append(dialog)
        return dialog

    def single(self, **kw) -> un.UninstallDialog:
        a = make_app("Foo App")
        status = AppStatus(a)
        quiet = uninstaller.UninstallPlan(a, "quiet", "C:\\x\\unins000.exe", "/S", True)
        window = uninstaller.UninstallPlan(a, "interactive", "C:\\x\\unins000.exe", "", False)
        with mock.patch.object(un.uninstaller, "plan", lambda app_, wid=None, prefer_silent=True: quiet if prefer_silent else window):
            d = self.keep(un.UninstallDialog(status, [a], **kw))
        return d

    def review(self, items=ITEMS, **kw) -> un.UninstallDialog:
        d = self.single(**kw)
        d._on_scan_done(list(items))
        d._show(d.p_review)
        return d

    def rows(self, d) -> dict[str, QTreeWidgetItem]:
        found = {}
        for g in range(d.tree.topLevelItemCount()):
            head = d.tree.topLevelItem(g)
            for i in range(head.childCount()):
                row = head.child(i)
                found[d.items[row.data(0, Qt.UserRole)].location] = row
        return found

    def heads(self, d) -> dict[str, QTreeWidgetItem]:
        return {d.tree.topLevelItem(g).text(0).split()[0]: d.tree.topLevelItem(g) for g in range(d.tree.topLevelItemCount())}


class ReviewTicks(Base):
    def test_only_certain_and_not_huge_start_ticked(self):
        d = self.review()
        rows = self.rows(d)
        ticked = {loc for loc, row in rows.items() if row.checkState(0) == Qt.Checked}
        self.assertEqual(ticked, {"C:\\Data\\Foo", "C:\\Data\\Foo.lnk", "HKLM\\SOFTWARE\\Foo"})
        self.assertEqual({id(i) for i in d.ticked()}, {id(i) for i in ITEMS if i.location in ticked})
        self.assertIs(rows["C:\\Data\\HugeGame"].checkState(0), Qt.Unchecked, "huge items are never preselected")

    def test_protected_rows_cannot_be_ticked_and_groups_tick_their_children(self):
        d = self.review()
        rows, heads = self.rows(d), self.heads(d)
        protected = rows["C:\\Windows"]
        self.assertFalse(protected.flags() & Qt.ItemIsUserCheckable)
        self.assertEqual(protected.data(0, un._ROW_ROLE), "protected")
        self.assertTrue(heads["Certain"].flags() & Qt.ItemIsUserCheckable)
        self.assertTrue(heads["Likely"].flags() & Qt.ItemIsUserCheckable)
        self.assertFalse(heads["Possible"].flags() & Qt.ItemIsUserCheckable, "no select-all for weak matches")
        self.assertFalse(heads["Protected"].flags() & Qt.ItemIsUserCheckable)
        self.assertIs(heads["Certain"].checkState(0), Qt.PartiallyChecked)
        heads["Likely"].setCheckState(0, Qt.Checked)
        self.assertIs(rows["HKCU\\Software\\Foo"].checkState(0), Qt.Checked)
        self.assertIs(rows["C:\\Data\\FooMaybe"].checkState(0), Qt.Unchecked)
        self.assertNotIn(ITEMS[-1], d.ticked())
        rows["C:\\Data\\HugeGame"].setCheckState(0, Qt.Checked)
        self.assertIs(heads["Certain"].checkState(0), Qt.Checked, "every certain item is ticked, so the group box is too")
        rows["C:\\Data\\Foo"].setCheckState(0, Qt.Unchecked)
        self.assertIs(heads["Certain"].checkState(0), Qt.PartiallyChecked)

    def test_indeterminate_glyph_is_drawn_once_per_colour(self):
        path = un._dash_glyph("#123456")
        self.assertTrue(path.endswith("dash-123456.png") and Path(path).is_file())
        self.assertEqual(un._dash_glyph("#123456"), path)

    def test_remove_button_follows_the_ticks(self):
        d = self.review()
        self.assertTrue(d.remove_btn.isEnabled())
        self.assertIn("3", d.pick_line.text())
        for row in self.rows(d).values():
            if row.flags() & Qt.ItemIsUserCheckable:
                row.setCheckState(0, Qt.Unchecked)
        self.assertFalse(d.remove_btn.isEnabled())
        self.assertEqual(d.pick_line.text(), i18n.tr("Nothing ticked"))
        self.rows(d)["C:\\Data\\FooMaybe"].setCheckState(0, Qt.Checked)
        self.assertTrue(d.remove_btn.isEnabled())
        self.assertIn("10 B", d.pick_line.text())

    def test_nothing_found_and_the_old_review_features_still_work(self):
        d = self.single()
        d._on_scan_done([])
        self.assertEqual(d.review_stack.currentIndex(), 2)
        d = self.review()
        row = self.rows(d)["C:\\Data\\Foo"]
        d.tree.setCurrentItem(row)
        self.assertIs(d.selected(), ITEMS[0])
        self.assertIn(leftovers.R_NAMED, d.detail_text.text())
        self.assertEqual(d.detail_path.text(), "C:\\Data\\Foo")
        self.assertTrue(d.open_btn.isEnabled())
        self.assertTrue(d.tree.isColumnHidden(3), "the app column is for the batch review only")
        text = d.export_text()
        self.assertIn("[Folder] C:\\Data\\Foo", text)
        self.assertNotIn("From ", text)
        self.assertEqual(d.stack.currentIndex(), un.REVIEW)


class ReviewSort(Base):
    """The columns of the leftover review sort the rows of each group; ticks and the selection stay with their rows."""

    def order(self, d, group="Certain") -> list[str]:
        head = self.heads(d)[group]
        return [d.items[head.child(i).data(0, Qt.UserRole)].location for i in range(head.childCount())]

    def click(self, d, column):
        d.tree.header().sectionClicked.emit(column)

    DEFAULT = ["C:\\Data\\Foo", "C:\\Data\\Foo.lnk", "C:\\Data\\HugeGame", "HKLM\\SOFTWARE\\Foo"]

    def test_the_review_keeps_its_order_until_a_header_is_clicked(self):
        d = self.review()
        self.assertEqual(self.order(d), self.DEFAULT)
        self.assertFalse(d.tree.header().isSortIndicatorShown())

    def test_space_used_sorts_biggest_first_then_reverses_and_unknown_sizes_stay_last(self):
        d = self.review()
        self.click(d, 2)
        self.assertEqual(self.order(d), ["C:\\Data\\Foo", "C:\\Data\\Foo.lnk", "C:\\Data\\HugeGame", "HKLM\\SOFTWARE\\Foo"])
        self.assertTrue(d.tree.header().isSortIndicatorShown())
        self.assertEqual((d.tree.header().sortIndicatorSection(), d.tree.header().sortIndicatorOrder()),
                         (2, Qt.DescendingOrder))
        self.click(d, 2)
        self.assertEqual(self.order(d), ["C:\\Data\\Foo.lnk", "C:\\Data\\Foo", "C:\\Data\\HugeGame", "HKLM\\SOFTWARE\\Foo"])
        self.assertEqual(d.tree.header().sortIndicatorOrder(), Qt.AscendingOrder)

    def test_item_and_type_sort_alphabetically(self):
        d = self.review()
        self.click(d, 0)
        self.assertEqual(self.order(d), sorted(self.DEFAULT, key=str.casefold))
        self.click(d, 0)
        self.assertEqual(self.order(d), sorted(self.DEFAULT, key=str.casefold, reverse=True))
        self.click(d, 1)
        kinds = [i.kind for i in (d.items[self.heads(d)["Certain"].child(n).data(0, Qt.UserRole)] for n in range(4))]
        labels = [i18n.tr(un.KIND_LABELS[k]).casefold() for k in kinds]
        self.assertEqual(labels, sorted(labels))

    def test_the_why_column_does_not_sort(self):
        d = self.review()
        self.click(d, 2)
        before = self.order(d)
        self.click(d, 4)
        self.assertEqual(self.order(d), before)

    def test_groups_stay_in_their_places_and_every_group_is_sorted(self):
        d = self.review()
        self.click(d, 0)
        self.assertEqual(list(self.heads(d)), ["Certain", "Likely", "Possible", "Protected"])
        self.assertEqual(self.order(d, "Likely"), sorted(self.order(d, "Likely"), key=str.casefold))

    def test_ticks_and_the_selection_follow_their_rows(self):
        d = self.review()
        rows = self.rows(d)
        rows["C:\\Data\\HugeGame"].setCheckState(0, Qt.Checked)
        rows["C:\\Data\\Foo.lnk"].setCheckState(0, Qt.Unchecked)
        d.tree.setCurrentItem(rows["C:\\Data\\HugeGame"])
        before = {id(i) for i in d.ticked()}
        self.click(d, 2)
        self.click(d, 0)
        self.assertEqual({id(i) for i in d.ticked()}, before)
        rows = self.rows(d)
        self.assertIs(rows["C:\\Data\\Foo.lnk"].checkState(0), Qt.Unchecked)
        self.assertIs(d.selected(), ITEMS[2])
        self.assertEqual(d.detail_path.text(), "C:\\Data\\HugeGame")
        self.assertIn("3", d.pick_line.text())

    def test_the_sort_by_box_sorts_like_the_headers(self):
        d = self.review()
        bar = d.sort_bar
        self.assertEqual([bar.combo.itemText(i) for i in range(bar.combo.count())],
                         [i18n.tr("Item"), i18n.tr("Type"), i18n.tr("Space used")], "no app column for a single app")
        self.assertEqual((bar.combo.currentIndex(), bar.reverse.isEnabled()), (-1, False), "nothing is sorted yet")
        self.assertEqual(self.order(d), self.DEFAULT)
        pick(bar, "size")
        self.assertEqual(self.order(d), ["C:\\Data\\Foo", "C:\\Data\\Foo.lnk", "C:\\Data\\HugeGame", "HKLM\\SOFTWARE\\Foo"])
        self.assertEqual((d.tree.header().sortIndicatorSection(), d.tree.header().sortIndicatorOrder()), (2, Qt.DescendingOrder))
        self.assertTrue(bar.reverse.isEnabled())
        bar.reverse.setChecked(True)
        self.assertEqual(self.order(d), ["C:\\Data\\Foo.lnk", "C:\\Data\\Foo", "C:\\Data\\HugeGame", "HKLM\\SOFTWARE\\Foo"])
        self.assertEqual(d.tree.header().sortIndicatorOrder(), Qt.AscendingOrder)
        pick(bar, "location")
        self.assertEqual(self.order(d), sorted(self.DEFAULT, key=str.casefold), "Item runs A to Z, Reverse order is cleared")
        bar.reverse.setChecked(True)
        self.assertEqual(self.order(d), sorted(self.DEFAULT, key=str.casefold, reverse=True))

    def test_the_sort_by_box_follows_the_headers(self):
        d = self.review()
        self.click(d, 2)
        self.assertEqual((d.sort_bar.combo.currentData(), d.sort_bar.reverse.isChecked()), ("size", False))
        self.click(d, 2)
        self.assertEqual((d.sort_bar.combo.currentData(), d.sort_bar.reverse.isChecked()), ("size", True))
        self.click(d, 0)
        self.assertEqual((d.sort_bar.combo.currentData(), d.sort_bar.reverse.isChecked()), ("location", False))
        pick(d.sort_bar, "kind")
        self.click(d, 1)
        self.assertTrue(d.sort_bar.reverse.isChecked(), "the same header again reverses, whoever sorted first")

    def test_the_sort_by_box_works_from_the_keyboard(self):
        d = self.review()
        bar = d.sort_bar
        for widget in (bar.combo, bar.reverse):
            self.assertNotEqual(widget.focusPolicy(), Qt.NoFocus)
        self.assertEqual(bar.label.buddy(), bar.combo)
        QTest.keyClick(bar.combo, Qt.Key_Down)
        self.assertEqual(bar.combo.currentData(), "location")
        self.assertEqual(self.order(d), sorted(self.DEFAULT, key=str.casefold))
        QTest.keyClick(bar.reverse, Qt.Key_Space)
        self.assertEqual(self.order(d), sorted(self.DEFAULT, key=str.casefold, reverse=True))

    def test_ticks_and_the_selection_follow_their_rows_through_the_box(self):
        d = self.review()
        rows = self.rows(d)
        rows["C:\\Data\\HugeGame"].setCheckState(0, Qt.Checked)
        rows["C:\\Data\\Foo.lnk"].setCheckState(0, Qt.Unchecked)
        d.tree.setCurrentItem(rows["C:\\Data\\HugeGame"])
        before = {id(i) for i in d.ticked()}
        pick(d.sort_bar, "size")
        pick(d.sort_bar, "location", True)
        self.assertEqual({id(i) for i in d.ticked()}, before)
        self.assertIs(d.selected(), ITEMS[2])

    def test_the_app_column_of_a_combined_review_sorts_through_the_box_too(self):
        b = self.keep(un.BatchUninstallDialog([AppStatus(make_app("Alpha"))], [make_app("Alpha")]))
        items = [item("folder", "C:\\A", size=1), item("folder", "C:\\B", size=2), item("folder", "C:\\C", size=3)]
        b._on_scan_done(items, ["Zed", "", "Amy"])
        self.assertIn(i18n.tr("From app"), [b.sort_bar.combo.itemText(i) for i in range(b.sort_bar.combo.count())])
        pick(b.sort_bar, "owner")
        self.assertEqual([b.items[b.tree.topLevelItem(0).child(i).data(0, Qt.UserRole)].location for i in range(3)],
                         ["C:\\C", "C:\\A", "C:\\B"], "Amy, Zed, then the item with no app")

    def test_the_app_column_of_a_combined_review_sorts_too(self):
        b = self.keep(un.BatchUninstallDialog([AppStatus(make_app("Alpha"))], [make_app("Alpha")]))
        items = [item("folder", "C:\\A", size=1), item("folder", "C:\\B", size=2), item("folder", "C:\\C", size=3)]
        b._on_scan_done(items, ["Zed", "", "Amy"])
        self.click(b, 3)
        self.assertEqual([b.items[b.tree.topLevelItem(0).child(i).data(0, Qt.UserRole)].location for i in range(3)],
                         ["C:\\C", "C:\\A", "C:\\B"], "Amy, Zed, then the item with no app")


class FakeCleanup:
    """Stands in for cleanup.run and cleanup.restore; the worker thread can be held until the test says so."""

    def __init__(self, **result):
        self.calls, self.restores = [], []
        self.result = result
        self.gate = threading.Event()
        self.gate.set()
        self.restore_gate = threading.Event()
        self.restore_gate.set()
        self.remaining = 0

    def run(self, items, name, progress, cancel=None, **kw):
        self.calls.append((list(items), name, kw))
        progress("Removing {item}", 40, item="Foo")
        self.gate.wait(5)
        done = [] if cancel is not None and cancel.is_set() else list(items)
        failed = [(items[0], cleanup.T_IN_USE, {})] if self.result.get("fail") else []
        return cleanup.CleanupResult(removed=len(items) - len(failed), failed=failed, backup_id="20260101-000000-abcdef",
                                     restore_point=self.result.get("point"), freed=6200, done=done,
                                     cancelled=bool(cancel is not None and cancel.is_set()))

    def restore(self, backup_id, progress=None):
        self.restores.append(backup_id)
        if progress:
            progress("Restoring {item}", 50, item="Foo")
        self.restore_gate.wait(5)
        return cleanup.RestoreResult(restored=2, failed=[("C:\\Data\\Foo", cleanup.R_EXISTS, {})], remaining=self.remaining)


class CleanupFlow(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeCleanup()
        self.enter(mock.patch.object(un.cleanup, "run", self.fake.run))
        self.enter(mock.patch.object(un.cleanup, "restore", self.fake.restore))

    def test_confirmation_counts_and_back_changes_nothing(self):
        d = self.review(settings=FakeSettings(cleanup_backup_days=14))
        d._ask_remove()
        self.assertEqual(d.stack.currentIndex(), un.CLEAN_CONFIRM)
        self.assertEqual(d.cc_title.text(), "Remove 3 items?")
        self.assertIn("1 folder", d.cc_counts.text())
        self.assertIn("1 shortcut", d.cc_counts.text())
        self.assertIn("1 registry key", d.cc_counts.text())
        self.assertIn("6.1 KB", d.cc_size.text())
        self.assertIn("14 days", d.cc_backup_note.label.text())
        self.assertFalse(d.cc_admin_note.isHidden(), "an HKLM key is ticked and Upstream is not elevated")
        self.assertTrue(d.cc_back.isDefault() and not d.cc_go.autoDefault())
        d.cc_back.click()
        self.assertEqual(d.stack.currentIndex(), un.REVIEW)
        self.assertEqual(self.fake.calls, [], "nothing runs until the confirmation is accepted")

    def test_no_admin_note_for_user_items_or_when_elevated(self):
        d = self.review([ITEMS[0], ITEMS[3]])
        d._ask_remove()
        self.assertTrue(d.cc_admin_note.isHidden())
        with mock.patch.object(un.cleanup, "is_admin", lambda: True):
            d = self.review()
            d._ask_remove()
            self.assertTrue(d.cc_admin_note.isHidden())

    def test_remove_runs_the_ticked_items_and_shows_the_summary(self):
        d = self.review()
        seen = []
        d.cleanup_finished.connect(lambda name, result: seen.append((name, result)))
        rows = self.rows(d)
        rows["C:\\Data\\FooMaybe"].setCheckState(0, Qt.Checked)
        rows["HKLM\\SOFTWARE\\Foo"].setCheckState(0, Qt.Unchecked)
        wanted = {id(i) for i in d.ticked()}
        d._ask_remove()
        d.cc_go.click()
        self.assertEqual(d.stack.currentIndex(), un.CLEANING)
        self.assertTrue(pump(lambda: seen))
        (items, name, kw), = self.fake.calls
        self.assertEqual({id(i) for i in items}, wanted)
        self.assertNotIn(ITEMS[-1], items, "a protected item is never passed on")
        self.assertEqual(name, "Foo App 1.0")
        self.assertIsNone(kw["restore_point"], "no settings given: the module's own setting decides")
        self.assertEqual(seen[0][0], "Foo App 1.0")
        self.assertEqual(seen[0][1].backup_id, "20260101-000000-abcdef")
        self.assertEqual(d.stack.currentIndex(), un.SUMMARY)
        self.assertEqual(d.sum_title.text(), i18n.tr("Leftovers removed"))
        self.assertIn("Removed 3 items", d.sum_text.text())
        self.assertIn("6.1 KB moved into the backup", d.sum_text.text())
        self.assertTrue(d.sum_extra.isHidden(), "no restore point was asked for")
        self.assertTrue(d.sum_problems.isHidden() and d.undo_btn.isVisibleTo(d))
        self.assertEqual(un.running(), 0)

    def test_settings_choose_the_restore_point(self):
        for value in (True, False):
            self.fake.calls.clear()
            d = self.review(settings=FakeSettings(cleanup_restore_point=value))
            seen = []
            d.cleanup_finished.connect(lambda *a: seen.append(a))
            d._ask_remove()
            d.cc_go.click()
            self.assertTrue(pump(lambda: seen))
            self.assertIs(self.fake.calls[0][2]["restore_point"], value)

    def test_failures_and_the_restore_point_note(self):
        self.fake.result = {"fail": True, "point": False}
        d = self.review()
        seen = []
        d.cleanup_finished.connect(lambda *a: seen.append(a))
        d._ask_remove()
        d.cc_go.click()
        self.assertTrue(pump(lambda: seen))
        self.assertIn("Windows did not create a restore point", d.sum_extra.text())
        self.assertEqual(d.sum_problems.topLevelItemCount(), 1)
        self.assertEqual(d.sum_problems.topLevelItem(0).text(1), cleanup.T_IN_USE)
        self.assertIn("1 item was not removed", d.sum_problems_title.text())
        self.assertEqual(d.sum_problems_title.isHidden(), False)

    def test_undo_restores_and_reports(self):
        d = self.review()
        done, undone = [], []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d.restored.connect(lambda name, result: undone.append((name, result)))
        d._ask_remove()
        d.cc_go.click()
        self.assertTrue(pump(lambda: done))
        d.undo_btn.click()
        self.assertFalse(d.undo_btn.isEnabled() or d.sum_done.isEnabled(), "buttons wait for the undo")
        self.assertTrue(pump(lambda: undone))
        self.assertEqual(self.fake.restores, ["20260101-000000-abcdef"])
        self.assertEqual(undone[0][0], "Foo App 1.0")
        self.assertEqual(d.sum_title.text(), i18n.tr("Cleanup undone"))
        self.assertIn("Restored 2 items", d.sum_text.text())
        self.assertEqual(d.sum_problems.topLevelItem(0).text(1), cleanup.R_EXISTS)
        self.assertFalse(d.undo_btn.isVisibleTo(d), "everything is back: nothing left to undo")
        self.assertTrue(d.sum_done.isEnabled())
        self.assertEqual(un.running(), 0)

    def test_undo_stays_available_when_something_was_skipped(self):
        self.fake.remaining = 1
        d = self.review()
        done, undone = [], []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d.restored.connect(lambda *a: undone.append(a))
        d._ask_remove()
        d.cc_go.click()
        pump(lambda: done)
        d.undo_btn.click()
        pump(lambda: undone)
        self.assertTrue(d.undo_btn.isVisibleTo(d))

    def test_stop_ends_the_cleanup_between_two_items(self):
        self.fake.gate.clear()
        d = self.review()
        done = []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d._ask_remove()
        d.cc_go.click()
        self.assertTrue(pump(lambda: self.fake.calls and d.cl_step.text() == "Removing Foo"))
        self.assertEqual(d.cl_step.text(), "Removing Foo")
        self.assertEqual(d.cl_bar.value(), 40)
        d.cl_stop.click()
        self.assertFalse(d.cl_stop.isEnabled())
        self.assertIn("Stopping", d.cl_step.text())
        self.fake.gate.set()
        self.assertTrue(pump(lambda: done))
        self.assertTrue(done[0][1].cancelled)
        self.assertEqual(d.sum_title.text(), i18n.tr("Cleanup stopped"))

    def test_the_dialog_cannot_be_closed_while_it_removes(self):
        self.fake.gate.clear()
        d = self.review()
        d.show()
        d._ask_remove()
        d.cc_go.click()
        pump(lambda: self.fake.calls)
        d.reject()
        self.assertTrue(d.isVisible(), "closing is held back until the worker is done")
        self.assertGreater(un.running(), 0, "the main window warns while a cleanup runs")
        self.fake.gate.set()
        pump(lambda: un.running() == 0)
        d.reject()
        self.assertFalse(d.isVisible())

    def test_a_cleanup_that_crashes_is_reported_not_raised(self):
        d = self.review()
        done = []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        with mock.patch.object(un.cleanup, "run", side_effect=RuntimeError("boom")):
            d._ask_remove()
            d.cc_go.click()
            self.assertTrue(pump(lambda: done))
        self.assertEqual(d.sum_title.text(), i18n.tr("Nothing was removed"))
        self.assertEqual(d.sum_problems.topLevelItem(0).text(1), "boom")
        self.assertFalse(d.undo_btn.isVisibleTo(d))

    def test_release_while_a_cleanup_runs_waits_for_it(self):
        self.fake.gate.clear()
        d = self.review()
        done = []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d._ask_remove()
        d.cc_go.click()
        pump(lambda: self.fake.calls)
        d.release()
        self.fake.gate.set()
        self.assertTrue(pump(lambda: done), "the result still reaches whoever listens")

    def test_backups_link_opens_the_backups_dialog(self):
        d = self.review()
        opened = []
        with mock.patch.object(un.BackupsDialog, "exec", lambda self_: opened.append(self_)), \
                mock.patch.object(cleanup, "backups", lambda: []):
            d.backups_btn.click()
        self.assertEqual(len(opened), 1)
        self.assertIsInstance(opened[0], un.BackupsDialog)

    SERVICE = item("service", "HKLM\\SYSTEM\\CurrentControlSet\\Services\\FooSvc")

    def test_a_ticked_service_is_named_in_the_confirmation(self):
        """Undo can't bring a service back, so the confirmation may not promise it can."""
        d = self.review([ITEMS[0], self.SERVICE])
        d._ask_remove()
        self.assertFalse(d.cc_service_note.isHidden())
        self.assertIn("reinstall the app", d.cc_service_note.label.text())
        d = self.review([ITEMS[0], ITEMS[1], self.SERVICE])
        self.rows(d)[self.SERVICE.location].setCheckState(0, Qt.Unchecked)
        d._ask_remove()
        self.assertTrue(d.cc_service_note.isHidden(), "no service is ticked")
        self.assertEqual(d.cc_service_note.label.text(), "")

    def test_the_backups_links_wait_while_a_cleanup_or_an_undo_runs(self):
        """A second restore or a delete of the same backup would race with the one under way."""
        self.fake.gate.clear()
        d = self.review()
        done, undone, opened = [], [], []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d.restored.connect(lambda *a: undone.append(a))
        d._ask_remove()
        d.cc_go.click()
        pump(lambda: self.fake.calls)
        self.assertFalse(d.backups_btn.isEnabled() or d.sum_backups_btn.isEnabled(), "the cleanup is running")
        self.fake.gate.set()
        self.assertTrue(pump(lambda: done))
        self.assertTrue(d.sum_backups_btn.isEnabled() and d.backups_btn.isEnabled())

        self.fake.restore_gate.clear()
        with mock.patch.object(un.BackupsDialog, "exec", lambda self_: opened.append(self_)):
            d.undo_btn.click()
            self.assertTrue(pump(lambda: self.fake.restores))
            self.assertFalse(d.sum_backups_btn.isEnabled() or d.backups_btn.isEnabled(), "the undo is running")
            d._open_backups()
            self.assertEqual(opened, [], "not even by a key press")
            self.fake.restore_gate.set()
            self.assertTrue(pump(lambda: undone))
            self.assertTrue(d.sum_backups_btn.isEnabled() and d.backups_btn.isEnabled())
            d._open_backups()
        self.assertEqual(len(opened), 1)

    def test_the_review_snapshot_reaches_the_cleanup(self):
        """The registry items are compared with what the review saw, so the scan thread's snapshot must get there."""
        d = self.single()
        with mock.patch.object(un.leftovers, "scan", lambda app_, others: list(ITEMS[:4])):
            d._busy = True
            d._want_scan = True
            un._RUNNING.add(d.uid)
            d._on_finished("removed", "{name} was removed", {"name": "Foo App"}, True)
            self.assertTrue(pump(lambda: d.items))
        self.assertEqual(d.baseline, {("regkey", "hkcu\\software\\foo"): "state of HKCU\\Software\\Foo"})
        done = []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d._ask_remove()
        d.cc_go.click()
        self.assertTrue(pump(lambda: done))
        self.assertEqual(self.fake.calls[0][2]["baseline"], d.baseline)


class SingleDialogStillWorks(Base):
    def test_uninstall_then_scan_then_review(self):
        a = make_app("Foo App")
        status = AppStatus(a)
        d = self.single()
        d.removed.connect(lambda uid: removed.append(uid))
        removed = []
        with mock.patch.object(un.leftovers, "scan", lambda app_, others: list(ITEMS[:2])):
            d._busy = True
            d._want_scan = True
            un._RUNNING.add(d.uid)
            d._on_finished("removed", "{name} was removed", {"name": a.name}, True)
            self.assertEqual(removed, [status.uid])
            self.assertTrue(pump(lambda: d.items))
        self.assertEqual(d.stack.currentIndex(), un.REVIEW)
        self.assertEqual(len(d.items), 2)
        self.assertEqual(un.running(), 0)

    def test_a_failed_uninstall_shows_the_result_page_without_a_scan(self):
        d = self.single()
        with mock.patch.object(un.leftovers, "scan", side_effect=AssertionError("no scan after a failure")):
            d._on_finished("failed", "The uninstaller exited with code {code}", {"code": 5}, False)
        self.assertEqual(d.stack.currentIndex(), un.RESULT)
        self.assertEqual(d.result_title.text(), i18n.tr("Couldn't remove it"))

    def test_scan_failure_message(self):
        d = self.single()
        d._on_scan_failed("disk error")
        self.assertIn("disk error", d.review_message.text())

    def test_uninstall_button_is_not_the_default(self):
        d = self.single()
        self.assertTrue(d.cancel_btn.isDefault())
        self.assertFalse(d.go_btn.autoDefault())

    def test_the_outcome_is_public_and_read_only(self):
        d = self.single()
        self.assertEqual(d.outcome, ("", "", {}))
        d._on_finished("failed", "The uninstaller exited with code {code}", {"code": 5}, False)
        self.assertEqual(d.outcome, ("failed", "The uninstaller exited with code {code}", {"code": 5}))
        self.assertEqual(d._outcome, d.outcome, "the private name keeps working")
        with self.assertRaises(AttributeError):
            d.outcome = ("removed", "", {})


class BatchDialog(Base):
    def setUp(self):
        super().setUp()
        self.apps = [make_app("Alpha"), make_app("Bravo"), make_app("Charlie"), make_app("Delta Runtime"), make_app("Echo")]
        self.statuses = [AppStatus(a) for a in self.apps]
        plans = {
            "Alpha": ("quiet", True), "Bravo": ("interactive", False), "Charlie": ("blocked", True),
            "Delta Runtime": ("quiet", True), "Echo": ("quiet", True),
        }

        def plan(app_, wid=None, prefer_silent=True):
            method, silent = plans[app_.name]
            if method == "blocked":
                return uninstaller.UninstallPlan(app_, "interactive", "", "", False, "{name} is marked as not removable.",
                                                 {"name": app_.name})
            notes = [("Other apps may need this. Removing it can stop them from working.", {})] if "Runtime" in app_.name else []
            return uninstaller.UninstallPlan(app_, method, "x.exe", "/S", silent and prefer_silent, notes=notes)

        self.enter(mock.patch.object(un.uninstaller, "plan", plan))
        self.order, self.outcomes, self.gate = [], {}, threading.Event()
        self.gate.set()

        def fake_run(plan_, progress):
            self.order.append(plan_.app.name)
            progress("Uninstalling (Windows may ask for permission)")
            self.gate.wait(5)
            return self.outcomes.get(plan_.app.name, ("removed", "{name} was removed", {"name": plan_.app.name}))

        self.enter(mock.patch.object(un.uninstaller, "run", fake_run))
        self.enter(mock.patch.object(un.uninstaller, "is_gone", lambda app_, wid=None: True))
        self.fake = FakeCleanup()
        self.enter(mock.patch.object(un.cleanup, "run", self.fake.run))
        self.enter(mock.patch.object(un.cleanup, "restore", self.fake.restore))

    def dialog(self, apps=None, **kw) -> un.BatchUninstallDialog:
        chosen = self.statuses if apps is None else apps
        return self.keep(un.BatchUninstallDialog(chosen, [s.app for s in self.statuses] + [make_app("Other")], **kw))

    def test_confirmation_lists_every_app_and_skips_the_blocked_ones(self):
        d = self.dialog()
        self.assertEqual(d.stack.currentIndex(), un.BATCH_CONFIRM)
        self.assertEqual(d.apps_tree.topLevelItemCount(), 5)
        texts = {d.apps_tree.topLevelItem(i).text(0).split()[0]: [d.apps_tree.topLevelItem(i).text(c) for c in (0, 3, 4)]
                 for i in range(5)}
        self.assertEqual(texts["Charlie"][1], "Can't be removed here")
        self.assertIn("not removable", texts["Charlie"][2])
        self.assertEqual(texts["Alpha"][1], "By its own uninstaller, quietly")
        self.assertEqual(texts["Bravo"][1], "In its own uninstall window")
        self.assertIn("Other apps may need this", texts["Delta"][2])
        self.assertEqual(d.bc_title.text(), "Uninstall 4 apps?")
        self.assertIn("1 app can't be removed here", d.bc_sub.text())
        self.assertEqual(d.go_btn.text(), "Uninstall 4 apps")
        self.assertFalse(d.ack_box.isHidden())
        self.assertFalse(d.go_btn.isEnabled(), "a shared runtime needs the extra yes")
        d.ack_box.setChecked(True)
        self.assertTrue(d.go_btn.isEnabled())
        self.assertTrue(d.cancel_btn.isDefault() and not d.go_btn.autoDefault())

    def test_nothing_runnable_and_waiting_for_other_work(self):
        d = self.dialog([self.statuses[2]])
        self.assertFalse(d.go_btn.isEnabled())
        d = self.dialog([self.statuses[0]], updates_running=True)
        self.assertFalse(d.wait_notice.isHidden())
        self.assertFalse(d.go_btn.isEnabled())
        d = self.dialog([self.statuses[0]])
        self.assertTrue(d.go_btn.isEnabled())
        self.assertTrue(d.ack_box.isHidden())

    def test_quiet_choice_replans_every_app(self):
        d = self.dialog([self.statuses[0]])
        d.quiet_box.setChecked(False)
        self.assertEqual(d.apps_tree.topLevelItem(0).text(3), "In its own uninstall window")

    def run_all(self, d, scan=True):
        removed, finished = [], []
        d.removed.connect(removed.append)
        d.leftover_box.setChecked(scan)
        d.ack_box.setChecked(True)
        d.go_btn.click()
        return removed

    def test_apps_are_removed_one_after_another_own_window_first(self):
        d = self.dialog()
        removed = self.run_all(d, scan=False)
        self.assertEqual(d.stack.currentIndex(), un.BATCH_RUNNING)
        self.assertGreater(un.running(), 0)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        self.assertEqual(self.order, ["Bravo", "Alpha", "Delta Runtime", "Echo"], "own window first, then the quiet ones")
        self.assertEqual(sorted(removed), sorted(s.uid for s in self.statuses if s.app.name != "Charlie"))
        rows = {d.state_tree.topLevelItem(i).text(0): d.state_tree.topLevelItem(i).text(3) for i in range(5)}
        self.assertEqual(rows["Alpha"], i18n.tr("Removed"))
        self.assertTrue(rows["Charlie"].startswith("Left alone"))
        self.assertEqual(d.run_title.text(), "Removed 4 of 5 apps")
        self.assertEqual(d.b_bar.value(), 4)
        self.assertFalse(d.b_stop.isVisibleTo(d))
        self.assertEqual(un.running(), 0)

    def test_stop_after_the_current_one(self):
        self.gate.clear()
        d = self.dialog()
        removed = self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: self.order))
        d.b_stop.click()
        self.assertFalse(d.b_stop.isEnabled())
        self.gate.set()
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        self.assertEqual(self.order, ["Bravo"], "the others were never started")
        self.assertEqual(len(removed), 1)
        states = [d.state_tree.topLevelItem(i).text(3) for i in range(5)]
        self.assertEqual(sum(s.startswith("Left alone") for s in states), 4)

    def finish(self, d):
        self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        return d.outcome

    def test_the_outcome_of_the_batch(self):
        d = self.dialog([self.statuses[0], self.statuses[4]])
        self.assertEqual(d.outcome, ("", "", {}), "not run yet")
        self.assertEqual(self.finish(d), ("removed", "", {}))
        self.assertEqual(d._outcome, d.outcome)
        with self.assertRaises(AttributeError):
            d.outcome = ("failed", "", {})

        self.outcomes = {"Alpha": ("failed", "The uninstaller exited with code {code}", {"code": 1603})}
        failed = self.finish(self.dialog([self.statuses[0], self.statuses[4]]))
        self.assertEqual(failed, ("failed", "The uninstaller exited with code {code}", {"code": 1603}),
                         "one app stayed: the batch says how it ended")
        self.outcomes = {}
        self.assertEqual(self.finish(self.dialog([self.statuses[0], self.statuses[2]])), ("removed", "", {}),
                         "an app that can't be removed here was never tried")

    def test_a_stopped_batch_is_not_reported_as_removed(self):
        self.gate.clear()
        d = self.dialog([self.statuses[0], self.statuses[4]])
        self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: self.order))
        d.b_stop.click()
        self.gate.set()
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        self.assertEqual(d.outcome, ("skipped", "Skipped because the list was stopped", {}))

    def test_failures_and_a_restart_are_reported_per_app(self):
        self.outcomes = {"Alpha": ("failed", "The uninstaller exited with code {code}", {"code": 1603}),
                         "Echo": ("reboot_required", "Restart Windows to finish removing {name}", {"name": "Echo"})}
        d = self.dialog([self.statuses[0], self.statuses[4]])
        removed = self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        rows = {d.state_tree.topLevelItem(i).text(0): d.state_tree.topLevelItem(i).text(3) for i in range(2)}
        self.assertIn("1603", rows["Alpha"])
        self.assertTrue(rows["Echo"].startswith(i18n.tr("Restart needed")))
        self.assertEqual(removed, [self.statuses[4].uid], "Windows no longer lists Echo, so it counts as removed")
        self.assertEqual(d.run_title.text(), "Removed 1 of 2 apps")

    def test_one_combined_review_with_the_app_of_each_item(self):
        scans = []

        def scan(app_, others):
            scans.append((app_.name, [o.name for o in others]))
            shared = item("folder", "C:\\Shared\\Thing", leftovers.LIKELY, 100)
            return [item("folder", f"C:\\Data\\{app_.name}", leftovers.CERTAIN, 10), shared,
                    item("regkey", f"HKCU\\Software\\{app_.name}", leftovers.PROTECTED)] \
                + ([item("folder", "C:\\Shared\\Thing", leftovers.CERTAIN, 100)] if app_.name == "Echo" else [])

        d = self.dialog([self.statuses[0], self.statuses[1], self.statuses[4]])
        with mock.patch.object(un.leftovers, "scan", scan):
            self.run_all(d, scan=True)
            self.assertTrue(pump(lambda: d.items))
        self.assertEqual(sorted(a for a, _ in scans), ["Alpha", "Bravo", "Echo"], "one scan per removed app")
        self.assertTrue(all("Alpha" not in others and "Bravo" not in others and "Echo" not in others and "Other" in others
                            for _, others in scans), "the apps that are gone are not 'other installed apps'")
        self.assertEqual(d.stack.currentIndex(), d.p_review)
        shared = [i for i in d.items if i.location == "C:\\Shared\\Thing"]
        self.assertEqual(len(shared), 1, "the same folder found for three apps is listed once")
        self.assertEqual(shared[0].confidence, leftovers.CERTAIN, "with the strongest confidence")
        owner = {i.location: d.owners[n] for n, i in enumerate(d.items)}
        self.assertEqual(owner["C:\\Shared\\Thing"], "Alpha, Bravo, Echo")
        self.assertEqual(owner["C:\\Data\\Bravo"], "Bravo")
        self.assertFalse(d.tree.isColumnHidden(3))
        row = self.rows(d)["C:\\Data\\Bravo"]
        self.assertEqual(row.text(3), "Bravo")
        self.assertIn("Alpha, Bravo, Echo", d.review_sub.text() + d.export_text())
        self.assertIn("From Bravo", d.export_text())
        self.assertEqual(un.running(), 0)

    def test_cleanup_from_the_combined_review_uses_a_batch_label(self):
        d = self.dialog([self.statuses[0], self.statuses[1], self.statuses[3], self.statuses[4]])
        with mock.patch.object(un.leftovers, "scan", lambda app_, others: [item("folder", f"C:\\Data\\{app_.name}", size=5)]):
            self.run_all(d, scan=True)
            self.assertTrue(pump(lambda: d.items))
        done = []
        d.cleanup_finished.connect(lambda *a: done.append(a))
        d._ask_remove()
        d.cc_go.click()
        self.assertTrue(pump(lambda: done))
        self.assertEqual(self.fake.calls[0][1], "Alpha, Bravo, Delta Runtime +1")
        self.assertEqual(len(self.fake.calls[0][0]), 4)
        self.assertEqual(d.stack.currentIndex(), d.p_summary)
        undone = []
        d.restored.connect(lambda *a: undone.append(a))
        d.undo_btn.click()
        self.assertTrue(pump(lambda: undone))
        self.assertEqual(undone[0][0], "Alpha, Bravo, Delta Runtime +1")

    def test_no_scan_when_nothing_was_removed_or_the_option_is_off(self):
        self.outcomes = {"Alpha": ("failed", "The uninstaller exited with code {code}", {"code": 1}), }
        d = self.dialog([self.statuses[0]])
        with mock.patch.object(un.leftovers, "scan", side_effect=AssertionError("nothing was removed")):
            self.run_all(d, scan=True)
            self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        self.assertEqual(d.stack.currentIndex(), un.BATCH_RUNNING)

    def test_closing_while_running_stops_the_rest_of_the_list(self):
        self.gate.clear()
        d = self.dialog()
        self.run_all(d, scan=False)
        pump(lambda: self.order)
        with mock.patch.object(un.QMessageBox, "exec", lambda self_: None), \
                mock.patch.object(un.QMessageBox, "clickedButton", lambda self_: next(
                    b for b in self_.buttons() if b.text() == i18n.tr("Close window"))):
            d.reject()
        d.release()
        self.gate.set()
        self.assertTrue(pump(lambda: un.running() == 0))
        self.assertEqual(self.order, ["Bravo"])

    # -- sizes and sorting -------------------------------------------------
    SIZES = [(51200, "1.0"), (2048, "10.2"), (900, "9.1"), (0, "2.0.1"), (1048576, "3.0")]   # Alpha ... Echo

    def sized(self):
        """Alpha 50 MB, Bravo 2 MB, Charlie (blocked) 900 KB, Delta Runtime no size, Echo 1 GB."""
        for status, (size_kb, version) in zip(self.statuses, self.SIZES):
            status.app = dataclasses.replace(status.app, size_kb=size_kb, version=version)

    @staticmethod
    def names(tree) -> list[str]:
        return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]

    @staticmethod
    def click(tree, column):
        tree.header().sectionClicked.emit(column)

    def test_the_confirmation_shows_each_size_and_the_total_to_free(self):
        self.sized()
        d = self.dialog()
        self.assertEqual([d.apps_tree.headerItem().text(c) for c in range(5)],
                         ["Application", "Version", "Size", "How it will be removed", "Heads-up"])
        rows = {d.apps_tree.topLevelItem(i).text(0): d.apps_tree.topLevelItem(i) for i in range(5)}
        self.assertEqual([rows[n].text(2) for n in ("Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo")],
                         ["50.0 MB", "2.0 MB", "900.0 KB", "", "1.0 GB"])
        self.assertEqual(rows["Bravo"].text(1), "10.2")
        self.assertEqual(d.bc_title.text(), "Uninstall 4 apps?")
        self.assertEqual(d.bc_total.text(), "4 apps, about 1.1 GB to free · "
                         "1 app has no reported size and isn't counted.", "the blocked app is not counted")
        self.assertFalse(d.bc_total.isHidden())

    def test_the_total_of_one_app_and_of_none_with_a_size(self):
        self.sized()
        self.assertEqual(self.dialog([self.statuses[0]]).bc_total.text(), "1 app, about 50.0 MB to free")
        d = self.dialog([self.statuses[3]])
        self.assertTrue(d.bc_total.isHidden(), "no size is known, so there is no total to show")
        self.assertEqual(d.apps_tree.topLevelItem(0).text(2), "")

    def test_the_list_keeps_its_order_until_a_header_is_clicked(self):
        self.sized()
        d = self.dialog()
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo"])
        self.assertFalse(d.apps_tree.header().isSortIndicatorShown())
        d.quiet_box.setChecked(False)
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo"])

    def test_size_sorts_biggest_first_then_reverses_with_unknown_sizes_always_last(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 2)
        self.assertEqual(self.names(d.apps_tree), ["Echo", "Alpha", "Bravo", "Charlie", "Delta Runtime"])
        header = d.apps_tree.header()
        self.assertEqual((header.isSortIndicatorShown(), header.sortIndicatorSection(), header.sortIndicatorOrder()),
                         (True, 2, Qt.DescendingOrder))
        self.click(d.apps_tree, 2)
        self.assertEqual(self.names(d.apps_tree), ["Charlie", "Bravo", "Alpha", "Echo", "Delta Runtime"])
        self.assertEqual(header.sortIndicatorOrder(), Qt.AscendingOrder)

    def test_name_and_version_sort_and_versions_compare_as_numbers(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 0)
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo"])
        self.click(d.apps_tree, 0)
        self.assertEqual(self.names(d.apps_tree), ["Echo", "Delta Runtime", "Charlie", "Bravo", "Alpha"])
        self.click(d.apps_tree, 1)
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Delta Runtime", "Echo", "Charlie", "Bravo"],
                         "1.0, 2.0.1, 3.0, 9.1, 10.2")
        self.click(d.apps_tree, 1)
        self.assertEqual(self.names(d.apps_tree), ["Bravo", "Charlie", "Echo", "Delta Runtime", "Alpha"])

    def test_a_sort_by_box_sits_above_the_apps_and_sorts_like_the_headers(self):
        self.sized()
        d = self.dialog()
        bar = d.apps_sort_bar
        layout = d.stack.widget(un.BATCH_CONFIRM).layout()
        self.assertLess(layout.indexOf(bar), layout.indexOf(d.apps_tree))
        self.assertEqual([bar.combo.itemText(i) for i in range(bar.combo.count())],
                         [i18n.tr("Name"), i18n.tr("Version"), i18n.tr("Size")])
        self.assertEqual(bar.label.text(), i18n.tr("Sort by"))
        self.assertEqual((bar.combo.currentIndex(), bar.reverse.isEnabled()), (-1, False))
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo"], "as given")
        pick(bar, "size")
        self.assertEqual(self.names(d.apps_tree), ["Echo", "Alpha", "Bravo", "Charlie", "Delta Runtime"])
        self.assertEqual((d.apps_tree.header().sortIndicatorSection(), d.apps_tree.header().sortIndicatorOrder()),
                         (2, Qt.DescendingOrder))
        bar.reverse.setChecked(True)
        self.assertEqual(self.names(d.apps_tree), ["Charlie", "Bravo", "Alpha", "Echo", "Delta Runtime"],
                         "an unknown size stays last")
        pick(bar, "version")
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Delta Runtime", "Echo", "Charlie", "Bravo"])
        pick(bar, "name", True)
        self.assertEqual(self.names(d.apps_tree), ["Echo", "Delta Runtime", "Charlie", "Bravo", "Alpha"])

    def test_the_sort_by_boxes_follow_the_headers_and_each_other(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 2)
        self.click(d.apps_tree, 2)
        for bar in (d.apps_sort_bar, d.state_sort_bar):
            self.assertEqual((bar.combo.currentData(), bar.reverse.isChecked()), ("size", True))
        self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        pick(d.state_sort_bar, "name")
        self.assertEqual(self.names(d.state_tree), ["Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo"])
        self.assertEqual(self.names(d.apps_tree), self.names(d.state_tree), "one sort for both lists")
        self.assertEqual(d.apps_sort_bar.combo.currentData(), "name")

    def test_the_sort_by_box_works_from_the_keyboard(self):
        self.sized()
        d = self.dialog()
        bar = d.apps_sort_bar
        for widget in (bar.combo, bar.reverse):
            self.assertNotEqual(widget.focusPolicy(), Qt.NoFocus)
        QTest.keyClick(bar.combo, Qt.Key_Down)
        QTest.keyClick(bar.combo, Qt.Key_Down)
        self.assertEqual(bar.combo.currentData(), "version")
        self.assertEqual(self.names(d.apps_tree), ["Alpha", "Delta Runtime", "Echo", "Charlie", "Bravo"])
        QTest.keyClick(bar.reverse, Qt.Key_Space)
        self.assertEqual(self.names(d.apps_tree), ["Bravo", "Charlie", "Echo", "Delta Runtime", "Alpha"])

    def test_the_columns_that_are_not_about_the_app_do_not_sort(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 2)
        before = self.names(d.apps_tree)
        self.click(d.apps_tree, 3)
        self.click(d.apps_tree, 4)
        self.assertEqual(self.names(d.apps_tree), before)

    def test_the_sort_survives_the_list_being_rebuilt(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 2)
        d.quiet_box.setChecked(False)
        d._refresh_confirm()
        self.assertEqual(self.names(d.apps_tree)[0], "Echo")

    def test_sorting_does_not_change_the_order_the_apps_are_removed_in(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 0)
        self.click(d.apps_tree, 0)
        self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        self.assertEqual(self.order, ["Bravo", "Alpha", "Delta Runtime", "Echo"], "own window first, then as given")

    def test_the_result_rows_show_sizes_follow_the_sort_and_sort_again(self):
        self.sized()
        d = self.dialog()
        self.click(d.apps_tree, 2)
        self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        tree = d.state_tree
        self.assertEqual([tree.headerItem().text(c) for c in range(4)], ["Application", "Version", "Size", "Outcome"])
        self.assertEqual(self.names(tree), ["Echo", "Alpha", "Bravo", "Charlie", "Delta Runtime"])
        self.assertEqual(tree.topLevelItem(0).text(2), "1.0 GB")
        self.assertEqual(tree.topLevelItem(4).text(2), "")
        self.assertTrue(tree.header().isSortIndicatorShown())
        self.click(tree, 0)
        self.assertEqual(self.names(tree), ["Alpha", "Bravo", "Charlie", "Delta Runtime", "Echo"])
        outcome = {tree.topLevelItem(i).text(0): tree.topLevelItem(i).text(3) for i in range(5)}
        self.assertEqual(outcome["Alpha"], i18n.tr("Removed"))
        self.assertTrue(outcome["Charlie"].startswith("Left alone"))
        self.assertEqual(self.names(d.apps_tree), self.names(tree), "both lists show the same order")

    def test_the_single_dialog_names_the_size_next_to_the_version(self):
        def meta_of(app_):
            plan = uninstaller.UninstallPlan(app_, "quiet", "unins000.exe", "/S", True)
            with mock.patch.object(un.uninstaller, "plan", lambda a_, wid=None, prefer_silent=True: plan):
                d = self.keep(un.UninstallDialog(AppStatus(app_), [app_]))
            return [w.full_text for w in d.findChildren(un.ElidedLabel)]

        self.assertTrue(any("45.0 MB" in text for text in meta_of(dataclasses.replace(make_app("Foo App"), size_kb=46080))))
        self.assertFalse(any("MB" in text or "KB" in text for text in meta_of(make_app("Foo App"))))

    def test_dialog_titles(self):
        self.assertEqual(self.dialog().windowTitle(), "Uninstall 5 apps")
        self.assertEqual(self.dialog([self.statuses[0]]).windowTitle(), "Uninstall 1 app")


class BackupsList(Base):
    def setUp(self):
        super().setUp()
        self.entries = [cleanup.Backup("20260102-101500-aaaaaa", "Foo App", time.mktime((2026, 1, 2, 10, 15, 0, 0, 0, -1)), 3, 2048),
                        cleanup.Backup("20251201-080000-bbbbbb", "Bar", time.mktime((2025, 12, 1, 8, 0, 0, 0, 0, -1)), 12, 5 * 1024 ** 2)]
        self.deleted, self.restored_ids = [], []
        self.remaining = 0
        self.can_delete = True
        self.enter(mock.patch.object(un.cleanup, "backups", lambda: list(self.entries)))

        def delete(backup_id):
            self.deleted.append(backup_id)
            if self.can_delete:
                self.entries = [b for b in self.entries if b.id != backup_id]
            return self.can_delete

        def restore(backup_id, progress=None):
            self.restored_ids.append(backup_id)
            self.entries = [b for b in self.entries if b.id != backup_id]
            return cleanup.RestoreResult(restored=3, failed=[("C:\\Data\\Foo", cleanup.R_EXISTS, {})])

        self.enter(mock.patch.object(un.cleanup, "delete_backup", delete))
        self.enter(mock.patch.object(un.cleanup, "restore", restore))

    def test_rows_show_date_app_items_and_size(self):
        d = self.keep(un.BackupsDialog(None, days=30))
        self.assertEqual(d.tree.topLevelItemCount(), 2)
        row = d.tree.topLevelItem(0)
        self.assertEqual([row.text(c) for c in (1, 2, 3)], ["Foo App", "3", "2.0 KB"])
        self.assertIn("2026-01-02 10:15", row.text(0))
        self.assertEqual(d.tree.topLevelItem(1).text(3), "5.0 MB")
        self.assertTrue(d.restore_btn.isEnabled() and d.delete_btn.isEnabled())
        self.assertEqual(d.selected().id, "20260102-101500-aaaaaa")
        self.assertIn("30 days", d.findChildren(un.QLabel)[1].text())

    def test_empty_state(self):
        self.entries = []
        d = self.keep(un.BackupsDialog(None))
        self.assertEqual(d.stack.currentIndex(), 1)
        self.assertFalse(d.restore_btn.isEnabled() or d.delete_btn.isEnabled())

    def test_restore_reports_and_refreshes(self):
        d = self.keep(un.BackupsDialog(None))
        got = []
        d.restored.connect(lambda name, result: got.append((name, result)))
        d.tree.setCurrentItem(d.tree.topLevelItem(1))
        d.restore_btn.click()
        self.assertFalse(d.restore_btn.isEnabled(), "buttons wait while it restores")
        self.assertTrue(pump(lambda: got))
        self.assertEqual(self.restored_ids, ["20251201-080000-bbbbbb"])
        self.assertEqual(got[0][0], "Bar")
        self.assertEqual(got[0][1].restored, 3)
        self.assertEqual(d.tree.topLevelItemCount(), 1)
        self.assertIn("Restored 3 items", d.status.text())
        self.assertFalse(d.problems.isHidden())
        self.assertEqual(d.problems.topLevelItem(0).text(1), cleanup.R_EXISTS)
        self.assertEqual(un.running(), 0)

    def test_the_question_names_the_backup_and_follows_the_row(self):
        d = self.keep(un.BackupsDialog(None))
        d.delete_btn.click()
        self.assertIn("Foo App", d.question.text())
        self.assertIn(un.when_text(self.entries[0].ts), d.question.text())
        d.tree.setCurrentItem(d.tree.topLevelItem(1))
        self.assertTrue(d.confirm.isHidden(), "another row is picked: the question was about the first one")
        d.delete_btn.click()
        self.assertIn("Bar", d.question.text())
        self.assertNotIn("Foo App", d.question.text())

    def test_a_confirmation_never_deletes_the_row_picked_afterwards(self):
        """Delete on A, click B, press 'Delete for good': B used to go, irreversibly."""
        d = self.keep(un.BackupsDialog(None))
        d.delete_btn.click()
        d.tree.setCurrentItem(d.tree.topLevelItem(1))
        d.confirm_yes.click()      # the bar is hidden, but a press that still arrives must not reach B
        self.assertEqual(self.deleted, [])
        self.assertEqual(d.tree.topLevelItemCount(), 2)
        d.delete_btn.click()       # asked about B this time
        d.confirm_yes.click()
        self.assertEqual(self.deleted, ["20251201-080000-bbbbbb"])

    def test_the_confirmed_backup_is_the_one_asked_about(self):
        d = self.keep(un.BackupsDialog(None))
        d.delete_btn.click()
        d.tree.blockSignals(True)      # a selection change that reaches the dialog late or not at all
        d.tree.setCurrentItem(d.tree.topLevelItem(1))
        d.tree.blockSignals(False)
        d.confirm_yes.click()
        self.assertEqual(self.deleted, ["20260102-101500-aaaaaa"])

    def test_a_delete_that_failed_is_not_reported_as_done(self):
        self.can_delete = False
        d = self.keep(un.BackupsDialog(None))
        d.delete_btn.click()
        d.confirm_yes.click()
        self.assertEqual(self.deleted, ["20260102-101500-aaaaaa"])
        self.assertNotIn("Backup deleted", d.status.text())
        self.assertIn("Couldn't delete this backup", d.status.text())
        self.assertEqual(d.status.property("tone"), "danger")
        self.assertEqual(d.tree.topLevelItemCount(), 2, "it is still there")
        self.can_delete = True
        d.delete_btn.click()
        d.confirm_yes.click()
        self.assertEqual(d.status.text(), "Backup deleted.")
        self.assertEqual(d.status.property("tone"), "accent")
        self.assertEqual(d.tree.topLevelItemCount(), 1)

    def test_the_new_texts_have_a_persian_entry(self):
        for template in ("Reverse order", "Delete the backup of {app} from {date} for good? Then it can no longer be restored.",
                         "Couldn't delete this backup. A cleanup or an undo may be running, or it was made while Upstream ran as administrator.",
                         "Undo can't bring a removed service back. To get one back, reinstall the app."):
            self.assertIn(template, i18n.FA)

    def test_delete_asks_first(self):
        d = self.keep(un.BackupsDialog(None))
        d.delete_btn.click()
        self.assertFalse(d.confirm.isHidden())
        self.assertEqual(self.deleted, [])
        d.confirm_no.click()
        self.assertTrue(d.confirm.isHidden() and self.deleted == [])
        d.delete_btn.click()
        d.confirm_yes.click()
        self.assertEqual(self.deleted, ["20260102-101500-aaaaaa"])
        self.assertEqual(d.tree.topLevelItemCount(), 1)
        self.assertTrue(d.confirm.isHidden())


class LanguageAndTheme(Base):
    def test_persian_rtl_review_and_batch(self):
        self.addCleanup(apply_language, app, "en")
        self.addCleanup(theme.apply_theme, app, "paper")
        apply_language(app, "fa")
        theme.apply_theme(app, "midnight")
        d = self.review(settings=FakeSettings(cleanup_backup_days=30))
        self.assertEqual(d.layoutDirection(), Qt.RightToLeft)
        self.assertEqual(d.remove_btn.text(), i18n.tr("Remove selected…"))
        self.assertNotEqual(d.remove_btn.text(), "Remove selected…")
        self.assertTrue(any("\u0600" <= ch <= "\u06ff" for ch in d.remove_btn.text()))
        row = self.rows(d)["C:\\Data\\Foo"]
        self.assertTrue(row.text(0).startswith(un._LRI), "paths stay left-to-right inside the RTL window")
        d._ask_remove()
        self.assertNotIn("Remove", d.cc_title.text())
        self.assertIn("\u06f3", d.cc_title.text(), "counts use Persian digits")
        b = self.keep(un.BatchUninstallDialog([AppStatus(make_app("Alpha"))], [make_app("Alpha")]))
        self.assertEqual(b.layoutDirection(), Qt.RightToLeft)
        self.assertNotEqual(b.go_btn.text(), "Uninstall 1 app")
        with mock.patch.object(un.cleanup, "backups", lambda: [cleanup.Backup("20260102-101500-aaaaaa", "Foo", time.time(), 3, 100)]):
            k = self.keep(un.BackupsDialog(None, days=30))
        self.assertEqual(k.layoutDirection(), Qt.RightToLeft)
        self.assertIn("\u06f3", k.tree.topLevelItem(0).text(2))

    def test_persian_sizes_totals_and_headers(self):
        self.addCleanup(apply_language, app, "en")
        apply_language(app, "fa")
        status = AppStatus(dataclasses.replace(make_app("Alpha"), size_kb=46080, version="3.0"))
        other = AppStatus(dataclasses.replace(make_app("Bravo"), size_kb=0))
        with mock.patch.object(un.uninstaller, "plan", lambda app_, wid=None, prefer_silent=True:
                               uninstaller.UninstallPlan(app_, "quiet", "x.exe", "/S", True)):
            b = self.keep(un.BatchUninstallDialog([status, other], [status.app, other.app]))
        self.assertEqual(b.apps_tree.headerItem().text(2), i18n.tr("Size"))
        self.assertTrue(any("\u0600" <= ch <= "\u06ff" for ch in b.apps_tree.headerItem().text(2)))
        size = b.apps_tree.topLevelItem(0).text(2)
        self.assertIn("\u06f4\u06f5", size, "45.0 MB with Persian digits")
        self.assertIn("MB", size)
        self.assertTrue(size.startswith(un._LRI), "the size stays in one piece inside the RTL window")
        self.assertIn("\u06f4\u06f5", b.bc_total.text())
        self.assertNotIn("to free", b.bc_total.text())
        self.assertEqual(b.apps_tree.headerItem().toolTip(2), i18n.tr("Click to sort. Click again to reverse the order."))
        self.assertNotEqual(b.apps_tree.headerItem().toolTip(2), "Click to sort. Click again to reverse the order.")

    def test_every_theme_builds_the_tree_indicator(self):
        self.addCleanup(theme.apply_theme, app, "paper")
        for key in theme.THEME_ORDER:
            theme.apply_theme(app, key)
            style = un._indicator_style()
            self.assertIn(theme.current()["accent"], style)
            d = self.review()
            self.assertIn("indicator", d.tree.styleSheet())

    def test_every_template_the_dialogs_produce_is_translated(self):
        for template in (*cleanup.REASONS, *un.KIND_LABELS.values(), *(t for t, _ in un.RESULT_TITLES.values())):
            self.assertIn(template, i18n.FA, template)
        for one, many in un.KIND_COUNTS.values():
            self.assertIn(one, i18n.FA)
            self.assertIn(many, i18n.FA)


if __name__ == "__main__":
    unittest.main()
