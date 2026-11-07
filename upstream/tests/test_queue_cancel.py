"""Cancelling the install queue: _shell_run stops waiting without killing the installer, and the window
takes queued jobs out at once, stops running ones and never lets one stuck update hold up the rest."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from upstream import i18n, installer
from upstream.context import CancelToken
from upstream.errors import Cancelled
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update
from upstream.ui import window as window_module
from upstream.ui.locale import apply_language
from upstream.ui.window import MainWindow, UiLogHandler

PING = "/c ping -n 4 127.0.0.1 >nul"   # about three seconds, then exits by itself


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        pass

    def is_ignored(self, ident):
        return False

    def skipped_version(self, ident):
        return None

    def override(self, ident):
        return {}

    def set_override(self, ident, value):
        pass


def until(condition, timeout=6.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class ShellRunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def cancel_later(self, token, seconds=0.4):
        timer = threading.Timer(seconds, token.cancel)
        timer.start()
        self.addCleanup(timer.cancel)

    def test_returns_the_exit_code(self):
        self.assertEqual(installer._shell_run("cmd.exe", "/c exit 3", show=0), 3)

    def test_unused_cancel_token_changes_nothing(self):
        self.assertEqual(installer._shell_run("cmd.exe", "/c exit 5", show=0, cancel=CancelToken()), 5)

    def test_cancel_stops_waiting_and_leaves_the_process_running(self):
        marker = Path(self.dir.name) / "finished.txt"
        token = CancelToken()
        self.cancel_later(token)
        started = time.monotonic()
        with self.assertRaises(Cancelled) as raised:
            installer._shell_run("cmd.exe", f'/c ping -n 3 127.0.0.1 >nul & echo done> "{marker}"', show=0, cancel=token)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(raised.exception.template, "Stopped waiting for the installer. It may still be running.")
        self.assertFalse(marker.exists())
        # Nobody killed it: it still gets to the end of its command line.
        self.assertTrue(until(marker.exists, 10))

    def test_a_token_cancelled_beforehand_never_starts_the_installer(self):
        marker = Path(self.dir.name) / "started.txt"
        token = CancelToken()
        token.cancel()
        with self.assertRaises(Cancelled):
            installer._shell_run("cmd.exe", f'/c echo x> "{marker}"', show=0, cancel=token)
        time.sleep(0.5)
        self.assertFalse(marker.exists())

    def test_time_limit_stops_waiting(self):
        started = time.monotonic()
        with self.assertRaises(Cancelled) as raised:
            installer._shell_run("cmd.exe", PING, show=0, timeout_min=0.01)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(raised.exception.values, {"minutes": "0.01"})
        self.assertIn("{minutes} minutes", raised.exception.template)

    def test_no_limit_waits_for_the_process(self):
        self.assertEqual(installer._shell_run("cmd.exe", "/c ping -n 2 127.0.0.1 >nul", show=0, timeout_min=None), 0)

    def test_wait_for_polls_until_finished(self):
        calls = []

        def finished(seconds):
            calls.append(seconds)
            return len(calls) == 3

        installer._wait_for(finished, CancelToken(), timeout_min=60)
        self.assertEqual(len(calls), 3)

    def test_waiting_on_a_child_process_can_be_cancelled(self):
        token = CancelToken()
        self.cancel_later(token, 0.3)
        proc = subprocess.Popen(["cmd.exe", "/c", "ping -n 3 127.0.0.1 >nul"], stdout=subprocess.DEVNULL,
                                creationflags=installer.CREATE_NO_WINDOW)
        self.addCleanup(proc.wait)
        with self.assertRaises(Cancelled):
            installer._wait_for(lambda seconds: installer._exited(proc, seconds), token, None)
        self.assertIsNone(proc.poll())


class InstallWiringTest(unittest.TestCase):
    def tearDown(self):
        installer.configure(None)

    def test_timeout_comes_from_the_settings_on_every_call(self):
        settings = FakeSettings(install_timeout_min=7)
        installer.configure(settings)
        self.assertEqual(installer.timeout_minutes(), 7)
        settings.set("install_timeout_min", 0)
        self.assertEqual(installer.timeout_minutes(), 0)

    def test_bad_values_fall_back_to_the_default(self):
        for value in ("soon", None, [3]):
            installer.configure(FakeSettings(install_timeout_min=value))
            self.assertEqual(installer.timeout_minutes(), installer.DEFAULT_TIMEOUT_MIN)
        installer.configure(FakeSettings(install_timeout_min=-5))
        self.assertEqual(installer.timeout_minutes(), 0)
        installer.configure(None)
        self.assertEqual(installer.timeout_minutes(), installer.DEFAULT_TIMEOUT_MIN)

    def test_install_hands_the_token_and_the_limit_to_the_wait(self):
        installer.configure(FakeSettings(install_timeout_min=9))
        token = CancelToken()
        for kind in ("inno", "msi", "exe"):
            with mock.patch.object(installer, "_shell_run", return_value=0) as run:
                installer.install(Path("setup.exe"), kind, "machine", cancel=token)
            self.assertIs(run.call_args.kwargs["cancel"], token, kind)
            self.assertEqual(run.call_args.kwargs["timeout_min"], 9, kind)

    def test_msix_goes_through_the_same_wait(self):
        installer.configure(FakeSettings(install_timeout_min=9))
        token = CancelToken()
        with mock.patch.object(installer, "_add_appx", return_value=0) as run:
            installer.install(Path("app.msix"), "msix", "user", cancel=token)
        run.assert_called_once_with(Path("app.msix"), token, 9)

    def test_msix_with_a_cancelled_token_does_not_start_powershell(self):
        token = CancelToken()
        token.cancel()
        with mock.patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(Cancelled):
                installer._add_appx(Path("app.msix"), token, None)
        popen.assert_not_called()

    def test_uninstaller_call_shape_still_works(self):
        # uninstaller.py calls _shell_run(file, params) with nothing else.
        self.assertEqual(installer._shell_run("cmd.exe", "/c exit 0", 0), 0)


class FakeUpdater:
    """Stands in for updater.Updater. Scripts per app name: "quick", "polite" (stops when cancelled),
    "stubborn" (ignores the token like winget does)."""

    def __init__(self):
        self.script: dict[str, str] = {}
        self.started: list[str] = []
        self.release = threading.Event()

    def run(self, status, progress, ctx):
        name = status.app.name
        self.started.append(name)
        progress("Downloading", 10)
        mode = self.script.get(name, "quick")
        if mode == "polite":
            while not self.release.wait(0.02):
                ctx.cancel.check()
        elif mode == "stubborn":
            self.release.wait(20)
            progress("Installing", -1)
        return True, "Installed {version}", {"version": "2.0"}, "2.0"


def make_status(name: str) -> AppStatus:
    app = InstalledApp(reg_key=rf"HKLM64\{name}", name=name, version="1.0", publisher="Demo", scope="machine", arch="x64")
    upd = Update("github", "2.0", "o/r", download_url="https://example.invalid/x.exe", file_name="x.exe")
    return AppStatus(app, update=upd)


class FakeBox:
    """QMessageBox that always picks the first button added (Close) or the second (Stay)."""
    pick = 0
    AcceptRole = RejectRole = 0

    def __init__(self, parent=None):
        self.buttons = []

    def __getattr__(self, name):
        return lambda *a, **k: None

    def addButton(self, text, role):
        button = object()
        self.buttons.append(button)
        return button

    def exec(self):
        return 0

    def clickedButton(self):
        return self.buttons[FakeBox.pick]


class WindowQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.settings = FakeSettings()

    def setUp(self):
        apply_language(self.app, "en")
        self.updater = FakeUpdater()
        self.win = MainWindow(self.settings, object())
        self.win.updater = self.updater
        self.statuses = {n: make_status(n) for n in ("Alpha", "Beta", "Gamma")}
        self.win.statuses = {s.uid: s for s in self.statuses.values()}
        self.win.last_scan = datetime.now()
        self.win.populate()
        patcher = mock.patch.object(window_module, "WIND_DOWN", 0.4)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.updater.release.set()
        self.win.cancel_all_jobs()
        for handler in [h for h in logging.getLogger("upstream").handlers if isinstance(h, UiLogHandler)]:
            logging.getLogger("upstream").removeHandler(handler)
        apply_language(self.app, "en")

    # -- helpers
    def uid(self, name):
        return self.statuses[name].uid

    def pump(self, condition, timeout=6.0) -> bool:
        def check():
            self.app.processEvents()
            return condition()
        return until(check, timeout)

    def rows(self, name):
        return self.win.rows[self.uid(name)]

    def state(self, name):
        return self.win.job_state[self.uid(name)]

    def update(self, name):
        self.win.request_update(self.uid(name))

    # -- tests
    def test_every_queued_or_running_row_offers_cancel(self):
        self.updater.script["Alpha"] = "polite"
        self.update("Alpha")
        self.update("Beta")
        self.assertEqual(self.state("Beta"), ("queued",))
        for row in self.rows("Beta"):
            self.assertFalse(row.cancel_btn.isHidden())
            self.assertEqual(row.cancel_btn.text(), "Cancel")
        self.assertTrue(self.pump(lambda: self.win.job_state[self.uid("Alpha")][0] == "progress"))
        for row in self.rows("Alpha"):
            self.assertFalse(row.cancel_btn.isHidden())
            self.assertEqual(row.cancel_btn.text(), "Cancel")
        self.assertTrue(self.win.cancel_all_btn.isEnabled())

    def test_the_installing_phase_says_stop_waiting(self):
        self.update("Alpha")
        for template in ("Installing", "Setup window opened"):
            self.win._set_state(self.uid("Alpha"), ("progress", template, {}, -1))
            for row in self.rows("Alpha"):
                self.assertEqual(row.cancel_btn.text(), "Stop waiting", template)
                self.assertNotEqual(row.cancel_btn.toolTip(), "")
        self.win._set_state(self.uid("Alpha"), ("progress", "Downloading", {}, 40))
        self.assertEqual(self.rows("Alpha")[0].cancel_btn.text(), "Cancel")

    def test_install_phases_are_the_ones_the_updater_reports(self):
        source = (Path(__file__).resolve().parents[1] / "updater.py").read_text("utf-8")
        for phase in window_module.INSTALL_PHASES:
            self.assertIn(f'"{phase}"', source)

    def test_a_recheck_has_nothing_to_cancel(self):
        self.win._set_state(self.uid("Alpha"), ("progress", "Rechecking", {}, -1))
        self.assertTrue(self.rows("Alpha")[0].cancel_btn.isHidden())

    def test_cancelling_a_queued_row_takes_effect_at_once(self):
        self.updater.script["Alpha"] = "polite"
        for name in ("Alpha", "Beta"):
            self.update(name)
        self.assertTrue(self.pump(lambda: self.updater.started == ["Alpha"]))
        self.rows("Beta")[0].cancel_btn.click()          # no event loop between the click and the check
        self.assertEqual(self.state("Beta")[0], "cancelled")
        self.assertNotIn(self.uid("Beta"), self.win.active_jobs)
        self.assertEqual(self.rows("Beta")[0].button.text(), "Update")
        self.assertFalse(self.rows("Beta")[0].note.isHidden())
        self.updater.release.set()
        self.assertTrue(self.pump(lambda: self.state("Alpha")[0] == "result"))
        time.sleep(0.3)
        self.app.processEvents()
        self.assertEqual(self.updater.started, ["Alpha"])  # Beta was skipped, not run
        self.assertEqual(self.state("Beta")[0], "cancelled")

    def test_cancelling_a_running_update_ends_neutral_and_keeps_the_update_offered(self):
        self.updater.script["Alpha"] = "polite"
        self.update("Alpha")
        self.assertTrue(self.pump(lambda: self.updater.started == ["Alpha"]))
        self.win.cancel_job(self.uid("Alpha"))
        self.assertEqual(self.state("Alpha"), ("cancelling",))
        self.assertTrue(self.rows("Alpha")[0].cancel_btn.isHidden())
        self.assertTrue(self.pump(lambda: self.state("Alpha")[0] == "cancelled"))
        row = self.rows("Alpha")[0]
        self.assertEqual(row.note.text(), "Cancelled")
        self.assertEqual(row.button.text(), "Update")
        self.assertIsNotNone(self.statuses["Alpha"].update)
        self.assertNotIn(self.uid("Alpha"), self.win.active_jobs)
        self.assertIn(self.uid("Alpha"), self.win._pending_updates())
        self.assertFalse(self.win.cancel_all_btn.isEnabled())

    def test_progress_after_cancelling_does_not_undo_the_cancelling_row(self):
        self.updater.script["Alpha"] = "polite"
        self.update("Alpha")
        self.assertTrue(self.pump(lambda: self.updater.started == ["Alpha"]))
        self.win.cancel_job(self.uid("Alpha"))
        self.win.bus.job_progress.emit(self.uid("Alpha"), "Downloading", {}, 50)  # a late event already in flight
        self.app.processEvents()
        self.assertIn(self.state("Alpha")[0], ("cancelling", "cancelled"))

    def test_a_cancelled_update_can_be_started_again(self):
        self.updater.script["Alpha"] = "polite"
        self.update("Alpha")
        self.assertTrue(self.pump(lambda: self.updater.started == ["Alpha"]))
        self.win.cancel_job(self.uid("Alpha"))
        self.assertTrue(self.pump(lambda: self.state("Alpha")[0] == "cancelled"))
        self.updater.script["Alpha"] = "quick"
        self.update("Alpha")
        self.assertTrue(self.pump(lambda: self.state("Alpha")[0] == "result"))
        self.assertTrue(self.state("Alpha")[1])

    def test_an_update_that_ignores_cancel_does_not_hold_up_the_queue(self):
        self.updater.script["Alpha"] = "stubborn"
        self.update("Alpha")
        self.update("Beta")
        self.assertTrue(self.pump(lambda: self.updater.started == ["Alpha"]))
        self.win.cancel_job(self.uid("Alpha"))
        self.assertTrue(self.pump(lambda: self.state("Beta")[0] == "result", 6))
        self.assertEqual(self.state("Alpha")[0], "cancelled")
        self.assertEqual(self.updater.started, ["Alpha", "Beta"])
        self.updater.release.set()      # the abandoned update finishes later and must stay silent
        time.sleep(0.3)
        self.app.processEvents()
        self.assertEqual(self.state("Alpha")[0], "cancelled")

    def test_cancel_all_stops_everything(self):
        self.updater.script.update(Alpha="polite", Beta="polite", Gamma="polite")
        for name in self.statuses:
            self.update(name)
        self.assertTrue(self.pump(lambda: self.updater.started == ["Alpha"]))
        self.win.cancel_all_jobs()
        self.assertEqual(self.state("Beta")[0], "cancelled")
        self.assertEqual(self.state("Gamma")[0], "cancelled")
        self.assertTrue(self.pump(lambda: not self.win.active_jobs and self.state("Alpha")[0] == "cancelled"))
        self.assertEqual(self.updater.started, ["Alpha"])
        self.assertFalse(self.win.cancel_all_btn.isEnabled())

    def test_the_cancel_all_button_cancels(self):
        self.updater.script.update(Alpha="polite", Beta="polite")
        self.update("Alpha")
        self.update("Beta")
        self.assertTrue(self.win.cancel_all_btn.isEnabled())
        self.win.cancel_all_btn.click()
        self.assertTrue(self.pump(lambda: not self.win.active_jobs))

    def test_a_failure_is_still_a_failure(self):
        self.updater.run = lambda status, progress, ctx: (False, "Installer exited with code {code}", {"code": 5}, "1.0")
        self.update("Alpha")
        self.assertTrue(self.pump(lambda: self.state("Alpha")[0] == "result"))
        self.assertEqual(self.rows("Alpha")[0].button.text(), "Retry")
        self.assertTrue(self.rows("Alpha")[0].note.isHidden())

    def test_states_survive_the_rows_being_rebuilt_and_a_language_switch(self):
        self.updater.script["Alpha"] = "polite"
        for name in ("Alpha", "Beta", "Gamma"):
            self.update(name)
        self.assertTrue(self.pump(lambda: self.state("Alpha")[0] == "progress"))
        self.win.cancel_job(self.uid("Gamma"))
        self.win._set_state(self.uid("Alpha"), ("progress", "Installing", {}, -1))
        apply_language(self.app, "fa")
        self.win.retranslate()
        self.assertEqual(self.win.cancel_all_btn.text(), i18n.FA["Cancel all"])
        self.assertEqual(self.rows("Alpha")[0].cancel_btn.text(), i18n.FA["Stop waiting"])
        self.assertEqual(self.rows("Beta")[0].cancel_btn.text(), i18n.FA["Cancel"])
        self.assertEqual(self.rows("Gamma")[0].note.text(), i18n.FA["Cancelled"])
        self.assertTrue(self.app.layoutDirection().name == "RightToLeft")
        apply_language(self.app, "en")
        self.win.retranslate()
        self.assertEqual(self.rows("Alpha")[0].cancel_btn.text(), "Stop waiting")
        self.assertEqual(self.rows("Gamma")[0].note.text(), "Cancelled")

    def test_closing_the_window_cancels_what_is_left(self):
        self.updater.script.update(Alpha="polite", Beta="polite")
        self.update("Alpha")
        self.update("Beta")
        tokens = list(self.win.cancels.values())
        with mock.patch.object(window_module, "QMessageBox", FakeBox):
            FakeBox.pick = 1     # Stay
            event = QCloseEvent()
            self.win.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertFalse(any(t.is_set for t in tokens))
            FakeBox.pick = 0     # Close
            event = QCloseEvent()
            self.win.closeEvent(event)
            self.assertTrue(event.isAccepted())
        self.assertTrue(all(t.is_set for t in tokens))


if __name__ == "__main__":
    unittest.main()
