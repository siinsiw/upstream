"""The uninstall dialogs offscreen: the ticked review, the confirmation, the cleanup and its undo, the batch dialog
and the backups dialog. Uninstallers, the leftover scan and the cleanup itself are fakes: nothing is removed."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, Qt
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


class FakeCleanup:
    """Stands in for cleanup.run and cleanup.restore; the worker thread can be held until the test says so."""

    def __init__(self, **result):
        self.calls, self.restores = [], []
        self.result = result
        self.gate = threading.Event()
        self.gate.set()
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
        self.assertTrue(pump(lambda: self.fake.calls and d.cl_step.text()))
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
        texts = {d.apps_tree.topLevelItem(i).text(0).split()[0]: [d.apps_tree.topLevelItem(i).text(c) for c in range(3)]
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
        self.assertEqual(d.apps_tree.topLevelItem(0).text(1), "In its own uninstall window")

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
        rows = {d.state_tree.topLevelItem(i).text(0): d.state_tree.topLevelItem(i).text(1) for i in range(5)}
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
        states = [d.state_tree.topLevelItem(i).text(1) for i in range(5)]
        self.assertEqual(sum(s.startswith("Left alone") for s in states), 4)

    def test_failures_and_a_restart_are_reported_per_app(self):
        self.outcomes = {"Alpha": ("failed", "The uninstaller exited with code {code}", {"code": 1603}),
                         "Echo": ("reboot_required", "Restart Windows to finish removing {name}", {"name": "Echo"})}
        d = self.dialog([self.statuses[0], self.statuses[4]])
        removed = self.run_all(d, scan=False)
        self.assertTrue(pump(lambda: d.b_done.isVisibleTo(d)))
        rows = {d.state_tree.topLevelItem(i).text(0): d.state_tree.topLevelItem(i).text(1) for i in range(2)}
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
        self.enter(mock.patch.object(un.cleanup, "backups", lambda: list(self.entries)))

        def delete(backup_id):
            self.deleted.append(backup_id)
            self.entries = [b for b in self.entries if b.id != backup_id]

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
