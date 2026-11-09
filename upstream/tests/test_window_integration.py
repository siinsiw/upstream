"""The main window with every feature wired in, offscreen and against fakes: tabs, the library factory, the row
menu, pins and rollback, history, background hooks, export, the administrator banner, instant start, Discover,
the Updates sort and download size, and the settings sections. Nothing here starts a scan, an install, a tray icon,
an elevation or an autostart write, and nothing touches the real settings or data folder (temp folders and fakes)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gc
import json
import logging
import re
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QCloseEvent, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from upstream import app as app_module, elevation, export, history, i18n, pins, scanner as scan_store, winget
from upstream.badges import Badge
from upstream.cleanup import CleanupResult, RestoreResult
from upstream.context import CancelToken
from upstream.discover import Package
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update
from upstream.ui import settings as settings_ui, theme, window as window_module
from upstream.ui.locale import apply_language
from upstream.ui.widgets import AppRow
from upstream.ui.window import MainWindow, UiLogHandler, older_versions

DEFAULTS = {"language": "en", "theme": "system", "use_gh_cli_token": True, "include_prereleases": False,
            "publisher_policy": "ask", "close_apps_policy": "ask", "metered_policy": "ask", "battery_policy": "ask",
            "proxy": {"mode": "system", "url": ""}, "install_timeout_min": 30}
MB = 1024 * 1024


class FakeSettings:
    """The part of config.Settings the window and the dialog use, in memory."""

    def __init__(self, **values):
        self.values = dict(DEFAULTS)
        self.values.update(values)
        self.ignored: set[str] = set()
        self.skipped: dict[str, str] = {}
        self.saves = 0
        self.fail: OSError | None = None

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        if self.fail:
            raise self.fail
        self.saves += 1

    def is_ignored(self, ident):
        return ident in self.ignored

    def set_ignored(self, ident, value):
        self.ignored.add(ident) if value else self.ignored.discard(ident)

    def skipped_version(self, ident):
        return self.skipped.get(ident)

    def skip_version(self, ident, version):
        self.skipped.pop(ident, None) if not version else self.skipped.__setitem__(ident, version)

    def override(self, ident):
        return {}

    def set_override(self, ident, value):
        pass


class FakeBox:
    """QMessageBox for the window: remembers what it was asked and 'clicks' button number `pick`."""
    AcceptRole, RejectRole = 0, 1
    Question, Warning, Information, Ok = 1, 2, 3, 4
    log: list = []
    pick = 0
    tick: bool | None = None        # what the check box (if the window adds one) shows when the box is "clicked"

    def __init__(self, parent=None):
        self.buttons = []
        self.text = self.detail = ""
        self.check_box = None
        FakeBox.log.append(self)

    def setCheckBox(self, box):
        self.check_box = box

    def setIcon(self, icon):
        self.icon = icon

    def setWindowTitle(self, text):
        pass

    def setText(self, text):
        self.text = text

    def setInformativeText(self, text):
        self.detail = text

    def setDefaultButton(self, button):
        pass

    def addButton(self, *args):
        button = object()
        self.buttons.append((button, args))
        return button

    def exec(self):
        if self.check_box is not None and FakeBox.tick is not None:
            self.check_box.setChecked(FakeBox.tick)
        return 0

    def clickedButton(self):
        return self.buttons[min(FakeBox.pick, len(self.buttons) - 1)][0]


class FakeUpdater:
    """Stands in for updater.Updater. Scripts per app name: quick (default), polite (runs until cancelled), fail,
    declined (the user said no to a question), ask (tries to ask the user something)."""

    def __init__(self):
        self.script: dict[str, str] = {}
        self.started: list[str] = []
        self.modes: dict[str, tuple] = {}
        self.asked: dict[str, bool] = {}
        self.release = threading.Event()

    def run(self, status, progress, ctx):
        name = status.app.name
        self.started.append(name)
        self.modes[name] = (status.update.winget_mode, list(status.fallbacks), ctx.auto, ctx.bulk)
        progress("Downloading", 10)
        mode = self.script.get(name, "quick")
        if mode == "polite":
            while not self.release.wait(0.02):
                ctx.cancel.check()
        elif mode == "fail":
            return False, "{error}", {"error": "boom"}, status.app.version
        elif mode == "declined":
            return False, "Update cancelled: {name} is still running.", {"name": name}, status.app.version
        elif mode == "ask":
            self.asked[name] = ctx.ask("{name} is running. Close it to update?", name=name)
        return True, "Installed {version}", {"version": status.update.version}, status.update.version


def make_status(name, version="1.0", new="2.0", size=0, publisher="Demo", scope="machine", source="github",
                winget_id=None, **update):
    app = InstalledApp(reg_key=rf"HKLM64\{name}", name=name, version=version, publisher=publisher, scope=scope, arch="x64")
    upd = Update(source, new, "o/r", download_url="https://example.invalid/x.exe", file_name="x.exe", size=size, **update) \
        if new else None
    return AppStatus(app, update=upd, winget_id=winget_id)


def winget_status(pid, name, version="1.0", new=None):
    app = InstalledApp(reg_key=f"WINGET\\{pid}", name=name, version=version, publisher=pid.split(".")[0],
                       scope="user", arch="x64")
    return AppStatus(app, winget_id=pid, update=Update("winget", new, pid) if new else None)


def until(condition, timeout=6.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class WindowCase(unittest.TestCase):
    """A window per test, with the data folders redirected into a temp folder and the system calls faked."""

    persist = False

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        for patcher in (mock.patch.object(history, "DATA_DIR", self.tmp),
                        mock.patch.object(scan_store, "SNAPSHOT_FILE", self.tmp / "last_scan.json"),
                        mock.patch.object(theme, "DATA_DIR", self.tmp),
                        mock.patch.object(winget, "available", return_value=True),
                        mock.patch.object(window_module.elevation, "is_elevated", return_value=False),
                        mock.patch.object(window_module.elevation, "can_relaunch_elevated", return_value=True),
                        mock.patch.object(window_module, "QMessageBox", FakeBox)):
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeBox.log, FakeBox.pick, FakeBox.tick = [], 0, None
        history.configure(None)
        apply_language(self.app, "en")
        self.updater = FakeUpdater()
        self.windows: list[MainWindow] = []
        self.addCleanup(self._close_windows)

    def _close_windows(self):
        self.updater.release.set()
        for win in self.windows:
            win.cancel_all_jobs()
            win.hide()
            win.deleteLater()       # a stale window would be re-styled by every later theme change
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for handler in [h for h in logging.getLogger("upstream").handlers if isinstance(h, UiLogHandler)]:
            logging.getLogger("upstream").removeHandler(handler)
        apply_language(self.app, "en")
        self.app.setStyleSheet("")
        self.app.setPalette(QPalette())
        theme._current = theme.THEMES["paper"]

    def make(self, statuses=(), persist=None, **values) -> MainWindow:
        self.settings = FakeSettings(**values)
        win = MainWindow(self.settings, object(), persist=self.persist if persist is None else persist)
        win.updater = self.updater
        win.start_scan = mock.Mock(name="start_scan")       # a real check would read the registry and the network
        self.windows.append(win)
        if statuses:
            self.fill(win, statuses)
        return win

    @staticmethod
    def fill(win, statuses):
        win.statuses = {s.uid: s for s in statuses}
        win.last_scan = datetime.now()
        win.populate()

    def pump(self, condition, timeout=6.0) -> bool:
        def check():
            self.app.processEvents()
            return condition()
        return until(check, timeout)

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def updates_uids(self, win) -> list[str]:
        lw = win.lists["updates"]
        return [lw.item(i).data(Qt.UserRole) for i in range(lw.count())]

    def run_job(self, win, uid, **kwargs):
        self.assertTrue(win.request_update(uid, **kwargs))
        self.assertTrue(self.pump(lambda: uid not in win.active_jobs))


class TabsTest(WindowCase):
    def test_the_tabs_and_their_counts(self):
        win = self.make([make_status("Alpha"), make_status("Beta", new=None)])
        self.assertEqual(list(win.tab_buttons), ["updates", "all", "discover", "history", "ignored"])
        self.assertEqual([b.text() for b in win.tab_buttons.values()],
                         ["Updates  1", "All apps  2", "Discover", "History", "Ignored"])

    def test_last_tab_is_restored_without_a_write_and_saved_when_the_user_switches(self):
        win = self.make(last_tab="all")
        self.assertEqual(win.current_tab, "all")
        self.assertTrue(win.tab_buttons["all"].isChecked())
        self.assertEqual(self.settings.saves, 0)
        win.tab_buttons["ignored"].click()
        self.assertEqual((win.current_tab, self.settings.values["last_tab"], self.settings.saves), ("ignored", "ignored", 1))
        win.show_tab("ignored")
        self.assertEqual(self.settings.saves, 1)    # nothing changed, nothing written

    def test_an_unknown_last_tab_opens_updates(self):
        for stored in ("nonsense", None, 3):
            self.assertEqual(self.make(last_tab=stored).current_tab, "updates", stored)

    def test_the_search_box_belongs_to_the_list_tabs_only(self):
        win = self.make()
        for key, shown in (("updates", True), ("all", True), ("discover", False), ("history", False), ("ignored", True)):
            win.show_tab(key)
            self.assertEqual(not win.search.isHidden(), shown, key)

    def test_the_pages_in_the_stack(self):
        win = self.make([make_status("Alpha")])
        win.show_tab("discover")
        self.assertIs(win.stack.currentWidget(), win.discover_page)
        win.show_tab("all")
        self.assertIs(win.stack.currentWidget(), win.library)
        win.show_tab("updates")
        self.assertIs(win.stack.currentWidget(), win.updates_page)
        win.show_tab("ignored")
        self.assertIs(win.stack.currentWidget(), win.empty)     # nothing ignored yet

    def test_the_history_page_is_built_when_first_opened(self):
        history.record("update", "Alpha", "1", "2", True, "Installed {version}", {"version": "2"}, "github")
        win = self.make()
        self.assertIsNone(win.history_page)
        win.show_tab("history")
        self.assertIsNotNone(win.history_page)
        self.assertIs(win.stack.currentWidget(), win.history_page)
        self.assertEqual(win.history_page.list.count(), 1)

    def test_a_history_tab_restored_at_start_has_its_page(self):
        win = self.make(last_tab="history")
        self.assertIs(win.stack.currentWidget(), win.history_page)

    def test_the_all_apps_page_stays_when_a_search_matches_nothing(self):
        win = self.make([make_status("Alpha")])
        win.show_tab("all")
        win.search.setText("zzz")
        self.assertIs(win.stack.currentWidget(), win.library)
        self.assertEqual(win.library.visible_count(), 0)
        win.search.setText("alp")
        self.assertEqual(win.library.visible_count(), 1)


class RowFactoryTest(WindowCase):
    def test_every_row_is_registered_and_queued_for_its_icon(self):
        win = self.make([make_status("Alpha"), make_status("Beta", new=None)])
        self.assertEqual(sorted(len(rows) for rows in win.rows.values()), [1, 2])   # Alpha: All apps and Updates
        self.assertEqual(len(win.icon_queue), 3)
        self.assertEqual(sum(1 for r in win.icon_queue if r.uid == win.statuses[r"HKLM64\Alpha"].uid), 2)

    def test_the_wave_two_chips_are_handed_to_each_row(self):
        calls = []

        class Recording(AppRow):
            def set_badges(self, badges):
                calls.append(("badges", self.uid, badges))
                super().set_badges(badges)

            def set_pin(self, pin):
                calls.append(("pin", self.uid, pin))
                super().set_pin(pin)

            def set_auto_update(self, on):
                calls.append(("auto", self.uid, on))
                super().set_auto_update(on)

        status = make_status("Alpha")
        status.badges = [Badge("security", "Security fix", "")]
        status.pin = {"mode": "hold", "version": "1.0"}
        with mock.patch.object(window_module, "AppRow", Recording):
            self.make([status], auto_update=[status.app.ident])
        self.assertEqual({c[0] for c in calls}, {"badges", "pin", "auto"})
        for kind, expected in (("badges", status.badges), ("pin", status.pin), ("auto", True)):
            got = [c[2] for c in calls if c[0] == kind]
            self.assertEqual(got, [expected] * 2, kind)     # the All apps row and the Updates row

    def test_a_row_made_while_a_job_runs_shows_the_job(self):
        win = self.make([make_status("Alpha"), make_status("Beta")])
        uid = r"HKLM64\Alpha"
        self.updater.script["Alpha"] = "polite"
        win.request_update(uid)
        self.assertTrue(self.pump(lambda: win.job_state[uid][0] == "progress"))
        win.populate()      # a rebuild (a language switch, a recheck of another row) must not lose the control
        for row in win.rows[uid]:
            self.assertFalse(row.cancel_btn.isHidden())
            self.assertEqual(row.actions.currentIndex(), 1)
        self.assertEqual(win.rows[r"HKLM64\Beta"][0].actions.currentIndex(), 0)

    def test_rows_the_page_threw_away_are_dropped_from_the_registry(self):
        win = self.make([make_status("Alpha")])
        uid = r"HKLM64\Alpha"
        self.assertEqual(len(win.rows[uid]), 2)
        win.library.set_statuses([], set(), set())      # the page deletes its rows; the registry is not told
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        live = win._live_rows(uid)
        self.assertEqual(len(live), 1)
        self.assertEqual(len(win.rows[uid]), 1)
        win.library.set_statuses([], set(), set())
        win._set_state(uid, ("queued",))                # must not touch a deleted row

    def test_the_library_gets_the_skipped_apps_and_not_the_ignored_ones(self):
        win = self.make([make_status("Alpha"), make_status("Beta"), make_status("Gamma")])
        self.settings.ignored.add(win.statuses[r"HKLM64\Gamma"].app.ident)
        self.settings.skipped[win.statuses[r"HKLM64\Beta"].app.ident] = "2.0"
        win.populate()
        self.assertEqual(win.library.count(), 2)
        self.assertEqual(win.lists["ignored"].count(), 1)
        self.assertEqual(self.updates_uids(win), [r"HKLM64\Alpha"])    # Beta is skipped
        self.assertEqual(win.tab_buttons["all"].text(), "All apps  2")


class BatchTest(WindowCase):
    def setUp(self):
        super().setUp()
        self.win = self.make([make_status("Alpha"), make_status("Beta"), make_status("Gamma", new=None)])

    def test_ignore_marks_every_ticked_app_and_moves_it_to_the_ignored_tab(self):
        self.win.library.batch_ignore_requested.emit([r"HKLM64\Alpha", r"HKLM64\Gamma", "missing"])
        self.assertEqual(self.settings.ignored, {self.win.statuses[r"HKLM64\Alpha"].app.ident,
                                                 self.win.statuses[r"HKLM64\Gamma"].app.ident})
        self.assertEqual(self.win.lists["ignored"].count(), 2)
        self.assertEqual(self.win.library.count(), 1)

    def test_export_writes_just_the_ticked_apps(self):
        target = self.tmp / "picked.csv"
        with mock.patch.object(self.win, "_choose_format", return_value="csv"), \
                mock.patch.object(window_module.QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.win.library.batch_export_requested.emit([r"HKLM64\Beta"])
        text = target.read_text("utf-8-sig")
        self.assertIn("Beta", text)
        self.assertNotIn("Alpha", text)
        self.assertEqual(FakeBox.log[-1].text, "Saved a list of 1 app.")

    def test_export_with_no_format_chosen_does_nothing(self):
        with mock.patch.object(self.win, "_choose_format", return_value=None), \
                mock.patch.object(window_module.QFileDialog, "getSaveFileName", side_effect=AssertionError("no dialog")):
            self.win.library.batch_export_requested.emit([r"HKLM64\Beta"])
        self.assertEqual(FakeBox.log, [])

    def test_uninstall_opens_the_batch_dialog_for_the_ticked_apps_and_does_the_same_bookkeeping(self):
        made = []

        class FakeBatch(QObject):
            removed = Signal(str)
            ended = Signal()
            cleanup_finished = Signal(str, object)
            restored = Signal(str, object)

            def __init__(self, statuses, apps, parent, updates_running=False, settings=None):
                super().__init__()
                self.statuses, self.apps, self.settings, self.updates_running = statuses, apps, settings, updates_running
                self.jobs = []
                self.released = False
                made.append(self)

            def exec(self):
                self.removed.emit(r"HKLM64\Alpha")

            def release(self):
                self.released = True

        with mock.patch.object(window_module, "BatchUninstallDialog", FakeBatch):
            self.win.library.batch_uninstall_requested.emit([r"HKLM64\Alpha", r"HKLM64\Beta"])
        dialog = made[0]
        self.assertEqual([s.app.name for s in dialog.statuses], ["Alpha", "Beta"])
        self.assertEqual(len(dialog.apps), 3)
        self.assertIs(dialog.settings, self.settings)
        self.assertTrue(dialog.released)
        self.assertNotIn(r"HKLM64\Alpha", self.win.statuses)
        self.assertIn(r"HKLM64\Alpha", self.win.removed)
        self.assertEqual(self.win.library.count(), 2)

    def test_uninstall_waits_for_a_running_uninstall(self):
        with mock.patch.object(window_module, "uninstalls_running", return_value=1), \
                mock.patch.object(window_module, "BatchUninstallDialog", side_effect=AssertionError("no dialog")):
            self.win.library.batch_uninstall_requested.emit([r"HKLM64\Alpha"])
        self.assertIn("still running", self.win.subtitle.text())


class MenuKeysTest(WindowCase):
    def setUp(self):
        super().setUp()
        self.status = make_status("Alpha", version="2.5", new="3.0", winget_id="Demo.Alpha")
        self.win = self.make([self.status])
        self.uid = self.status.uid

    def test_details_opens_the_dialog_with_the_icon_and_the_busy_check(self):
        shown = []

        class FakeDetails:
            def __init__(self, status, parent, icon=None, updates_running=False):
                self.args = (status, parent, icon, updates_running)
                shown.append(self)

            def setAttribute(self, *a):
                pass

            def show(self):
                self.shown = True

        with mock.patch.object(window_module, "DetailsDialog", FakeDetails):
            self.win.on_menu(self.uid, "details")
        status, parent, icon, busy = shown[0].args
        self.assertEqual((status, parent), (self.status, self.win))
        self.assertFalse(icon.isNull())
        self.assertEqual(busy, self.win.is_busy)
        self.assertTrue(shown[0].shown)

    def pin_with(self, mode):
        class FakePin:
            def __init__(self, status, parent):
                self.mode = mode

            def exec(self):
                return QDialog.Accepted

        return mock.patch.object(window_module, "PinDialog", FakePin)

    def test_pin_saves_the_pin_and_rechecks_the_app(self):
        with self.pin_with("major"), mock.patch.object(self.win.scanner, "recheck") as recheck:
            self.win.on_menu(self.uid, "pin")
            self.assertTrue(self.pump(lambda: self.uid not in self.win.job_state))
        self.assertEqual(self.settings.values["pins"], {self.status.app.ident: {"mode": "major", "version": "2.5"}})
        self.assertGreaterEqual(self.settings.saves, 1)
        recheck.assert_called_once_with(self.status)

    def test_unpin_removes_the_pin_and_rechecks(self):
        pins.set_pin(self.settings, self.status.app.ident, "hold", "2.5")
        with mock.patch.object(self.win.scanner, "recheck") as recheck:
            self.win.on_menu(self.uid, "unpin")
            self.assertTrue(self.pump(lambda: self.uid not in self.win.job_state))
        self.assertEqual(self.settings.values["pins"], {})
        recheck.assert_called_once()

    def test_a_pin_that_cannot_be_saved_says_so(self):
        self.settings.fail = OSError("disk full")
        with self.pin_with("hold"), mock.patch.object(self.win.scanner, "recheck") as recheck:
            self.win.on_menu(self.uid, "pin")
        recheck.assert_not_called()
        self.assertIn("disk full", FakeBox.log[-1].text)

    def test_the_pin_dialog_offers_the_three_choices_and_starts_on_the_current_one(self):
        dialog = window_module.PinDialog(self.status)
        self.assertEqual(set(dialog.radios), {"hold", "major", None})
        self.assertTrue(dialog.radios[None].isChecked())
        self.assertIn("2.5", dialog.radios["hold"].text())
        self.assertIn("2.x", dialog.radios["major"].text())
        dialog.radios["major"].setChecked(True)
        dialog.save_btn.click()
        self.assertEqual(dialog.mode, "major")
        self.status.pin = {"mode": "hold", "version": "2.5"}
        again = window_module.PinDialog(self.status)
        self.assertTrue(again.radios["hold"].isChecked())

    def test_a_major_pin_needs_a_version_with_a_number(self):
        self.status.app.version = "nightly"
        dialog = window_module.PinDialog(self.status)
        self.assertFalse(dialog.radios["major"].isEnabled())
        dialog.save_btn.click()
        self.assertIsNone(dialog.mode)

    def test_auto_toggles_the_app_in_the_auto_update_list_and_tells_the_background(self):
        background = mock.Mock()
        self.win.background = background
        ident = self.status.app.ident
        self.win.on_menu(self.uid, "auto")
        self.assertEqual(self.settings.values["auto_update"], [ident])
        self.assertTrue(self.settings.saves)
        self.assertEqual(background.settings_changed.call_count, 1)
        self.win.on_menu(self.uid, "auto")
        self.assertEqual(self.settings.values["auto_update"], [])
        self.assertEqual(background.settings_changed.call_count, 2)

    def test_auto_updates_the_state_shown_on_every_row_of_the_app(self):
        states = []
        for row in self.win.rows[self.uid]:
            row.set_auto_update = lambda on, r=row: states.append(on)
        self.win.on_menu(self.uid, "auto")
        self.assertEqual(states, [True, True])


class RollbackTest(WindowCase):
    persist = True

    def setUp(self):
        super().setUp()
        self.status = make_status("Alpha", version="2.5", new="3.0", winget_id="Demo.Alpha")
        self.win = self.make([self.status], persist=True)
        self.win._recheck = mock.Mock(name="_recheck")      # a real recheck would ask winget and GitHub
        self.uid = self.status.uid
        self.original = (self.status.update, self.status.fallbacks)
        self.offered = []
        self.looked = []
        self.win.bus.versions_found.connect(lambda uid, found: self.looked.append(uid))   # runs after the window's own slot

    def saved_update(self):
        """The update the snapshot file holds for the app (read as JSON: loading drops apps the registry lacks)."""
        return json.loads((self.tmp / "last_scan.json").read_text("utf-8"))["statuses"][0]["update"]

    def start(self, found, choose="2.0"):
        offered = self.offered

        class FakePicker:
            def __init__(self, status, versions, parent):
                offered.append(list(versions))
                self.version = choose

            def exec(self):
                return QDialog.Accepted

        picker = mock.patch.object(window_module, "RollbackDialog", FakePicker)
        listing = mock.patch.object(window_module.pins, "available_versions", return_value=found)
        with picker, listing:
            self.win.on_menu(self.uid, "rollback")
            self.assertEqual(self.win.job_state[self.uid][0], "progress")      # the row says it is looking
            self.assertTrue(self.pump(lambda: self.looked))

    def test_the_version_list_has_the_name_the_theme_styles(self):
        dialog = window_module.RollbackDialog(self.status, ["2.0", "1.0"])
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.list.objectName(), "versions")

    def test_only_older_versions_are_offered_newest_first(self):
        self.start(["3.0", "2.5", "2.0", "1.0", "preview"], choose="2.0")
        self.assertEqual(self.offered, [["2.0", "1.0"]])

    def test_the_rollback_runs_through_the_queue_without_fallbacks(self):
        self.status.fallbacks = [Update("site", "3.0", "https://x.invalid", download_url="https://x.invalid/a.exe")]
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: "Alpha" in self.updater.started))
        self.assertTrue(self.pump(lambda: self.win.job_state[self.uid][0] == "result"))
        self.assertEqual(self.updater.modes["Alpha"][:2], ("rollback", []))
        self.assertEqual(FakeBox.log[-1].buttons[0][1][0], i18n.tr("Roll back"))
        self.assertIn("2.0", FakeBox.log[-1].text)
        self.assertEqual(self.status.app.version, "2.0")
        entry = history.load()[0]
        self.assertEqual((entry.kind, entry.version_from, entry.version_to, entry.ok, entry.source),
                         ("rollback", "2.5", "2.0", True, "winget"))

    def test_declining_the_confirmation_starts_nothing(self):
        FakeBox.pick = 1
        self.start(["3.0", "2.5", "2.0"])
        self.assertEqual(self.win.active_jobs, set())
        self.assertEqual(self.updater.started, [])
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)

    def test_no_older_version_says_so_and_opens_no_picker(self):
        self.start(["3.0", "2.5"])
        self.assertEqual(self.offered, [])
        self.assertIn("Alpha", FakeBox.log[-1].text)
        self.assertEqual(self.updater.started, [])

    def test_a_lookup_that_found_nothing_says_so(self):
        self.start([])
        self.assertEqual(self.offered, [])
        self.assertEqual(len(FakeBox.log), 1)

    def test_cancelling_a_rollback_puts_the_original_offer_back(self):
        self.updater.script["Alpha"] = "polite"
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.win.job_state.get(self.uid, ("",))[0] == "progress"))
        self.win.cancel_job(self.uid)
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)
        self.assertNotIn(self.uid, self.win.job_state)
        self.assertEqual(history.load(), [])        # a cancellation is not history

    def test_a_failed_rollback_puts_the_original_offer_back_and_says_why(self):
        # The stash used to be restored only for a cancellation: a failure left the older version as the pending
        # "update", the real offer lost until the next check and "rollback" in the snapshot.
        self.updater.script["Alpha"] = "fail"
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs and self.updater.started))
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)
        self.assertEqual(self.status.update.version, "3.0")
        self.assertNotIn(self.uid, self.win.job_state)
        self.assertEqual(self.win.pending_updates(), [self.uid])        # the offer is back, as an ordinary update
        self.assertEqual(self.win._rollback_original, {})
        box = FakeBox.log[-1]
        self.assertEqual(box.icon, FakeBox.Warning)
        self.assertEqual(box.text, "Alpha could not be rolled back.")
        self.assertEqual(box.detail, "boom")
        entry = history.load()[0]
        self.assertEqual((entry.kind, entry.version_to, entry.ok), ("rollback", "2.0", False))
        saved = self.saved_update()
        self.assertEqual((saved["version"], saved["winget_mode"]), ("3.0", self.original[0].winget_mode))
        self.assertNotEqual(saved["winget_mode"], "rollback")
        row = self.win._live_rows(self.uid)[0]
        self.assertEqual(row.status.update.version, "3.0")      # the row shows the real offer again

    def test_a_rollback_that_was_declined_is_put_back_without_a_message(self):
        self.updater.script["Alpha"] = "declined"
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs and self.updater.started))
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)
        self.assertEqual(self.win.pending_updates(), [self.uid])
        self.assertEqual(history.load(), [])
        self.assertEqual([b for b in FakeBox.log if "rolled back" in b.text and "could not" in b.text], [])

    def test_the_snapshot_never_holds_a_rollback_offer(self):
        rollback = pins.rollback_update(self.status, "2.0")
        self.win._rollback_original[self.uid] = self.original
        self.status.update, self.status.fallbacks = rollback, []
        self.win._save_snapshot()
        self.assertEqual(self.saved_update()["version"], "3.0")
        self.assertEqual(self.status.update, rollback)          # the live status is left alone
        self.win._rollback_original.clear()
        self.win._save_snapshot()
        self.assertIsNone(self.saved_update())

    def test_the_confirmation_offers_to_hold_the_app_at_the_older_version_and_starts_ticked(self):
        self.start(["3.0", "2.5", "2.0"])
        box = FakeBox.log[-1]
        self.assertTrue(box.check_box.isChecked())
        self.assertEqual(box.check_box.text(), "Also hold this app at 2.0 so it is not updated again")

    def test_a_successful_rollback_holds_the_app_there_and_rechecks(self):
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.win._recheck.called))
        self.assertEqual(self.settings.values["pins"], {self.status.app.ident: {"mode": "hold", "version": "2.0"}})
        self.win._recheck.assert_called_once_with(self.status)
        self.assertEqual(self.win._rollback_hold, {})

    def test_the_hold_can_be_switched_off(self):
        FakeBox.tick = False
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.win.job_state.get(self.uid, ("",))[0] == "result"))
        self.assertNotIn("pins", self.settings.values)
        self.win._recheck.assert_not_called()
        self.assertEqual(self.win._rollback_hold, {})

    def test_no_hold_when_the_rollback_did_not_happen(self):
        self.updater.script["Alpha"] = "fail"
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs and self.updater.started))
        self.assertNotIn("pins", self.settings.values)
        self.win._recheck.assert_not_called()
        self.assertEqual(self.win._rollback_hold, {})

    def test_a_request_that_is_refused_forgets_the_rollback_and_its_hold(self):
        self.win.scanning = True
        self.win.roll_back(self.status, "2.0")
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)
        self.assertEqual((self.win._rollback_original, self.win._rollback_hold), ({}, {}))

    def test_a_hold_that_cannot_be_saved_says_so_and_still_rechecks(self):
        self.settings.fail = OSError("disk full")
        self.start(["3.0", "2.5", "2.0"])
        self.assertTrue(self.pump(lambda: self.win._recheck.called))
        self.assertIn("disk full", FakeBox.log[-1].text)

    def test_a_check_that_finishes_behind_the_confirmation_does_not_split_the_rollback(self):
        # The picker and the confirmation are modal; a check finishing meanwhile replaces every status. The rollback
        # used to edit the old object while the queue ran the new one: a plain upgrade.
        fresh = make_status("Alpha", version="2.5", new="3.0", winget_id="Demo.Alpha")

        def confirm(status, version):
            self.win.statuses = {fresh.uid: fresh}
            return True, False

        with mock.patch.object(self.win, "_confirm_rollback", confirm):
            self.win.roll_back(self.status, "2.0")
        self.assertTrue(self.pump(lambda: self.updater.started))
        self.assertEqual(self.updater.modes["Alpha"][0], "rollback")
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)     # the old object is untouched

    def test_a_status_replaced_before_the_rollback_starts_is_the_one_that_is_rolled_back(self):
        fresh = make_status("Alpha", version="2.5", new="3.0", winget_id="Demo.Alpha")
        self.win.statuses = {fresh.uid: fresh}
        self.win.roll_back(self.status, "2.0")
        self.assertTrue(self.pump(lambda: self.updater.started))
        self.assertEqual(self.updater.modes["Alpha"][0], "rollback")
        self.assertEqual((self.status.update, self.status.fallbacks), self.original)

    def test_an_app_that_is_gone_after_a_check_is_not_rolled_back(self):
        self.win.statuses = {}
        with mock.patch.object(self.win, "_confirm_rollback", side_effect=AssertionError("no question")):
            self.win.roll_back(self.status, "2.0")
        self.assertEqual((self.win.active_jobs, self.updater.started), (set(), []))

    def test_update_all_and_the_background_leave_an_app_alone_while_its_versions_are_looked_up(self):
        gate = threading.Event()
        answered = []
        self.win.bus.versions_found.connect(lambda uid, found: answered.append(uid))
        with mock.patch.object(window_module.pins, "available_versions", side_effect=lambda _id: gate.wait(5) and []):
            self.win.on_menu(self.uid, "rollback")
            self.assertEqual(self.win._looking_up, {self.uid})
            self.assertEqual(self.win.pending_updates(), [])
            self.win.update_all()
            self.assertFalse(self.win.request_update(self.uid, bulk=True, auto=True))
            self.assertEqual((self.win.active_jobs, self.updater.started), (set(), []))
            self.win.on_menu(self.uid, "rollback")          # a second lookup for the same app is not started
            gate.set()
            self.assertTrue(self.pump(lambda: answered))
        self.assertEqual(self.win._looking_up, set())
        self.assertEqual(self.win.pending_updates(), [self.uid])

    def test_the_answer_of_a_lookup_leaves_the_state_of_a_job_that_started_meanwhile(self):
        self.win.active_jobs.add(self.uid)
        self.win.job_state[self.uid] = ("queued",)
        self.win._looking_up.add(self.uid)
        self.win._on_versions_found(self.uid, ["2.0"])
        self.assertEqual(self.win.job_state[self.uid], ("queued",))
        self.assertEqual(self.win._looking_up, set())
        self.assertEqual(FakeBox.log, [])           # no picker over a running job
        self.win.active_jobs.discard(self.uid)

    def test_a_check_keeps_the_lookup_row_but_not_other_finished_states(self):
        self.win._looking_up.add(self.uid)
        self.win.job_state[self.uid] = ("progress", window_module.LOOKING_UP, {}, -1)
        self.win.job_state["gone"] = ("result", True, "Installed {version}", {"version": "1"}, "1")
        self.win._on_scan_finished([self.status])
        self.assertEqual(list(self.win.job_state), [self.uid])

    def test_a_recheck_that_ends_leaves_the_state_of_a_job_queued_meanwhile(self):
        self.win.active_jobs.add(self.uid)
        self.win.job_state[self.uid] = ("queued",)
        self.win._on_recheck_finished(self.status)
        self.assertEqual(self.win.job_state[self.uid], ("queued",))
        self.win.active_jobs.discard(self.uid)
        self.win.job_state[self.uid] = ("progress", "Rechecking", {}, -1)
        self.win._on_recheck_finished(self.status)
        self.assertNotIn(self.uid, self.win.job_state)

    def test_a_winget_that_cannot_list_versions_does_not_break_the_row(self):
        with mock.patch.object(window_module.pins, "available_versions", side_effect=RuntimeError("no winget")):
            self.win.on_menu(self.uid, "rollback")
            self.assertTrue(self.pump(lambda: self.looked))
        self.assertEqual((self.win.active_jobs, self.win.job_state), (set(), {}))

    def test_only_apps_winget_knows_can_be_rolled_back(self):
        other = make_status("Beta")
        self.fill(self.win, [self.status, other])
        with mock.patch.object(window_module.pins, "available_versions", side_effect=AssertionError("no lookup")):
            self.win.on_menu(other.uid, "rollback")
        self.assertNotIn(other.uid, self.win.job_state)

    def test_older_versions_helper(self):
        self.assertEqual(older_versions("2.5", ["3.0", "2.5", "2.4.9", "2.0", "weird", ""]), ["2.4.9", "2.0"])
        self.assertEqual(older_versions("nightly", ["1.0"]), [])


class HistoryTest(WindowCase):
    persist = True

    def setUp(self):
        super().setUp()
        self.win = self.make([make_status("Alpha"), make_status("Beta", version="3.0", new="3.1"),
                              make_status("Gamma"), make_status("Delta")], persist=True)

    def entries(self):
        return history.load()

    def test_an_update_is_recorded_with_versions_source_and_message(self):
        self.run_job(self.win, r"HKLM64\Alpha")
        (entry,) = self.entries()
        self.assertEqual((entry.kind, entry.name, entry.version_from, entry.version_to, entry.ok, entry.source),
                         ("update", "Alpha", "1.0", "2.0", True, "github"))
        self.assertEqual((entry.template, entry.values), ("Installed {version}", {"version": "2.0"}))

    def test_a_failure_is_recorded_with_the_version_it_tried(self):
        self.updater.script["Alpha"] = "fail"
        self.run_job(self.win, r"HKLM64\Alpha")
        (entry,) = self.entries()
        self.assertEqual((entry.ok, entry.version_from, entry.version_to, entry.template), (False, "1.0", "2.0", "{error}"))

    def test_a_declined_question_is_not_history(self):
        self.updater.script["Alpha"] = "declined"
        self.run_job(self.win, r"HKLM64\Alpha")
        self.assertEqual(self.entries(), [])

    def test_a_cancelled_update_is_not_history(self):
        self.updater.script["Alpha"] = "polite"
        self.win.request_update(r"HKLM64\Alpha")
        self.win.request_update(r"HKLM64\Beta")
        self.assertTrue(self.pump(lambda: self.win.job_state[r"HKLM64\Alpha"][0] == "progress"))
        self.win.cancel_job(r"HKLM64\Beta")             # still queued
        self.win.cancel_job(r"HKLM64\Alpha")            # running
        self.assertTrue(self.pump(lambda: not self.win.active_jobs))
        self.assertEqual(self.entries(), [])

    def test_a_window_that_does_not_persist_writes_nothing(self):
        win = self.make([make_status("Omega")], persist=False)
        self.run_job(win, r"HKLM64\Omega")
        self.assertEqual(self.entries(), [])

    def test_the_messages_that_mean_the_user_stopped_it_are_the_ones_the_update_steps_use(self):
        root = Path(window_module.__file__).resolve().parents[1]
        sources = "".join((root / name).read_text("utf-8") for name in ("net.py", "running.py", "updater.py", "installer.py",
                                                                         "exitcodes.py", "context.py"))
        sources = re.sub(r'"\s*\n\s*"', "", sources)      # a template split over two source lines
        for template in window_module.USER_STOPPED:
            self.assertIn(template, sources, template)

    def test_an_install_from_discover_is_recorded_as_an_install(self):
        pkg = Package("Foo.Bar", "Foo Bar", "1.2")
        self.win.discover_page.install_requested.emit(pkg)
        self.assertTrue(self.pump(lambda: "Foo Bar" in self.updater.started and not self.win.active_jobs))
        (entry,) = self.entries()
        self.assertEqual((entry.kind, entry.name, entry.version_from, entry.version_to, entry.ok),
                         ("install", "Foo Bar", "", "1.2", True))

    def test_a_single_uninstall_is_recorded_when_the_dialog_ended(self):
        app = self.win.statuses[r"HKLM64\Gamma"].app
        for outcome, expected in (("removed", True), ("reboot_required", True), ("failed", False), ("still_listed", False),
                                  ("cancelled", None), ("busy", None)):
            history.clear()
            dialog = types.SimpleNamespace(app=app, _outcome=(outcome, "{error}", {"error": "x"}), was_removed=False)
            self.win._record_uninstall(dialog, closing=True)
            got = [e.ok for e in self.entries()]
            self.assertEqual(got, [] if expected is None else [expected], outcome)
        history.clear()
        self.win._record_uninstall(types.SimpleNamespace(app=app, _outcome=("removed", "Removed", {}), was_removed=True))
        entry = self.entries()[0]
        self.assertEqual((entry.kind, entry.name, entry.version_from, entry.template), ("uninstall", "Gamma", "1.0", "Removed"))

    def test_an_uninstall_that_finished_after_its_dialog_was_closed_still_counts(self):
        app = self.win.statuses[r"HKLM64\Gamma"].app
        self.win._record_uninstall(types.SimpleNamespace(app=app, was_removed=True))
        self.assertEqual([e.ok for e in self.entries()], [True])
        self.win._record_uninstall(types.SimpleNamespace(app=app, was_removed=False))
        self.assertEqual(len(self.entries()), 1)

    def test_a_batch_records_each_app_once(self):
        a, b, c, d = (self.win.statuses[rf"HKLM64\{n}"].app for n in ("Alpha", "Beta", "Gamma", "Delta"))
        job = lambda app, outcome: types.SimpleNamespace(app=app, outcome=outcome)    # noqa: E731
        dialog = types.SimpleNamespace(jobs=[job(a, "removed"), job(b, "failed"), job(c, "cancelled"), job(d, "waiting")])
        self.win._record_uninstall(dialog)
        self.win._record_uninstall(dialog)
        dialog.jobs[3].outcome = "removed"
        self.win._record_uninstall(dialog)
        got = sorted((e.name, e.ok) for e in self.entries())
        self.assertEqual(got, [("Alpha", True), ("Beta", False), ("Delta", True)])
        failed = next(e for e in self.entries() if e.name == "Beta")
        self.assertEqual(failed.template, "Couldn't remove it")

    def test_check_again_after_still_listed_leaves_one_entry_for_the_answer_the_user_got(self):
        # still_listed -> Check again -> removed runs the dialog's finish twice: it used to write a failure and a removal.
        app = self.win.statuses[r"HKLM64\Gamma"].app
        dialog = types.SimpleNamespace(app=app, _outcome=("still_listed", "{error}", {"error": "x"}), was_removed=False)
        self.win._record_uninstall(dialog)
        self.assertEqual(self.entries(), [])        # not final yet: the user can still check again
        dialog._outcome = ("removed", "{name} was removed", {"name": "Gamma"})
        self.win._record_uninstall(dialog)
        self.win._record_uninstall(dialog)
        self.win._record_uninstall(dialog, closing=True)
        self.assertEqual([(e.kind, e.ok) for e in self.entries()], [("uninstall", True)])

    def test_a_dialog_closed_on_still_listed_records_that_failure_once(self):
        app = self.win.statuses[r"HKLM64\Gamma"].app
        dialog = types.SimpleNamespace(app=app, _outcome=("still_listed", "{error}", {"error": "x"}), was_removed=False)
        self.win._record_uninstall(dialog)
        self.win._record_uninstall(dialog, closing=True)
        self.win._record_uninstall(dialog, closing=True)
        self.assertEqual([(e.kind, e.ok) for e in self.entries()], [("uninstall", False)])

    def test_the_public_outcome_of_a_dialog_wins_over_the_private_one(self):
        app = self.win.statuses[r"HKLM64\Gamma"].app
        both = types.SimpleNamespace(app=app, outcome=("removed", "Removed", {}), _outcome=("failed", "{error}", {"error": "x"}),
                                     was_removed=False)
        self.win._record_uninstall(both)
        self.assertEqual([(e.ok, e.template) for e in self.entries()], [(True, "Removed")])
        history.clear()
        only_public = types.SimpleNamespace(app=app, outcome=("failed", "{error}", {"error": "x"}), was_removed=False)
        self.win._record_uninstall(only_public)
        self.assertEqual([e.ok for e in self.entries()], [False])

    def test_what_was_written_is_remembered_on_each_dialog_not_by_its_address(self):
        # A table keyed by id(dialog) lets a dialog that reuses a dead one's address skip its own records.
        a, b = (self.win.statuses[rf"HKLM64\{n}"].app for n in ("Alpha", "Beta"))
        first = types.SimpleNamespace(jobs=[types.SimpleNamespace(app=a, outcome="removed")])
        self.win._record_uninstall(first)
        del first
        gc.collect()
        second = types.SimpleNamespace(jobs=[types.SimpleNamespace(app=b, outcome="removed")])
        self.win._record_uninstall(second)
        self.assertEqual(sorted(e.name for e in self.entries()), ["Alpha", "Beta"])
        self.assertFalse(hasattr(self.win, "_uninstall_seen"))

    def test_cleanup_and_undo_are_recorded(self):
        self.win._record_cleanup("Alpha 1.0", CleanupResult(removed=3))
        self.win._record_cleanup("Alpha 1.0", CleanupResult(removed=1))
        self.win._record_cleanup("Alpha 1.0", CleanupResult(removed=0, failed=[("x", "t", {})]))
        self.win._record_cleanup("Alpha 1.0", CleanupResult(removed=0, cancelled=True))
        self.win._record_restore("Alpha 1.0", RestoreResult(restored=2))
        self.win._record_restore("Alpha 1.0", RestoreResult(restored=0, failed=[("x", "t", {})]))
        self.win._record_restore("Alpha 1.0", RestoreResult())
        got = [(e.kind, e.ok, e.template, e.values.get("n")) for e in reversed(self.entries())]
        self.assertEqual(got, [("cleanup", True, "Removed {n} items", 3), ("cleanup", True, "Removed {n} item", 1),
                               ("cleanup", False, "Nothing was removed", 0), ("restore", True, "Restored {n} items", 2),
                               ("restore", False, "Nothing was restored", 0)])

    def test_the_uninstall_dialog_is_wired_to_the_history_and_the_bookkeeping(self):
        made = []

        class FakeSingle(QObject):
            removed = Signal(str)
            ended = Signal()
            cleanup_finished = Signal(str, object)
            restored = Signal(str, object)

            def __init__(self, status, others, parent, updates_running=False, icon=None, settings=None):
                super().__init__()
                self.app, self.settings = status.app, settings
                self._outcome = ("removed", "Removed", {})
                self.was_removed = True
                made.append(self)

            def exec(self):
                self.removed.emit(r"HKLM64\Gamma")
                self.ended.emit()
                self.cleanup_finished.emit("Gamma 1.0", CleanupResult(removed=2))
                self.restored.emit("Gamma 1.0", RestoreResult(restored=2))

            def release(self):
                pass

        with mock.patch.object(window_module, "UninstallDialog", FakeSingle):
            self.win.uninstall(self.win.statuses[r"HKLM64\Gamma"])
            self.assertTrue(self.pump(lambda: len(self.entries()) == 3))
        self.assertIs(made[0].settings, self.settings)
        self.assertEqual(sorted(e.kind for e in self.entries()), ["cleanup", "restore", "uninstall"])
        self.assertNotIn(r"HKLM64\Gamma", self.win.statuses)

    def test_the_backups_dialog_records_an_undo(self):
        made = []

        class FakeBackups(QObject):
            restored = Signal(str, object)

            def __init__(self, parent, settings, days):
                super().__init__()
                self.args = (parent, settings, days)
                made.append(self)

            def exec(self):
                self.restored.emit("Alpha 1.0", RestoreResult(restored=4))

        with mock.patch.object(window_module, "BackupsDialog", FakeBackups):
            self.win.open_backups()
        self.assertEqual(made[0].args, (self.win, self.settings, 30))
        self.assertEqual([(e.kind, e.template) for e in self.entries()], [("restore", "Restored {n} items")])


class AutoUpdateTest(WindowCase):
    def make_ctx_spy(self):
        real, seen = window_module.UpdateContext, []

        def spy(*args, **kwargs):
            ctx = real(*args, **kwargs)
            seen.append(ctx)
            return ctx

        return mock.patch.object(window_module, "UpdateContext", side_effect=spy), seen

    def test_an_automatic_request_builds_a_context_that_never_asks(self):
        win = self.make([make_status("Alpha"), make_status("Beta")])
        self.updater.script["Alpha"] = "ask"
        spy, seen = self.make_ctx_spy()
        with spy, mock.patch.object(window_module, "QMessageBox", side_effect=AssertionError("no dialog")):
            self.run_job(win, r"HKLM64\Alpha", bulk=True, auto=True)
        self.assertTrue(seen[0].auto)
        self.assertTrue(seen[0].bulk)
        self.assertEqual(self.updater.asked, {"Alpha": False})
        self.assertEqual(win.auto_jobs, set())

    def test_an_ordinary_request_is_not_automatic(self):
        win = self.make([make_status("Alpha")])
        spy, seen = self.make_ctx_spy()
        with spy:
            self.run_job(win, r"HKLM64\Alpha")
        self.assertFalse(seen[0].auto)
        self.assertEqual(self.updater.modes["Alpha"][2:], (False, False))

    def test_background_is_told_how_an_automatic_update_ended_but_not_an_ordinary_one(self):
        win = self.make([make_status("Alpha"), make_status("Beta")])
        win.background = mock.Mock()
        self.run_job(win, r"HKLM64\Alpha", bulk=True, auto=True)
        self.run_job(win, r"HKLM64\Beta")
        calls = win.background.auto_finished.call_args_list
        self.assertEqual(len(calls), 1)
        uid, ok, template = calls[0].args
        self.assertEqual(uid, r"HKLM64\Alpha")
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(template, str)

    def test_a_background_that_fails_cannot_break_the_job_handler(self):
        win = self.make([make_status("Alpha")])
        win.background = mock.Mock()
        win.background.auto_finished.side_effect = RuntimeError("boom")
        self.run_job(win, r"HKLM64\Alpha", bulk=True, auto=True)
        self.assertEqual((win.active_jobs, win.auto_jobs), (set(), set()))

    def test_request_update_says_whether_it_queued(self):
        win = self.make([make_status("Alpha")])
        self.updater.script["Alpha"] = "polite"
        win.scanning = True
        self.assertFalse(win.request_update(r"HKLM64\Alpha", auto=True))
        self.assertEqual((win.active_jobs, win.auto_jobs), (set(), set()))
        win.scanning = False
        self.assertFalse(win.request_update("missing"))
        self.assertTrue(win.request_update(r"HKLM64\Alpha", auto=True))
        self.assertFalse(win.request_update(r"HKLM64\Alpha", auto=True))     # already queued


class BackgroundHooksTest(WindowCase):
    def setUp(self):
        super().setUp()
        self.win = self.make([make_status("Alpha")])
        self.background = mock.Mock()
        self.background.should_hide_on_close.return_value = False
        self.win.background = self.background

    def test_a_finished_scan_tells_the_background_but_a_failed_one_does_not(self):
        self.win._on_scan_finished([make_status("Alpha")])
        self.background.scan_finished.assert_called_once_with()
        self.win._on_scan_failed("offline")
        self.assertEqual(self.background.scan_finished.call_count, 1)

    def test_a_background_that_breaks_does_not_break_the_scan(self):
        self.background.scan_finished.side_effect = RuntimeError("boom")
        self.win._on_scan_finished([make_status("Alpha")])
        self.assertEqual(len(self.win.statuses), 1)

    def test_closing_hides_the_window_when_the_background_keeps_running(self):
        self.win.show()
        self.background.should_hide_on_close.return_value = True
        event = QCloseEvent()
        self.win.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.win.isVisible())
        self.assertEqual(FakeBox.log, [])

    def test_closing_without_a_tray_closes(self):
        event = QCloseEvent()
        self.win.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def test_quit_asks_once_when_something_is_running_and_the_close_that_followed_a_yes_does_not_ask_again(self):
        self.updater.script["Alpha"] = "polite"
        self.win.request_update(r"HKLM64\Alpha")
        with mock.patch.object(window_module.QApplication, "quit") as quit_:
            FakeBox.pick = 1       # Stay
            self.win.quit_app()
            quit_.assert_not_called()
            FakeBox.pick = 0       # Close
            self.win.quit_app()
            quit_.assert_called_once()
            asked = len(FakeBox.log)
            self.win.quit_app()
            self.assertEqual(len(FakeBox.log), asked)

    def test_a_confirmed_close_is_followed_by_a_quit_that_does_not_ask_again(self):
        self.updater.script["Alpha"] = "polite"
        self.win.request_update(r"HKLM64\Alpha")
        event = QCloseEvent()
        self.win.closeEvent(event)
        self.assertTrue(event.isAccepted())
        asked = len(FakeBox.log)
        with mock.patch.object(window_module.QApplication, "quit") as quit_:
            self.win.quit_app()     # what Background._after_close does next
        quit_.assert_called_once()
        self.assertEqual(len(FakeBox.log), asked)

    def test_quit_with_nothing_running_does_not_ask(self):
        with mock.patch.object(window_module.QApplication, "quit") as quit_:
            self.win.quit_app()
        quit_.assert_called_once()
        self.assertEqual(FakeBox.log, [])

    def test_the_settings_dialog_tells_the_window_what_changed(self):
        made = []

        class FakeSettingsDialog(QObject):
            language_changed = Signal(str)
            theme_changed = Signal(str)
            background_changed = Signal()
            history_cleared = Signal()
            badges_changed = Signal(bool)
            backups_requested = Signal()
            sources_dirty = False

            def __init__(self, settings, parent):
                super().__init__()
                made.append(self)

            def exec(self):
                self.background_changed.emit()
                self.history_cleared.emit()

        with mock.patch.object(window_module, "SettingsDialog", FakeSettingsDialog):
            self.win.show_tab("history")
            with mock.patch.object(self.win.history_page, "refresh") as refresh:
                self.win.open_settings()
        self.background.settings_changed.assert_called_once_with()
        refresh.assert_called_once_with()
        self.assertEqual(len(made), 1)

    def test_the_backups_button_opens_the_dialog_on_top_of_the_settings(self):
        opened = []
        dialog = types.SimpleNamespace()
        with mock.patch.object(self.win, "open_backups", side_effect=lambda parent=None: opened.append(parent)):
            class FakeSettingsDialog(QObject):
                language_changed = Signal(str)
                theme_changed = Signal(str)
                background_changed = Signal()
                history_cleared = Signal()
                badges_changed = Signal(bool)
                backups_requested = Signal()
                sources_dirty = False

                def __init__(self, settings, parent):
                    super().__init__()
                    dialog.me = self

                def exec(self):
                    self.backups_requested.emit()

            with mock.patch.object(window_module, "SettingsDialog", FakeSettingsDialog):
                self.win.open_settings()
        self.assertEqual(opened, [dialog.me])

    def test_switching_security_badges_off_and_on_applies_at_once(self):
        status = self.win.statuses[r"HKLM64\Alpha"]
        status.badges = [Badge("eol", "Old", "")]
        self.win._badges_changed(False)
        self.assertEqual(status.badges, [])
        fresh = [Badge("security", "Security fix", "")]
        with mock.patch.object(window_module.badges, "compute", return_value=fresh) as compute:
            self.win._badges_changed(True)
        self.assertEqual(status.badges, fresh)
        compute.assert_called_once()
        with mock.patch.object(window_module.badges, "compute", side_effect=RuntimeError("boom")):
            self.win._badges_changed(True)
        self.assertEqual(status.badges, [])


class ExportTest(WindowCase):
    def setUp(self):
        super().setUp()
        self.win = self.make([make_status("Alpha", winget_id="Demo.Alpha"), make_status("Beta"),
                              make_status("Sys", new=None)])
        self.win.statuses[r"HKLM64\Sys"].app.system_component = True
        self.target = self.tmp / "out.json"

    def dialog(self, path):
        return mock.patch.object(window_module.QFileDialog, "getSaveFileName", return_value=(str(path), ""))

    def test_json_flow_saves_the_list_remembers_the_folder_and_says_what_was_saved(self):
        with self.dialog(self.target):
            self.win.export_flow("json")
        names = [a["name"] for a in json.loads(self.target.read_text("utf-8"))["apps"]]
        self.assertEqual(names, ["Alpha", "Beta"])      # the system component is left out
        self.assertEqual(self.settings.values["export_dir"], str(self.tmp))
        self.assertTrue(self.settings.saves)
        self.assertEqual(FakeBox.log[-1].text, "Saved a list of 2 apps.")

    def test_the_save_dialog_starts_with_the_default_name_in_the_last_folder(self):
        with self.dialog(self.target) as picker:
            self.win.export_flow("csv")
            self.settings.values["export_dir"] = str(self.tmp)
            self.win.export_flow("csv")
        first, second = (call.args for call in picker.call_args_list)
        self.assertEqual(first[2], export.default_name("csv"))
        self.assertEqual(second[2], str(self.tmp / export.default_name("csv")))
        self.assertEqual(second[3], export.file_filter("csv"))

    def test_csv_has_the_bom_excel_needs(self):
        target = self.tmp / "out.csv"
        with self.dialog(target):
            self.win.export_flow("csv")
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_winget_file_says_how_many_apps_were_left_out(self):
        target = self.tmp / "winget.json"
        with self.dialog(target):
            self.win.export_flow("winget")
        self.assertEqual([p["PackageIdentifier"] for p in json.loads(target.read_text("utf-8"))["Sources"][0]["Packages"]],
                         ["Demo.Alpha"])
        self.assertIn("1 app has no winget ID", FakeBox.log[-1].text)

    def test_a_winget_list_with_no_winget_ids_is_refused_before_asking_where_to_save(self):
        win = self.make([make_status("Beta")])
        with mock.patch.object(window_module.QFileDialog, "getSaveFileName", side_effect=AssertionError("no dialog")):
            win.export_flow("winget")
        self.assertEqual(FakeBox.log[-1].text, "No app has a winget ID, so no file was saved.")

    def test_a_cancelled_save_dialog_writes_and_says_nothing(self):
        with self.dialog(""):
            self.win.export_flow("json")
        self.assertEqual(FakeBox.log, [])
        self.assertNotIn("export_dir", self.settings.values)

    def test_a_write_error_is_reported_and_leaves_no_file(self):
        missing = self.tmp / "no" / "such" / "out.json"
        with self.dialog(missing):
            self.win.export_flow("json")
        self.assertEqual(FakeBox.log[-1].icon, FakeBox.Warning)
        self.assertTrue(FakeBox.log[-1].text.startswith("Couldn't save the list: "))
        self.assertFalse(missing.exists())
        self.assertNotIn("export_dir", self.settings.values)

    def test_the_header_menu_offers_every_format(self):
        labels = [a.text() for a in self.win.export_menu.actions()]
        self.assertEqual(labels, [i18n.tr(export.FORMAT_LABELS[f]) for f in export.FORMATS])
        with self.dialog(self.target):
            self.win.export_menu.actions()[0].trigger()
        self.assertTrue(self.target.exists())

    def test_export_is_off_until_there_is_something_to_export(self):
        self.assertTrue(self.win.export_btn.isEnabled())
        self.assertFalse(self.make().export_btn.isEnabled())


class BannerTest(WindowCase):
    def setUp(self):
        super().setUp()
        self.relaunch = mock.patch.object(window_module.elevation, "relaunch_elevated", return_value=True)
        self.relaunch_mock = self.relaunch.start()
        self.addCleanup(self.relaunch.stop)

    def machine(self, win_count=2):
        return [make_status(f"App{i}") for i in range(win_count)]

    def test_the_banner_counts_the_updates_that_need_administrator_rights(self):
        win = self.make(self.machine(2) + [make_status("UserApp", scope="user")])
        self.assertFalse(win.banner_box.isHidden())
        self.assertEqual(win.banner_text.text(), elevation.banner_text(2))
        self.assertEqual(win.banner_btn.text(), "Restart as administrator")
        self.assertTrue(win.banner_btn.isEnabled())

    def test_no_banner_for_per_user_updates_or_when_it_cannot_help(self):
        self.assertTrue(self.make([make_status("UserApp", scope="user")]).banner_box.isHidden())
        with mock.patch.object(window_module.elevation, "can_relaunch_elevated", return_value=False):
            self.assertTrue(self.make(self.machine()).banner_box.isHidden())
        with mock.patch.object(window_module.elevation, "is_elevated", return_value=True):
            self.assertTrue(self.make(self.machine()).banner_box.isHidden())

    def test_dismissing_hides_it_for_the_session(self):
        win = self.make(self.machine())
        win.banner_dismiss.click()
        self.assertTrue(win.banner_box.isHidden())
        win._update_counts()
        win.populate()
        self.assertTrue(win.banner_box.isHidden())
        self.assertFalse(self.make(self.machine()).banner_box.isHidden())    # a new session shows it again

    def test_the_banner_follows_the_pending_updates(self):
        win = self.make(self.machine(2))
        self.updater.script["App0"] = "polite"
        win.request_update(r"HKLM64\App0")
        self.assertEqual(win.banner_text.text(), elevation.banner_text(1))
        win.request_update(r"HKLM64\App1")
        self.assertTrue(win.banner_box.isHidden())

    def test_the_button_waits_while_anything_is_running(self):
        win = self.make(self.machine())
        win.scanning = True
        win._update_counts()
        self.assertFalse(win.banner_btn.isEnabled())
        win.restart_as_admin()
        self.relaunch_mock.assert_not_called()
        win.scanning = False
        win._update_counts()
        self.assertTrue(win.banner_btn.isEnabled())

    def test_a_started_restart_quits_this_copy(self):
        win = self.make(self.machine())
        with mock.patch.object(win, "quit_app") as quit_:
            win.banner_btn.click()
        self.relaunch_mock.assert_called_once_with()
        quit_.assert_called_once_with()

    def test_a_declined_prompt_keeps_the_app_open_and_says_so(self):
        win = self.make(self.machine())
        self.relaunch_mock.return_value = False
        with mock.patch.object(win, "quit_app") as quit_:
            win.banner_btn.click()
        quit_.assert_not_called()
        self.assertFalse(win.banner_note.isHidden())
        self.assertEqual(win.banner_note.text(), "Administrator permission (UAC) was declined.")
        win.retranslate()
        self.assertEqual(win.banner_note.text(), "Administrator permission (UAC) was declined.")

    def test_the_footer_says_when_this_copy_runs_as_administrator(self):
        with mock.patch.object(window_module.elevation, "is_elevated", return_value=True):
            win = self.make([make_status("Alpha")])
        self.assertIn("Running as administrator", win.footer.text())
        self.assertNotIn("Running as administrator", self.make([make_status("Alpha")]).footer.text())


class SnapshotTest(WindowCase):
    persist = True

    def snapshot_file(self):
        return self.tmp / "last_scan.json"

    def apps(self):
        return [winget_status("Foo.Bar", "Foo Bar", "1.0", "2.0"), winget_status("Baz.Qux", "Baz Qux")]

    def test_a_snapshot_fills_the_window_at_once_and_says_it_is_refreshing(self):
        saved = datetime(2026, 1, 8, 9, 30)
        with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), saved.timestamp())):
            win = self.make()
            self.assertTrue(win.show_last_scan())
        self.assertEqual(len(win.statuses), 2)
        self.assertEqual(win.last_scan, saved)
        self.assertTrue(win._stale)
        self.assertTrue(win.subtitle.text().startswith("Last checked "))
        self.assertTrue(win.subtitle.text().endswith(", refreshing…"))
        self.assertIn("2026-01-08 09:30", win.subtitle.text())      # not today: the date is shown
        self.assertEqual(win.tab_buttons["updates"].text(), "Updates  1")
        self.assertTrue(win.all_btn.isEnabled())
        self.assertEqual(win.library.count(), 2)

    def test_the_subtitle_keeps_saying_so_while_the_check_runs_and_the_result_replaces_it(self):
        with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), time.time())):
            win = self.make()
            win.show_last_scan()
        win.scanning = True
        win._on_scan_progress("Checking GitHub", 3, 40)
        self.assertTrue(win.subtitle.text().endswith(", refreshing…"))
        self.assertEqual(win.scan_bar.value(), 3)
        win._on_scan_finished(self.apps())
        self.assertFalse(win._stale)
        self.assertIn("last checked", win.subtitle.text())
        self.assertNotIn("refreshing", win.subtitle.text())

    def test_a_failed_check_leaves_the_old_list_and_says_why(self):
        with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), time.time())):
            win = self.make()
            win.show_last_scan()
        win.scanning = True
        win._on_scan_failed("offline")
        self.assertEqual(win.subtitle.text(), "Check failed: offline")
        self.assertEqual(len(win.statuses), 2)

    def test_no_snapshot_changes_nothing(self):
        for result in (None, RuntimeError("boom")):
            with mock.patch.object(scan_store, "load_snapshot",
                                   **({"side_effect": result} if isinstance(result, Exception) else {"return_value": result})):
                win = self.make()
                self.assertFalse(win.show_last_scan())
            self.assertEqual((win.statuses, win.last_scan, win._stale), ({}, None, False))
        win = self.make(self.apps())
        with mock.patch.object(scan_store, "load_snapshot", return_value=([], 1.0)):
            self.assertFalse(win.show_last_scan())      # a scan that already finished is not replaced
        self.assertEqual(len(win.statuses), 2)

    def test_the_snapshot_is_saved_when_the_queue_goes_idle_and_can_be_loaded_again(self):
        win = self.make(self.apps())
        self.updater.script["Foo Bar"] = "quick"
        self.run_job(win, r"WINGET\Foo.Bar")
        self.assertTrue(self.snapshot_file().exists())
        loaded, taken = scan_store.load_snapshot()
        self.assertEqual({s.app.name: s.app.version for s in loaded}, {"Foo Bar": "2.0", "Baz Qux": "1.0"})
        self.assertAlmostEqual(taken, win.last_scan.timestamp(), places=3)
        other = self.make()
        self.assertTrue(other.show_last_scan())
        self.assertEqual(len(other.statuses), 2)

    def test_it_is_saved_after_an_uninstall_a_recheck_and_on_shutdown_but_not_during_a_check(self):
        win = self.make(self.apps())
        win._on_uninstalled(r"WINGET\Baz.Qux")
        names = lambda: [s.app.name for s in scan_store.load_snapshot()[0]]    # noqa: E731
        self.assertEqual(names(), ["Foo Bar"])
        self.snapshot_file().unlink()
        status = win.statuses[r"WINGET\Foo.Bar"]
        win._on_recheck_finished(status)
        self.assertTrue(self.snapshot_file().exists())
        self.snapshot_file().unlink()
        win.scanning = True
        win.shutdown()
        self.assertFalse(self.snapshot_file().exists())
        win.scanning = False
        win.shutdown()
        self.assertEqual(names(), ["Foo Bar"])

    def test_nothing_is_saved_before_the_first_result_or_without_persist(self):
        win = self.make()
        win.shutdown()
        self.assertFalse(self.snapshot_file().exists())
        plain = self.make(self.apps(), persist=False)
        plain.shutdown()
        self.updater.script["Foo Bar"] = "quick"
        self.run_job(plain, r"WINGET\Foo.Bar")
        self.assertFalse(self.snapshot_file().exists())

    def test_a_stored_time_that_cannot_be_a_time_does_not_stop_the_start(self):
        for taken in (1e30, float("inf"), -5.0):
            with self.subTest(taken=taken):
                with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), taken)):
                    win = self.make()
                    self.assertTrue(win.show_last_scan())
                self.assertEqual(len(win.statuses), 2)
                self.assertIsNone(win.last_scan)
                self.assertTrue(win._stale)
                self.assertEqual(win.subtitle.text(), "Checking…")      # no time to name

    def test_a_snapshot_that_cannot_be_shown_leaves_an_empty_window(self):
        with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), time.time())):
            win = self.make()
            real = win.populate
            calls = []

            def populate():
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("a row could not be built")
                real()

            win.populate = populate
            with self.assertLogs("upstream.ui", "ERROR"):
                self.assertFalse(win.show_last_scan())
        self.assertEqual((win.statuses, win.last_scan, win._stale), ({}, None, False))
        self.assertEqual(len(calls), 2)         # and the half-filled lists were cleared again

    def test_a_note_whose_format_is_bad_is_shown_as_its_template(self):
        bad = self.apps()
        bad[0].note, bad[0].note_args = "Held at {version:d}", {"version": "abc"}
        bad[1].note, bad[1].note_args = "Needs {missing}", {"other": "x"}
        bad[1].badges = [Badge("security", "Fixes {n:z}", "See {ids}", {"n": 3})]
        with self.assertRaises(ValueError):
            bad[0].note_text()
        with mock.patch.object(scan_store, "load_snapshot", return_value=(bad, time.time())):
            win = self.make()
            self.assertTrue(win.show_last_scan())
        self.assertEqual(win.statuses[bad[0].uid].note_text(), "Held at {version:d}")
        self.assertEqual(win.statuses[bad[1].uid].note_text(), "Needs {missing}")
        chip = win.statuses[bad[1].uid].badges[0]
        self.assertEqual((i18n.render(chip.text, chip.values), i18n.render(chip.tooltip, chip.values)),
                         ("Fixes {n:z}", "See {ids}"))
        for rows in win.rows.values():      # and a row can be built from it
            for row in rows:
                row.build()

    def test_a_good_note_is_left_alone(self):
        good = self.apps()
        good[0].note, good[0].note_args = "Held at {version}", {"version": "2.0"}
        with mock.patch.object(scan_store, "load_snapshot", return_value=(good, time.time())):
            win = self.make()
            win.show_last_scan()
        self.assertEqual(win.statuses[good[0].uid].note_args, {"version": "2.0"})

    def test_clicking_update_on_the_saved_list_while_the_check_runs_says_to_wait(self):
        # Instant start shows live Update buttons; the check starts 150 ms later and the click used to do nothing.
        with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), time.time())):
            win = self.make()
            win.show_last_scan()
        win.scanning = True
        win._on_scan_progress("Checking GitHub", 1, 4)
        hint = i18n.tr("Wait until the check finishes, then try again.")
        win.rows[r"WINGET\Foo.Bar"][0].update_clicked.emit(r"WINGET\Foo.Bar")
        self.assertEqual(win.subtitle.text(), hint)
        self.assertEqual(win.active_jobs, set())
        win._on_scan_progress("Checking GitHub", 2, 4)         # progress meanwhile does not wipe the hint ...
        self.assertEqual(win.subtitle.text(), hint)
        win._hint_timer.stop()
        win._end_hint()
        self.assertTrue(win.subtitle.text().endswith(", refreshing…"))      # ... and is not lost either

    def test_the_hint_goes_away_by_itself_and_the_previous_subtitle_comes_back(self):
        with mock.patch.object(window_module, "HINT_MS", 30):
            win = self.make(self.apps())
        win.scanning = True
        win.subtitle.setText("Reading installed apps 3 of 9")
        self.assertFalse(win.request_update(r"WINGET\Foo.Bar"))
        self.assertEqual(win.subtitle.text(), i18n.tr("Wait until the check finishes, then try again."))
        self.assertTrue(win._hint_timer.isActive())
        self.assertTrue(self.pump(lambda: not win._hint_timer.isActive()))
        self.assertEqual(win.subtitle.text(), "Reading installed apps 3 of 9")

    def test_only_a_users_click_gets_the_hint_not_update_all_or_an_automatic_request(self):
        win = self.make(self.apps())
        win.scanning = True
        win.subtitle.setText("before")
        self.assertFalse(win.request_update(r"WINGET\Foo.Bar", bulk=True))
        self.assertFalse(win.request_update(r"WINGET\Foo.Bar", bulk=True, auto=True))
        self.assertFalse(win.request_update("missing"))
        self.assertEqual(win.subtitle.text(), "before")
        self.assertFalse(win._hint_timer.isActive())

    def test_a_language_switch_ends_a_hint_instead_of_keeping_it_in_the_old_language(self):
        win = self.make(self.apps())
        win.subtitle.setText("before")
        win.scanning = True
        win.request_update(r"WINGET\Foo.Bar")
        win.retranslate()
        self.assertFalse(win._hint_timer.isActive())
        self.assertNotIn(i18n.tr("Wait until the check finishes, then try again."), win.subtitle.text())

    def test_a_check_that_has_to_wait_for_an_uninstall_runs_when_it_is_over(self):
        # _queue_idle cleared the flag and scheduled start_scan, which silently returned while an uninstall ran: a
        # check asked for by a Discover install was lost and the "refreshing" subtitle stayed.
        win = self.make(self.apps())
        win._rescan_wanted = True
        with mock.patch.object(window_module, "uninstalls_running", return_value=1):
            window_module.MainWindow.start_scan(win)
            self.assertFalse(win.scanning)
            self.assertTrue(win._rescan_wanted)
            self.assertTrue(win._rescan_timer.isActive())
            win._rescan_timer.stop()
            win.start_scan = lambda: window_module.MainWindow.start_scan(win)
            win._rescan_if_wanted()         # still running: asks again
            self.assertTrue(win._rescan_wanted)
            self.assertTrue(win._rescan_timer.isActive())
            win._rescan_timer.stop()
        win.start_scan = mock.Mock(name="start_scan")
        with mock.patch.object(window_module, "uninstalls_running", return_value=0):
            win._rescan_if_wanted()
        win.start_scan.assert_called_once_with()
        self.assertFalse(win._rescan_wanted)
        win._rescan_if_wanted()                         # nothing wanted any more
        win.start_scan.assert_called_once_with()

    def test_the_timer_that_waits_for_an_uninstall_fires_the_check(self):
        with mock.patch.object(window_module, "RESCAN_RETRY_MS", 20):
            win = self.make(self.apps())
        win._rescan_wanted = True
        with mock.patch.object(window_module, "uninstalls_running", return_value=1):
            window_module.MainWindow.start_scan(win)
        win.start_scan = mock.Mock(name="start_scan")
        self.assertTrue(self.pump(lambda: win.start_scan.called))
        self.assertFalse(win._rescan_wanted)

    def test_a_check_asked_for_while_updates_run_waits_for_the_queue(self):
        win = self.make(self.apps())
        win.active_jobs.add("somebody")
        window_module.MainWindow.start_scan(win)
        self.assertTrue(win._rescan_wanted)
        self.assertFalse(win._rescan_timer.isActive())      # the queue going idle runs it
        win.active_jobs.clear()

    def test_a_check_that_starts_clears_the_wish(self):
        win = self.make(self.apps())
        win._rescan_wanted = True
        win.scanner = mock.Mock(**{"scan.return_value": [], "github_if_ready": None})
        with mock.patch.object(window_module, "uninstalls_running", return_value=0):
            window_module.MainWindow.start_scan(win)
        self.assertTrue(self.pump(lambda: not win.scanning))
        self.assertFalse(win._rescan_wanted)

    def test_a_stale_list_is_refreshed_when_the_queue_goes_idle(self):
        with mock.patch.object(scan_store, "load_snapshot", return_value=(self.apps(), time.time())):
            win = self.make()
            win.show_last_scan()
        starts = []
        win.start_scan = lambda: starts.append(1)
        self.run_job(win, r"WINGET\Foo.Bar")
        self.assertTrue(self.pump(lambda: starts))


class DiscoverTest(WindowCase):
    persist = True

    def setUp(self):
        super().setUp()
        self.win = self.make([make_status("Alpha")], persist=True)
        self.pkg = Package("Foo.Bar", "Foo Bar", "1.2", "Foo")
        self.uid = "WINGET\\NEW\\Foo.Bar"
        self.page = self.win.discover_page

    def state(self):
        return self.page._states["foo.bar"]

    def test_an_install_runs_through_the_queue_and_is_kept_apart_from_the_apps(self):
        self.updater.script["Foo Bar"] = "polite"
        self.page.install_requested.emit(self.pkg)
        self.assertIn(self.uid, self.win.discover_statuses)
        self.assertNotIn(self.uid, self.win.statuses)
        self.assertEqual(self.win.library.count(), 1)
        self.assertEqual(self.win.lists["updates"].count(), 1)
        self.assertTrue(self.pump(lambda: self.state()[0] == "progress"))
        self.assertEqual(self.state()[1:], ("Downloading", 10, "cancel"))
        self.win.show_tab("updates")
        self.assertEqual(self.win.tab_buttons["all"].text(), "All apps  1")

    def test_progress_phases_and_the_result_are_mirrored(self):
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.assertEqual(self.state(), ("installed", "Installed 1.2"))
        self.assertIn("foo.bar", self.page._installed)

    def test_installing_phase_offers_stop_waiting(self):
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.win.cancels[self.uid] = CancelToken()
        self.win._set_state(self.uid, ("progress", "Installing", {}, -1))
        self.assertEqual(self.state(), ("progress", "Installing", -1, "wait"))
        self.win.cancels.pop(self.uid)

    def test_cancel_from_the_page_cancels_the_job(self):
        self.updater.script["Foo Bar"] = "polite"
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.state()[0] == "progress"))
        self.page.cancel_requested.emit("Foo.Bar")
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.assertEqual(self.state()[0], "cancelled")
        self.assertEqual(history.load(), [])

    def test_a_failed_install_shows_the_failure(self):
        self.updater.script["Foo Bar"] = "fail"
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.assertEqual(self.state(), ("failed", "boom"))
        self.assertEqual([e.ok for e in history.load()], [False])

    def test_the_new_app_is_looked_for_once_the_queue_is_idle(self):
        starts = []
        self.win.start_scan = lambda: starts.append(1)
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: starts))
        self.assertEqual(starts, [1])

    def test_a_failed_install_does_not_trigger_a_rescan(self):
        starts = []
        self.win.start_scan = lambda: starts.append(1)
        self.updater.script["Foo Bar"] = "fail"
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.settle()
        self.assertEqual(starts, [])

    def test_a_request_during_a_check_is_turned_down_on_the_page(self):
        self.win.scanning = True
        self.page.install_requested.emit(self.pkg)
        self.assertEqual(self.win.discover_statuses, {})
        self.assertEqual(self.state()[0], "failed")
        self.assertIn("Wait until", self.state()[1])
        self.assertEqual(self.updater.started, [])

    def test_the_page_learns_which_packages_are_installed_after_each_check(self):
        self.win._on_scan_finished([winget_status("Demo.One", "One"), winget_status("Demo.Two", "Two")])
        self.assertEqual(self.page._installed, {"demo.one", "demo.two"})
        self.win._on_scan_finished([winget_status("Demo.One", "One")])
        self.assertEqual(self.page._installed, {"demo.one"})

    def test_a_language_switch_renders_the_mirrored_texts_again(self):
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        apply_language(self.app, "fa")
        self.win.retranslate()
        self.assertEqual(self.state()[1], i18n.render("Installed {version}", {"version": "1.2"}))
        self.assertNotEqual(self.state()[1], "Installed 1.2")

    def test_a_scan_forgets_finished_installs_but_not_running_ones(self):
        self.updater.script["Foo Bar"] = "polite"
        self.page.install_requested.emit(self.pkg)
        self.assertTrue(self.pump(lambda: self.state()[0] == "progress"))
        self.win.scanning = True
        self.win._on_scan_finished([make_status("Alpha")])
        self.assertIn(self.uid, self.win.discover_statuses)
        self.updater.release.set()
        self.assertTrue(self.pump(lambda: self.uid not in self.win.active_jobs))
        self.win._on_scan_finished([make_status("Alpha")])
        self.assertEqual(self.win.discover_statuses, {})


class SortAndSizeTest(WindowCase):
    def statuses(self):
        return [make_status("Alpha", size=5 * MB, publisher="Zed"), make_status("Beta", size=0, publisher="Acme"),
                make_status("Gamma", size=9 * MB, publisher="Mid"), make_status("Delta", size=1 * MB, publisher="")]

    def names(self, win):
        return [win.statuses[u].app.name for u in self.updates_uids(win)]

    def test_name_is_the_default_order(self):
        win = self.make(self.statuses())
        self.assertEqual(win.sort_combo.currentData(), "name")
        self.assertEqual(self.names(win), ["Alpha", "Beta", "Delta", "Gamma"])

    def test_size_puts_the_largest_first_and_unknown_sizes_last(self):
        win = self.make(self.statuses())
        win.sort_combo.setCurrentIndex(win.sort_combo.findData("size"))
        self.assertEqual(self.names(win), ["Gamma", "Alpha", "Delta", "Beta"])

    def test_publisher_orders_by_publisher_with_the_unknown_last(self):
        win = self.make(self.statuses())
        win.sort_combo.setCurrentIndex(win.sort_combo.findData("publisher"))
        self.assertEqual(self.names(win), ["Beta", "Gamma", "Alpha", "Delta"])

    def test_the_choice_is_remembered_and_restored(self):
        win = self.make(self.statuses())
        win.sort_combo.setCurrentIndex(win.sort_combo.findData("size"))
        self.assertEqual(self.settings.values["updates_sort"], "size")
        self.assertTrue(self.settings.saves)
        again = self.make(self.statuses(), updates_sort="size")
        self.assertEqual(again.sort_combo.currentData(), "size")
        self.assertEqual(self.names(again), ["Gamma", "Alpha", "Delta", "Beta"])
        for junk in ("sideways", None, 7):
            self.assertEqual(self.make(self.statuses(), updates_sort=junk).sort_combo.currentData(), "name")

    def test_picking_the_current_order_again_writes_nothing(self):
        win = self.make(self.statuses(), updates_sort="size")
        before = self.settings.saves
        win._on_sort_changed()
        self.assertEqual(self.settings.saves, before)

    def test_sorting_keeps_the_running_jobs_and_the_other_lists(self):
        win = self.make(self.statuses())
        self.updater.script["Gamma"] = "polite"
        win.request_update(r"HKLM64\Gamma")
        self.assertTrue(self.pump(lambda: win.job_state[r"HKLM64\Gamma"][0] == "progress"))
        before = {uid: len(rows) for uid, rows in win.rows.items()}
        win.sort_combo.setCurrentIndex(win.sort_combo.findData("size"))
        self.assertEqual({uid: len(rows) for uid, rows in win.rows.items()}, before)     # no row leaked, none lost
        for row in win.rows[r"HKLM64\Gamma"]:
            self.assertFalse(row.cancel_btn.isHidden())
        self.assertEqual(win.library.count(), 4)

    def test_update_all_follows_the_order_on_screen(self):
        win = self.make(self.statuses())
        win.sort_combo.setCurrentIndex(win.sort_combo.findData("size"))
        queued = []
        win.request_update = lambda uid, bulk=False, auto=False: queued.append(win.statuses[uid].app.name)
        win.update_all()
        self.assertEqual(queued, ["Gamma", "Alpha", "Delta", "Beta"])

    def test_the_search_filters_the_sorted_list(self):
        win = self.make(self.statuses(), updates_sort="size")
        win.search.setText("a")
        hidden = [win.lists["updates"].item(i).isHidden() for i in range(win.lists["updates"].count())]
        self.assertEqual(hidden, [False, False, False, False])       # every name has an "a"
        win.search.setText("gam")
        hidden = [win.lists["updates"].item(i).isHidden() for i in range(win.lists["updates"].count())]
        self.assertEqual(hidden, [False, True, True, True])

    def test_the_total_download_size_is_shown_on_update_all(self):
        win = self.make([make_status("Alpha", size=1 * MB), make_status("Beta", size=2 * MB)])
        self.assertEqual(win._all_texts[0], "Update all (2) · 3.0 MB")

    def test_when_only_some_sizes_are_known_it_says_at_least(self):
        win = self.make([make_status("Alpha", size=1 * MB), make_status("Beta", size=0), make_status("Gamma", size=512 * 1024)])
        self.assertEqual(win._all_texts[0], "Update all (3) · at least 1.5 MB")

    def test_when_no_size_is_known_it_is_left_out(self):
        win = self.make([make_status("Alpha"), make_status("Beta")])
        self.assertEqual(win.all_btn.text(), "Update all (2)")
        self.assertEqual(self.make().all_btn.text(), "Update all")

    def test_the_total_only_counts_what_is_still_to_install(self):
        win = self.make([make_status("Alpha", size=1 * MB), make_status("Beta", size=2 * MB)])
        self.updater.script["Beta"] = "polite"
        win.request_update(r"HKLM64\Beta")
        self.assertEqual(win._all_texts[0], "Update all (1) · 1.0 MB")

    def test_sizes_that_make_no_sense_count_as_unknown(self):
        win = self.make([make_status("Alpha", size=3 * MB), make_status("Beta")])
        win.statuses[r"HKLM64\Beta"].update.size = "big"
        win._update_counts()
        self.assertEqual(win._all_texts[0], "Update all (2) · at least 3.0 MB")

    def test_persian_digits_in_the_total_and_translated_sort_labels(self):
        win = self.make([make_status("Alpha", size=1 * MB), make_status("Beta", size=2 * MB)])
        apply_language(self.app, "fa")
        win.retranslate()
        self.assertEqual(win.all_btn.text(), i18n.tr("Update all ({n}) · {size}", n=i18n.num(2), size=i18n.num("3.0 MB")))
        self.assertNotIn("3", win.all_btn.text())
        self.assertIn(i18n.num(2), win.all_btn.text())
        self.assertEqual([win.sort_combo.itemText(i) for i in range(3)],
                         [i18n.FA["Name"], i18n.FA["Download size, largest first"], i18n.FA["Publisher"]])
        self.assertEqual(win.sort_label.text(), i18n.FA["Sort by"])


class LanguageAndLayoutTest(WindowCase):
    def test_the_whole_window_follows_a_language_switch_and_back(self):
        win = self.make([make_status("Alpha"), make_status("Beta", new=None)])
        win.show_tab("history")
        win.show_tab("updates")
        apply_language(self.app, "fa")
        win.retranslate()
        self.assertEqual(self.app.layoutDirection(), Qt.RightToLeft)
        self.assertEqual(win.tab_buttons["discover"].text(), i18n.FA["Discover"])
        self.assertEqual(win.tab_buttons["history"].text(), i18n.FA["History"])
        self.assertEqual(win.export_btn.text(), i18n.FA["Export list…"])
        self.assertEqual(win.banner_dismiss.text(), i18n.FA["Not now"])
        self.assertEqual(win.banner_btn.text(), i18n.FA["Restart as administrator"])
        self.assertIn(i18n.num(1), win.tab_buttons["updates"].text())
        self.assertNotIn("1", win.tab_buttons["updates"].text())
        self.assertEqual([a.text() for a in win.export_menu.actions()],
                         [i18n.FA[export.FORMAT_LABELS[f]] for f in export.FORMATS])
        apply_language(self.app, "en")
        win.retranslate()
        self.assertEqual(win.tab_buttons["discover"].text(), "Discover")
        self.assertEqual(win.export_btn.text(), "Export list…")

    @unittest.skipUnless(os.environ.get("QT_QPA_FONTDIR"), "offscreen Qt measures text with placeholder fonts; set QT_QPA_FONTDIR")
    def test_the_header_and_tabs_fit_the_default_window_in_both_languages(self):
        statuses = [make_status(f"App{i}", size=(i + 1) * 30 * MB) for i in range(12)] + [make_status("NoSize")]
        win = self.make(statuses)
        for lang in ("en", "fa"):
            apply_language(self.app, lang)
            win.retranslate()
            win.resize(1040, 740)
            win.show()
            self.settle()
            self.assertLessEqual(win.centralWidget().minimumSizeHint().width(), 1040, lang)
            win.hide()

    @unittest.skipUnless(os.environ.get("QT_QPA_FONTDIR"), "offscreen Qt measures text with placeholder fonts; set QT_QPA_FONTDIR")
    def test_nothing_in_the_header_is_clipped_at_the_narrowest_width_in_both_languages(self):
        # At 800 px the primary button read "te all (26) · at least 5" and Settings covered the subtitle.
        statuses = [make_status(f"App{i}", size=(i + 1) * 30 * MB) for i in range(25)] + [make_status("NoSize")]
        theme.apply_theme(self.app, "paper")        # the buttons' padding comes from the style sheet
        win = self.make(statuses)
        for lang in ("en", "fa"):
            for width in (win.minimumWidth(), 800, 900, 1040):
                with self.subTest(lang=lang, width=width):
                    apply_language(self.app, lang)
                    win.retranslate()
                    win.resize(width, 740)
                    win.show()
                    self.settle()
                    self.assertGreaterEqual(win.width(), win.minimumWidth())
                    self.assertGreaterEqual(win.minimumWidth(), window_module.MIN_WIDTH)
                    self.assert_header_whole(win, lang)
            win.hide()

    @unittest.skipUnless(os.environ.get("QT_QPA_FONTDIR"), "offscreen Qt measures text with placeholder fonts; set QT_QPA_FONTDIR")
    def test_update_all_keeps_the_size_when_there_is_room_and_moves_it_to_the_tooltip_when_there_is_not(self):
        statuses = [make_status(f"App{i}", size=(i + 1) * 30 * MB) for i in range(25)]
        theme.apply_theme(self.app, "paper")
        win = self.make(statuses)
        win.resize(1300, 740)
        win.show()
        self.settle()
        self.assertIn(" · ", win.all_btn.text())
        self.assertEqual(win.all_btn.toolTip(), "")
        win.resize(win.minimumWidth(), 740)
        self.settle()
        self.assertNotIn(" · ", win.all_btn.text())
        self.assertIn("Download size: ", win.all_btn.toolTip())
        self.assertIn(i18n.num("25"), win.all_btn.text())
        win.resize(1300, 740)
        self.settle()
        self.assertIn(" · ", win.all_btn.text())            # and back when the window grows

    @unittest.skipUnless(os.environ.get("QT_QPA_FONTDIR"), "offscreen Qt measures text with placeholder fonts; set QT_QPA_FONTDIR")
    def test_a_subtitle_that_does_not_fit_is_elided_with_the_whole_line_in_its_tooltip(self):
        theme.apply_theme(self.app, "paper")
        win = self.make([make_status("Alpha")])
        win.resize(win.minimumWidth(), 740)
        win.show()
        win._set_subtitle("A very long line that says a great many things about the last check and what comes next " * 3)
        self.settle()
        self.assertEqual(win.subtitle.text(), "A very long line that says a great many things about the last check and what comes next " * 3)
        self.assertEqual(win.subtitle.toolTip(), win.subtitle.text())
        self.assertLess(win.subtitle.width(), win.subtitle.fontMetrics().horizontalAdvance(win.subtitle.text()))
        self.assertFalse(win.grab().isNull())
        win._set_subtitle("Short")
        self.settle()
        self.assertEqual(win.subtitle.toolTip(), "")

    def assert_header_whole(self, win, lang):
        """Every header control is at least as wide as it asks to be, none overlaps another, and the subtitle (elided or
        not) stays in front of the buttons."""
        root = win.centralWidget()
        title = win.findChild(QLabel, "title")
        buttons = [win.settings_btn, win.export_btn, win.scan_btn, win.cancel_all_btn, win.all_btn]

        def span(w):
            x = w.mapTo(root, QPoint(0, 0)).x()
            return x, x + w.width()

        for b in buttons:
            self.assertGreaterEqual(b.width(), b.sizeHint().width(), f"{b.text()!r} is squeezed ({lang})")
        heading = (min(span(title)[0], span(win.subtitle)[0]), max(span(title)[1], span(win.subtitle)[1]))
        blocks = sorted([heading] + [span(b) for b in buttons])
        for left, right in zip(blocks, blocks[1:]):
            self.assertLessEqual(left[1], right[0], f"header controls overlap ({lang}): {blocks}")
        self.assertGreaterEqual(blocks[0][0], 0)
        self.assertLessEqual(blocks[-1][1], root.width(), f"the header runs past the window ({lang})")

    def test_the_new_chrome_exists_in_every_theme(self):
        win = self.make([make_status("Alpha")])
        for key in theme.THEMES:
            theme.apply_theme(self.app, key)
            win.show()
            self.settle()
            self.assertFalse(win.grab().isNull(), key)
        win.hide()


class LargeListTest(WindowCase):
    """Building a row costs about ten milliseconds with the real style sheet: a list of hundreds is built where the
    user looks, not all at once (the Updates tab took 1.6 s for 103 rows)."""

    COUNT = 270

    def setUp(self):
        super().setUp()
        self.statuses = [make_status(f"App{i:03d}", size=MB) for i in range(self.COUNT)]
        self.win = self.make(self.statuses)
        self.win.resize(1040, 740)
        self.win.show()
        self.settle()
        self.lw = self.win.lists["updates"]

    def built(self, tab="updates"):
        lw = self.win.lists[tab]
        return sum(1 for i in range(lw.count()) if lw.itemWidget(lw.item(i)) is not None)

    def test_only_the_rows_near_the_viewport_are_built_but_every_app_is_listed(self):
        self.assertEqual(self.lw.count(), self.COUNT)
        self.assertLess(self.built(), 40)
        self.assertGreaterEqual(self.built(), 8)            # at least what is on screen
        self.assertEqual(len(self.win.pending_updates()), self.COUNT)
        self.assertEqual(self.win.tab_buttons["updates"].text(), f"Updates  {self.COUNT}")
        self.assertEqual(self.win.all_btn.isEnabled(), True)

    def test_every_row_has_the_height_the_first_one_measured(self):
        heights = {self.lw.item(i).sizeHint().height() for i in range(self.lw.count())}
        self.assertEqual(len(heights), 1)
        first = self.lw.itemWidget(self.lw.item(0))
        self.assertEqual(heights.pop(), first.sizeHint().height())

    def test_scrolling_builds_the_rows_that_come_into_view(self):
        last = self.lw.item(self.COUNT - 1)
        self.assertIsNone(self.lw.itemWidget(last))
        before = self.built()
        bar = self.lw.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.settle()
        self.assertIsNotNone(self.lw.itemWidget(last))
        self.assertGreater(self.built(), before)
        self.assertLess(self.built(), before + 40)

    def test_a_row_built_late_shows_the_job_its_app_already_has(self):
        uid = self.statuses[200].uid
        self.assertIsNone(self.lw.itemWidget(self.lw.item(200)))
        self.win.job_state[uid] = ("progress", "Downloading", {}, 40)
        self.lw.scrollToItem(self.lw.item(200))
        self.settle()
        row = self.lw.itemWidget(self.lw.item(200))
        self.assertEqual(row.phase.full_text, "Downloading")
        self.assertEqual(row.pct.text(), "40%")
        self.assertIn(row, self.win._live_rows(uid))

    def test_a_search_builds_the_rows_it_uncovers(self):
        item = self.lw.item(250)
        self.assertIsNone(self.lw.itemWidget(item))
        self.win.search.setText("App250")
        self.settle()
        self.assertFalse(item.isHidden())
        self.assertIsNotNone(self.lw.itemWidget(item))
        self.assertEqual(sum(1 for i in range(self.lw.count()) if not self.lw.item(i).isHidden()), 1)

    def test_a_resize_that_shows_more_of_the_list_builds_more_rows(self):
        before = self.built()
        self.win.resize(1040, 1400)
        self.settle()
        self.assertGreater(self.built(), before)

    def test_the_ignored_list_is_built_the_same_way(self):
        for s in self.statuses[:60]:
            self.settings.set_ignored(s.app.ident, True)
        self.win.populate()
        self.win.show_tab("ignored")
        self.settle()
        lw = self.win.lists["ignored"]
        self.assertEqual(lw.count(), 60)
        self.assertLess(self.built("ignored"), 40)
        bar = lw.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.settle()
        self.assertIsNotNone(lw.itemWidget(lw.item(59)))

    def test_a_new_populate_leaves_no_row_behind_in_the_registry(self):
        self.win.populate()
        self.win.populate()
        total = sum(len(rows) for rows in self.win.rows.values())
        # the All apps page keeps a shell for every app; the Updates list only the rows it built
        self.assertLessEqual(total, self.COUNT + 40)
        self.assertEqual(len(self.win._update_rows), self.built())


class AppWiringTest(unittest.TestCase):
    def test_a_hand_edited_settings_file_cannot_break_the_new_keys(self):
        settings = FakeSettings(pins=[1], auto_update="x", last_tab="elsewhere")
        app_module._clean_settings(settings)
        self.assertEqual((settings.values["pins"], settings.values["auto_update"], settings.values["last_tab"]),
                         ({}, [], "updates"))
        keep = FakeSettings(pins={"a": {"mode": "hold"}}, auto_update=["a"], last_tab="history")
        app_module._clean_settings(keep)
        self.assertEqual((keep.values["pins"], keep.values["auto_update"], keep.values["last_tab"]),
                         ({"a": {"mode": "hold"}}, ["a"], "history"))

    def test_old_cleanup_backups_are_purged_off_the_ui_thread(self):
        for days, expected in ((30, [30]), (0, [0]), ("30", []), (True, []), (None, [])):
            seen = []
            with mock.patch.object(app_module.cleanup, "purge_old", side_effect=lambda d: seen.append(d) or 2):
                app_module._purge_old_backups(FakeSettings(cleanup_backup_days=days))
                self.assertTrue(until(lambda: not any(t.name == "backup-purge" for t in threading.enumerate())))
            self.assertEqual(seen, expected, days)

    def test_a_failing_purge_never_escapes(self):
        with mock.patch.object(app_module.cleanup, "purge_old", side_effect=OSError("locked")),                 self.assertLogs("upstream.app", "ERROR"):
            app_module._purge_old_backups(FakeSettings(cleanup_backup_days=30))
            self.assertTrue(until(lambda: not any(t.name == "backup-purge" for t in threading.enumerate())))

    def test_a_last_check_that_cannot_be_shown_does_not_stop_the_start_up(self):
        fake_app = mock.Mock()
        fake_app.exec.return_value = 0
        window = mock.Mock()
        window.show_last_scan.side_effect = RuntimeError("poisoned")
        patches = {name: mock.Mock() for name in (
            "setup_logging", "migrate_legacy", "QGuiApplication", "ctypes", "apply_language", "apply_theme", "app_icon",
            "QTimer", "net", "installer", "history", "cleanup", "Background", "_show_window")}
        patches.update(Settings=mock.Mock(return_value=FakeSettings(tray_enabled=False)), Cache=mock.Mock(),
                       QApplication=mock.Mock(return_value=fake_app), MainWindow=mock.Mock(return_value=window),
                       stability=mock.Mock(**{"acquire_single_instance.return_value": True}))
        with mock.patch.multiple(app_module, **patches), mock.patch("sys.argv", ["upstream"]), \
                self.assertLogs("upstream.app", "ERROR"):
            self.assertEqual(app_module.main(), 0)
        patches["_show_window"].assert_called_once()
        patches["QTimer"].singleShot.assert_any_call(150, window.start_scan)

    def test_start_up_wiring_order(self):
        order = mock.Mock()
        fake_app = mock.Mock()
        fake_app.exec.return_value = 0
        window, background = mock.Mock(), mock.Mock()
        settings = FakeSettings(tray_enabled=False)
        patches = {
            "setup_logging": mock.Mock(), "migrate_legacy": mock.Mock(), "Settings": mock.Mock(return_value=settings),
            "Cache": mock.Mock(return_value="cache"), "QApplication": mock.Mock(return_value=fake_app),
            "QGuiApplication": mock.Mock(), "ctypes": mock.Mock(), "apply_language": mock.Mock(),
            "apply_theme": mock.Mock(), "app_icon": mock.Mock(), "QTimer": mock.Mock(),
            "net": order.net, "installer": order.installer, "history": order.history, "cleanup": order.cleanup,
            "MainWindow": order.MainWindow, "Background": order.Background, "_show_window": order._show_window,
            "stability": mock.Mock(**{"acquire_single_instance.return_value": True}),
        }
        order.MainWindow.return_value = window
        order.Background.return_value = background
        with mock.patch.multiple(app_module, **patches), mock.patch("sys.argv", ["upstream", "--minimized"]):
            self.assertEqual(app_module.main(), 0)
        names = [c[0] for c in order.mock_calls if c[0] in (
            "history.configure", "cleanup.configure", "MainWindow", "Background", "_show_window")]
        self.assertEqual(names, ["history.configure", "cleanup.configure", "MainWindow", "Background", "_show_window"])
        order.MainWindow.assert_called_once_with(settings, "cache", persist=True)
        window.show_last_scan.assert_called_once_with()
        self.assertIs(window.background, background)
        order.Background.assert_called_once_with(window, settings)
        background.start.assert_called_once_with(minimized=True)
        connected = [c.args[0] for c in fake_app.aboutToQuit.connect.call_args_list]
        self.assertIn(background.stop, connected)
        self.assertIn(window.shutdown, connected)
        delays = [(c.args[0], c.args[1]) for c in patches["QTimer"].singleShot.call_args_list]
        self.assertIn((150, window.start_scan), delays)
        self.assertEqual(sorted(d for d, _ in delays), [150, app_module.CRASH_REPORT_DELAY_MS,
                                                        app_module.HOUSEKEEPING_DELAY_MS, app_module.BACKUP_PURGE_DELAY_MS])


class FakeToggleBackground:
    """background.py's autostart pair, over a dict instead of the registry."""

    def __init__(self, enabled=False, works=True):
        self.enabled, self.works, self.calls = enabled, works, []

    def set_autostart(self, on):
        self.calls.append(on)
        if self.works:
            self.enabled = on
        return self.works

    def is_autostart(self):
        return self.enabled


class SettingsSectionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.autostart = FakeToggleBackground()
        for patcher in (mock.patch.object(theme, "DATA_DIR", Path(self._tmp.name)),
                        mock.patch.object(settings_ui.background, "set_autostart", self.autostart.set_autostart),
                        mock.patch.object(settings_ui.background, "is_autostart", self.autostart.is_autostart),
                        mock.patch.object(settings_ui.history, "clear")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._reset)
        self.events = []

    def _reset(self):
        apply_language(self.app, "en")
        self.app.setStyleSheet("")
        self.app.setPalette(QPalette())
        theme._current = theme.THEMES["paper"]

    def make(self, **values):
        fake = FakeSettings(**values)
        dialog = settings_ui.SettingsDialog(fake)
        self.addCleanup(dialog.close)
        dialog.background_changed.connect(lambda: self.events.append("background"))
        dialog.history_cleared.connect(lambda: self.events.append("history"))
        dialog.badges_changed.connect(lambda on: self.events.append(("badges", on)))
        dialog.backups_requested.connect(lambda: self.events.append("backups"))
        return dialog, fake

    def test_the_new_sections_load_the_stored_values(self):
        d, _ = self.make(tray_enabled=True, close_to_tray=True, notify_updates=False, history_enabled=False,
                         security_badges=False, cleanup_restore_point=False, check_interval_hours=6, cleanup_backup_days=90)
        on = {key: t.box.isChecked() for key, t in d.toggles.items()}
        self.assertEqual(on, {"tray_enabled": True, "close_to_tray": True, "start_with_windows": False, "notify_updates": False,
                              "history_enabled": False, "security_badges": False, "cleanup_restore_point": False})
        self.assertEqual((d.check_hours.currentData(), d.backup_days.currentData()), (6, 90))

    def test_the_defaults_are_the_ones_config_py_has(self):
        d, _ = self.make()
        on = {key: t.box.isChecked() for key, t in d.toggles.items()}
        self.assertEqual(on, {"tray_enabled": False, "close_to_tray": False, "start_with_windows": False, "notify_updates": True,
                              "history_enabled": True, "security_badges": True, "cleanup_restore_point": True})
        self.assertEqual((d.check_hours.currentData(), d.backup_days.currentData()), (0, 30))

    def test_opening_the_dialog_writes_nothing(self):
        _, fake = self.make()
        self.assertEqual(fake.saves, 0)

    def test_a_switch_is_stored_and_saved_and_the_background_is_told(self):
        d, fake = self.make()
        for key in ("tray_enabled", "close_to_tray", "notify_updates"):
            before = fake.saves
            d.toggles[key].box.click()
            self.assertEqual(fake.values[key], key != "notify_updates")
            self.assertEqual(fake.saves, before + 1, key)
        self.assertEqual(self.events, ["background"] * 3)

    def test_the_check_interval_is_stored_in_whole_hours(self):
        d, fake = self.make()
        d.check_hours.setCurrentIndex(d.check_hours.findData(12))
        self.assertEqual(fake.values["check_interval_hours"], 12)
        self.assertEqual(self.events, ["background"])
        self.assertEqual(d.check_hours.itemText(d.check_hours.findData(12)), "Every 12 hours")
        self.assertEqual(d.check_hours.itemText(d.check_hours.findData(1)), "Every 1 hour")
        self.assertEqual(d.check_hours.itemText(d.check_hours.findData(0)), "Off")

    def test_a_stored_interval_that_is_not_in_the_list_is_kept_and_nonsense_is_off(self):
        d, _ = self.make(check_interval_hours=2)
        self.assertEqual(d.check_hours.currentData(), 2)
        for junk in ("soon", None, True, -4):
            self.assertEqual(self.make(check_interval_hours=junk)[0].check_hours.currentData(), 0, junk)

    def test_backup_days_are_stored_without_telling_the_background(self):
        d, fake = self.make()
        d.backup_days.setCurrentIndex(d.backup_days.findData(7))
        self.assertEqual(fake.values["cleanup_backup_days"], 7)
        self.assertEqual(self.events, [])
        self.assertEqual(d.backup_days.itemText(d.backup_days.findData(7)), "7 days")
        self.assertEqual(self.make(cleanup_backup_days="many")[0].backup_days.currentData(), 30)

    def test_start_with_windows_follows_the_registry_entry_not_the_setting(self):
        self.autostart.enabled = True
        d, _ = self.make(start_with_windows=False)
        self.assertTrue(d.toggles["start_with_windows"].box.isChecked())
        self.assertEqual(self.autostart.calls, [])

    def test_start_with_windows_round_trip(self):
        d, fake = self.make()
        d.toggles["start_with_windows"].box.click()
        self.assertEqual(self.autostart.calls, [True])
        self.assertTrue(fake.values["start_with_windows"])
        self.assertTrue(d.autostart_note.isHidden())
        self.assertEqual(self.events, ["background"])
        d.toggles["start_with_windows"].box.click()
        self.assertEqual(self.autostart.calls, [True, False])
        self.assertFalse(fake.values["start_with_windows"])

    def test_a_start_up_entry_that_cannot_be_written_puts_the_box_back_and_says_so(self):
        self.autostart.works = False
        d, fake = self.make()
        d.toggles["start_with_windows"].box.click()
        self.assertFalse(d.toggles["start_with_windows"].box.isChecked())
        self.assertFalse(fake.values["start_with_windows"])
        self.assertFalse(d.autostart_note.isHidden())
        self.assertEqual(d.autostart_note.text(), "Couldn't change the Windows startup entry.")
        apply_language(self.app, "fa")
        d.retranslate()
        self.assertEqual(d.autostart_note.text(), i18n.FA["Couldn't change the Windows startup entry."])

    def test_security_badges_switch_tells_the_window(self):
        d, fake = self.make()
        d.toggles["security_badges"].box.click()
        d.toggles["security_badges"].box.click()
        self.assertEqual(self.events, [("badges", False), ("badges", True)])
        self.assertTrue(fake.values["security_badges"])

    def test_history_switch_and_restore_point_switch_are_only_stored(self):
        d, fake = self.make()
        d.toggles["history_enabled"].box.click()
        d.toggles["cleanup_restore_point"].box.click()
        self.assertEqual((fake.values["history_enabled"], fake.values["cleanup_restore_point"]), (False, False))
        self.assertEqual(self.events, [])

    def test_clear_history_asks_first(self):
        d, _ = self.make()
        with mock.patch.object(settings_ui, "QMessageBox", FakeBox):
            FakeBox.log, FakeBox.pick = [], 1       # Cancel
            d.clear_history_btn.click()
            settings_ui.history.clear.assert_not_called()
            self.assertEqual(self.events, [])
            FakeBox.pick = 0                        # Clear history
            d.clear_history_btn.click()
        settings_ui.history.clear.assert_called_once_with()
        self.assertEqual(self.events, ["history"])
        self.assertEqual(FakeBox.log[0].text, "Clear the whole history?")

    def test_the_backups_button_asks_the_window_to_open_the_dialog(self):
        d, _ = self.make()
        d.backups_btn.click()
        self.assertEqual(self.events, ["backups"])

    def test_the_wave_one_rows_still_come_first(self):
        d, _ = self.make()
        self.assertEqual(d.rows[0].title_text, "Publisher check")
        self.assertEqual([r.title_text for r in d.rows[-4:]],
                         ["Check for updates automatically", "Clear history", "Keep backups for", "Backups"])

    def test_english_and_persian_both_render_with_every_text_translated(self):
        d, _ = self.make()
        d.show()
        texts = [t.box.text() for t in d.toggles.values()] + [t.hint.text() for t in d.toggles.values()] \
            + [d.background_title.text(), d.history_title.text(), d.badges_title.text(), d.cleanup_title.text()] \
            + [r.title.text() for r in d.rows] + [r.hint.text() for r in d.rows]
        self.assertTrue(all(texts))
        apply_language(self.app, "fa")
        d.retranslate()
        persian = [t.box.text() for t in d.toggles.values()] + [t.hint.text() for t in d.toggles.values()] \
            + [d.background_title.text(), d.history_title.text(), d.badges_title.text(), d.cleanup_title.text()] \
            + [r.title.text() for r in d.rows] + [r.hint.text() for r in d.rows]
        self.assertTrue(all(any("\u0600" <= ch <= "\u06ff" for ch in text) for text in persian), [t for t in persian if t.isascii()])
        self.assertEqual(d.check_hours.itemText(d.check_hours.findData(6)), i18n.trn("Every {n} hour", "Every {n} hours", 6))
        self.assertIn(i18n.num(6), d.check_hours.itemText(d.check_hours.findData(6)))
        self.assertEqual(d.clear_history_btn.text(), i18n.FA["Clear history…"])

    def test_the_hint_lines_up_with_the_text_of_its_check_box_in_both_directions(self):
        for lang, rtl in (("en", False), ("fa", True)):
            apply_language(self.app, lang)
            d, _ = self.make()
            d.show()
            self.app.processEvents()
            toggle = d.toggles["tray_enabled"]
            box, hint = toggle.box.geometry(), toggle.hint.geometry()
            if rtl:
                self.assertLessEqual(hint.right(), box.right() - 20, lang)
            else:
                self.assertGreaterEqual(hint.left(), box.left() + 20, lang)
            d.close()

    def test_the_dialog_still_fits_the_screen_and_scrolls(self):
        d, _ = self.make()
        d.show()
        self.app.processEvents()
        room = QApplication.primaryScreen().availableGeometry().height()
        self.assertLessEqual(d.height(), room)

    def test_every_section_reads_in_every_theme(self):
        d, _ = self.make()
        for key in theme.THEMES:
            theme.apply_theme(self.app, key)
            d.show()
            self.app.processEvents()
            self.assertFalse(d.grab().isNull(), key)
            d.hide()


if __name__ == "__main__":
    unittest.main()
