"""Background: the schedule, notifications, automatic updates, the tray and the start-with-Windows entry.
Offscreen Qt with a fake window and a fake clock. The only registry write is an HKCU Run value with a throwaway
name (UpstreamTest_<random>) that the test removes in a finally; the real "Upstream" value is never touched."""
from __future__ import annotations

import copy
import inspect
import math
import os
import sys
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer                                             # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon, QWidget  # noqa: E402

from upstream import background, i18n, net                                      # noqa: E402
from upstream.background import Background                                     # noqa: E402
from upstream.inventory import InstalledApp                                     # noqa: E402
from upstream.running import RunningProcess                                     # noqa: E402
from upstream.scanner import AppStatus, Update                                  # noqa: E402
from upstream.ui.locale import apply_language                                   # noqa: E402
from upstream.ui.tray import TrayIcon                                           # noqa: E402

app = QApplication.instance() or QApplication([])
EPOCH = 1_700_000_000.0
HOUR = 3600.0


class FakeSettings:
    def __init__(self, **over):
        self.data = {"tray_enabled": False, "close_to_tray": False, "check_interval_hours": 6,
                     "notify_updates": True, "auto_update": []}
        self.data.update(over)
        self.saves = 0
        self.fail: OSError | None = None

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        if self.fail:
            raise self.fail
        self.saves += 1


class Clock:
    def __init__(self, now: float = EPOCH):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeWindow(QWidget):
    """The MainWindow API that Background documents."""

    def __init__(self):
        super().__init__()
        self.statuses: dict[str, AppStatus] = {}
        self.busy = False
        self.active = False
        self.scans = 0
        self.raised = 0
        self.minimized = 0
        self.quits = 0
        self.update_alls = 0
        self.requests: list[tuple] = []
        self.fail_requests: set[str] = set()
        self.reject: set[str] = set()       # request_update answers False: the queue would not take it now

    def add(self, status: AppStatus) -> AppStatus:
        self.statuses[status.uid] = status
        return status

    def pending_updates(self):
        return [uid for uid, s in self.statuses.items() if s.update]

    def is_busy(self):
        return self.busy

    def start_scan(self):
        self.scans += 1

    def show_and_raise(self):
        self.raised += 1
        self.show()

    def showMinimized(self):
        self.minimized += 1

    def quit_app(self):
        self.quits += 1

    def update_all(self):
        self.update_alls += 1

    def request_update(self, uid, bulk=False, auto=False):
        if uid in self.fail_requests:
            raise RuntimeError("queue is broken")
        self.requests.append((uid, bulk, auto))
        return uid not in self.reject

    def isActiveWindow(self):
        return self.active


class FakeTray:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.refreshed = 0

    def notify(self, title, body):
        self.messages.append((title, body))

    def refresh(self):
        self.refreshed += 1

    def isVisible(self):
        return True


def make_status(name: str, version: str = "2.0", *, update: bool = True, pin=None, source: str = "github",
                download_url: str | None = "https://example.invalid/setup.exe", fallbacks=None,
                scope: str = "user", repo: str | None = None, note: str = "") -> AppStatus:
    installed = InstalledApp(reg_key=f"HKCU\\{name}", name=name, version="1.0", publisher="Pub", scope=scope,
                             arch="x64")
    upd = Update(source, version, "owner/repo", download_url=download_url) if update else None
    return AppStatus(installed, update=upd, pin=pin, fallbacks=fallbacks or [], repo=repo, note=note)


def pump():
    for _ in range(5):
        app.processEvents()


def plain(text: str) -> str:
    """Without the bidi isolates that tr() puts around values in Persian."""
    return text.replace("⁨", "").replace("⁩", "")


class BackgroundCase(unittest.TestCase):
    interval = 6

    def setUp(self):
        self.addCleanup(self._reset)
        patch = mock.patch.object(background, "_tray_available", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)
        self.window = FakeWindow()
        self.addCleanup(self.window.deleteLater)
        self.settings = FakeSettings(check_interval_hours=self.interval)
        self.clock = Clock()
        self.bg = Background(self.window, self.settings, self.clock)

    @staticmethod
    def _reset():
        apply_language(app, "en")
        app.setQuitOnLastWindowClosed(True)

    def advance(self, seconds: float, step: float = 60.0):
        """Move the clock and fire the one-minute tick at every step, like the real timer."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now = min(self.clock.now + step, end)
            self.bg._tick()


class ScheduleTest(BackgroundCase):
    def test_off_by_default(self):
        self.settings.data["check_interval_hours"] = 0
        self.bg.start()
        self.advance(3 * 24 * HOUR, step=HOUR)
        self.assertEqual(self.window.scans, 0)

    def test_nonsense_intervals_never_scan(self):
        self.bg.start()
        for value in (True, "6", None, -1, 0, math.nan, math.inf, 10 ** 9, [6]):
            with self.subTest(value=value):
                self.settings.data["check_interval_hours"] = value
                self.advance(24 * HOUR, step=HOUR)
        self.assertEqual(self.window.scans, 0)

    def test_waits_after_start_then_checks(self):
        self.bg.start()
        self.advance(60)
        self.assertEqual(self.window.scans, 0)       # a check has just run at start-up
        self.advance(background.STARTUP_GRACE)
        self.assertEqual(self.window.scans, 1)       # never checked before: due as soon as the grace is over

    def test_not_due_until_the_interval_has_passed(self):
        self.settings.data["last_check_ts"] = self.clock.now - HOUR
        self.bg.start()
        self.advance(5 * HOUR - 120)
        self.assertEqual(self.window.scans, 0)
        self.advance(240)
        self.assertEqual(self.window.scans, 1)

    def test_unfinished_check_is_retried_not_hammered(self):
        self.bg.start()
        self.advance(3 * 60)
        self.assertEqual(self.window.scans, 1)
        self.advance(background.RETRY_AFTER - 120)
        self.assertEqual(self.window.scans, 1)
        self.advance(240)
        self.assertEqual(self.window.scans, 2)

    def test_finished_check_resets_the_schedule(self):
        self.bg.start()
        self.advance(3 * 60)
        self.assertEqual(self.window.scans, 1)
        self.bg.scan_finished()
        self.assertEqual(self.settings.data["last_check_ts"], self.clock.now)
        self.advance(6 * HOUR - 300)
        self.assertEqual(self.window.scans, 1)
        self.advance(600)
        self.assertEqual(self.window.scans, 2)

    def test_busy_window_postpones_the_check(self):
        self.window.busy = True
        self.bg.start()
        self.advance(10 * 60)
        self.assertEqual(self.window.scans, 0)
        self.window.busy = False
        self.advance(60)
        self.assertEqual(self.window.scans, 1)

    def test_waits_a_moment_after_sleep(self):
        self.settings.data["last_check_ts"] = self.clock.now
        self.bg.start()
        self.advance(5 * 60)
        self.clock.now += 7 * HOUR                    # the PC slept
        self.bg._tick()
        self.assertEqual(self.window.scans, 0)
        self.advance(60)
        self.assertEqual(self.window.scans, 0)        # the network is probably not back yet
        self.advance(60)
        self.assertEqual(self.window.scans, 1)

    def test_a_dialog_that_is_open_postpones_the_check(self):
        # The dialog's own event loop keeps the timer firing, and a scan that finishes under it replaces the statuses
        # the dialog is working on.
        dialog = QDialog()
        dialog.setModal(True)
        dialog.show()
        self.addCleanup(dialog.deleteLater)
        pump()
        self.assertIs(QApplication.activeModalWidget(), dialog)
        self.bg.start()
        self.advance(10 * 60)
        self.assertEqual(self.window.scans, 0)
        self.assertEqual(self.bg._skip_reason(self.clock.now), "a dialog is open")
        dialog.close()
        pump()
        self.advance(60)
        self.assertEqual(self.window.scans, 1)

    def test_a_clock_set_back_does_not_freeze_the_start_up_grace(self):
        self.bg.start()
        self.advance(60)
        self.clock.now -= HOUR                        # the user (or the time service) put the clock back
        self.bg._tick()
        self.advance(background.STARTUP_GRACE + 60)
        self.assertEqual(self.window.scans, 1)

    def test_a_clock_set_back_does_not_freeze_the_retry_wait(self):
        self.bg.start()
        self.advance(3 * 60)
        self.assertEqual(self.window.scans, 1)        # an attempt that never finishes
        self.clock.now -= HOUR
        self.bg._tick()
        self.advance(background.RETRY_AFTER + 120)
        self.assertEqual(self.window.scans, 2)

    def test_a_clock_set_back_does_not_extend_the_wake_up_pause(self):
        self.settings.data["last_check_ts"] = self.clock.now
        self.bg.start()
        self.advance(5 * 60)
        self.clock.now += 7 * HOUR                    # the PC slept: a check is due, after a short pause
        self.bg._tick()
        self.assertEqual(self.window.scans, 0)
        self.clock.now -= HOUR                        # and woke up to a clock that was an hour ahead
        self.bg._tick()
        self.advance(background.RESUME_DELAY + 120)
        self.assertEqual(self.window.scans, 1)

    def test_stamp_from_the_future_counts_as_never(self):
        self.settings.data["last_check_ts"] = self.clock.now + 30 * 24 * HOUR
        self.bg.start()
        self.advance(3 * 60)
        self.assertEqual(self.window.scans, 1)

    def test_unreadable_stamp_counts_as_never(self):
        for value in ("yesterday", None, True, math.nan, [1]):
            with self.subTest(value=value):
                self.settings.data["last_check_ts"] = value
                self.assertEqual(self.bg._last_check(self.clock.now), -math.inf)

    def test_a_failing_scan_does_not_break_the_timer(self):
        self.window.start_scan = mock.Mock(side_effect=RuntimeError("boom"))
        self.bg.start()
        with self.assertLogs("upstream.background", "ERROR"):
            self.advance(3 * 60)

    def test_scan_finished_survives_a_settings_save_error(self):
        self.settings.fail = PermissionError("read-only")
        with self.assertLogs("upstream.background", "WARNING"):
            self.bg.scan_finished()
        self.assertEqual(self.settings.data["last_check_ts"], self.clock.now)

    def test_timer_follows_the_settings(self):
        self.bg.start()
        self.assertTrue(self.bg.timer.isActive())
        self.settings.data["check_interval_hours"] = 0
        self.bg.settings_changed()
        self.assertFalse(self.bg.timer.isActive())       # no schedule and no tray: nothing to do every minute
        self.settings.data["check_interval_hours"] = 12
        self.bg.settings_changed()
        self.assertTrue(self.bg.timer.isActive())
        self.bg.stop()
        self.assertFalse(self.bg.timer.isActive())


class GithubUnreachableTest(BackgroundCase):
    """A check that mostly could not reach GitHub (Wi-Fi not up yet after a start at login) is not "done"."""

    def add(self, count: int, failed: int, note: str = "GitHub was unreachable"):
        for i in range(count):
            self.window.add(make_status(f"App{i}", update=False, repo=f"o/app{i}", note=note if i < failed else ""))

    def test_most_apps_blind_means_no_stamp_and_a_retry_after_the_usual_wait(self):
        self.add(4, 2)                                 # exactly half
        self.bg.start()
        self.advance(3 * 60)
        self.assertEqual(self.window.scans, 1)
        with self.assertLogs("upstream.background", "INFO"):
            self.bg.scan_finished()
        self.assertNotIn("last_check_ts", self.settings.data)
        self.advance(background.RETRY_AFTER - 300)
        self.assertEqual(self.window.scans, 1)
        self.advance(600)
        self.assertEqual(self.window.scans, 2)

    def test_the_rate_limit_note_counts_too(self):
        self.add(3, 3, "GitHub rate limit reached; using winget")
        self.bg.scan_finished()
        self.assertNotIn("last_check_ts", self.settings.data)

    def test_fewer_than_half_blind_is_a_finished_check(self):
        self.add(4, 1)
        self.bg.scan_finished()
        self.assertEqual(self.settings.data["last_check_ts"], self.clock.now)

    def test_apps_without_a_repository_do_not_count(self):
        self.window.add(make_status("Plain", update=False, note="GitHub was unreachable"))     # no repo
        self.bg.scan_finished()
        self.assertEqual(self.settings.data["last_check_ts"], self.clock.now)

    def test_a_scan_with_no_apps_at_all_is_a_finished_check(self):
        self.bg.scan_finished()
        self.assertEqual(self.settings.data["last_check_ts"], self.clock.now)

    def test_blind_checks_in_a_row_stop_counting_so_a_dead_repository_cannot_loop(self):
        self.add(2, 2)
        for _ in range(background.MAX_BLIND_SCANS - 1):
            self.bg.scan_finished()
            self.assertNotIn("last_check_ts", self.settings.data)
        self.bg.scan_finished()
        self.assertEqual(self.settings.data["last_check_ts"], self.clock.now)

    def test_a_good_check_starts_the_count_again(self):
        self.add(2, 2)
        self.bg.scan_finished()
        self.bg.scan_finished()
        self.window.statuses.clear()
        self.bg.scan_finished()                        # reached GitHub this time
        self.add(2, 2)
        self.settings.data.pop("last_check_ts")
        self.bg.scan_finished()
        self.assertNotIn("last_check_ts", self.settings.data)


class NotificationTest(BackgroundCase):
    def setUp(self):
        super().setUp()
        self.tray = self.bg.tray = FakeTray()

    def test_one_update(self):
        self.window.add(make_status("Alpha", "2.5"))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [("1 update ready", "Alpha 2.5 is ready to install")])

    def test_each_app_and_version_is_announced_once(self):
        alpha = self.window.add(make_status("Alpha", "2.5"))
        self.bg.scan_finished()
        self.bg.scan_finished()
        self.assertEqual(len(self.tray.messages), 1)
        self.window.add(make_status("Bravo", "1.1"))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages[-1], ("1 update ready", "Bravo 1.1 is ready to install"))
        alpha.update.version = "2.6"
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages[-1], ("1 update ready", "Alpha 2.6 is ready to install"))
        self.bg.scan_finished()
        self.assertEqual(len(self.tray.messages), 3)

    def test_several_apps_in_one_message(self):
        for name in ("Echo", "Alpha", "Delta", "Charlie", "Bravo"):
            self.window.add(make_status(name))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [("5 updates ready", "Alpha, Bravo, Charlie and 2 more")])

    def test_three_apps_list_all_names(self):
        for name in ("Charlie", "Alpha", "Bravo"):
            self.window.add(make_status(name))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [("3 updates ready", "Alpha, Bravo, Charlie")])

    def test_nothing_pending_nothing_said(self):
        self.window.add(make_status("Alpha", update=False))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [])

    def test_switched_off(self):
        self.settings.data["notify_updates"] = False
        self.window.add(make_status("Alpha"))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [])
        self.assertNotIn("notified", self.settings.data)
        self.settings.data["notify_updates"] = True
        self.bg.scan_finished()
        self.assertEqual(len(self.tray.messages), 1)     # turned on later: the waiting update is announced

    def test_open_window_gets_no_message_and_the_update_counts_as_seen(self):
        self.window.active = True
        self.window.add(make_status("Alpha"))
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [])
        self.window.active = False
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [])

    def test_remembered_list_is_trimmed_to_the_newest(self):
        self.settings.data["notified"] = [f"old{i}@1" for i in range(background.MAX_REMEMBERED + 50)]
        self.window.add(make_status("Alpha", "9.9"))
        self.bg.scan_finished()
        kept = self.settings.data["notified"]
        self.assertEqual(len(kept), background.MAX_REMEMBERED)
        self.assertEqual(kept[-1], "alpha@9.9")
        self.assertEqual(kept[0], f"old{50 + 1}@1")

    def test_damaged_setting_is_ignored(self):
        for value in ("text", 5, {"alpha": "2.0"}, [1, None, "x@1"]):
            with self.subTest(value=value):
                self.settings.data["notified"] = value
                self.tray.messages.clear()
                self.window.statuses.clear()
                self.window.add(make_status("Alpha", "2.0"))
                self.bg.scan_finished()
                self.assertEqual(len(self.tray.messages), 1)

    def test_persian(self):
        apply_language(app, "fa")
        for name, version in (("Alpha", "2.5"), ("Bravo", "1.0"), ("Charlie", "1.0"), ("Delta", "1.0")):
            self.window.add(make_status(name, version))
        self.bg.scan_finished()
        title, body = map(plain, self.tray.messages[0])
        self.assertEqual(title, i18n.FA["{n} updates ready"].format(n=i18n.num(4)))
        self.assertEqual(body, i18n.FA["{names} and {n} more"].format(
            names=i18n.FA[", "].join(["Alpha", "Bravo", "Charlie"]), n=i18n.num(1)))
        self.assertRegex(title + body, r"[\u0600-\u06ff]")
        self.tray.messages.clear()
        self.window.statuses.clear()
        self.window.add(make_status("Echo", "3.1"))
        self.bg.scan_finished()
        self.assertEqual(plain(self.tray.messages[0][1]),
                         i18n.FA["{name} {version} is ready to install"].format(name="Echo", version="3.1"))

    def test_without_a_tray_icon_nothing_is_shown(self):
        self.bg.tray = None
        with self.assertLogs("upstream.background", "INFO") as logs:
            self.assertFalse(self.bg.notify("Title", "Body"))
        self.assertIn("Title", logs.output[0])
        self.window.add(make_status("Alpha"))
        self.bg.scan_finished()                          # must not raise

    def test_scan_finished_refreshes_the_tray(self):
        self.bg.scan_finished()
        self.assertGreaterEqual(self.tray.refreshed, 1)


class AutoUpdateTest(BackgroundCase):
    def setUp(self):
        super().setUp()
        self.tray = self.bg.tray = FakeTray()
        self.settings.data["auto_update"] = ["alpha", "charlie", "delta", "echo", "foxtrot"]
        self.running: dict[str, list[str]] = {}
        patch = mock.patch.object(background.running, "find_running", side_effect=self._find_running)
        self.find = patch.start()
        self.addCleanup(patch.stop)

    def _find_running(self, installed):
        names = self.running.get(installed.name, [])
        return [RunningProcess(100 + i, n, f"C:\\{n}", 1) for i, n in enumerate(names)]

    def test_picks_only_the_apps_that_may_update_unattended(self):
        alpha = self.window.add(make_status("Alpha"))
        self.window.add(make_status("Bravo"))                                # not in auto_update
        self.window.add(make_status("Charlie", pin={"mode": "hold", "version": "1.0"}))
        self.window.add(make_status("Delta"))
        self.running["Delta"] = ["delta.exe"]
        self.window.add(make_status("Echo", update=False))                   # nothing to install
        self.bg.scan_finished()
        self.assertEqual(self.window.requests, [(alpha.uid, True, True)])

    def test_every_skip_is_logged_with_its_reason(self):
        self.window.add(make_status("Charlie", pin={"mode": "major", "version": "1.0"}))
        self.window.add(make_status("Delta"))
        self.running["Delta"] = ["delta.exe", "delta.exe", "helper.exe"]
        self.window.add(make_status("Foxtrot", source="site", download_url=None))
        with self.assertLogs("upstream.background", "INFO") as logs:
            self.bg.scan_finished()
        text = "\n".join(logs.output)
        self.assertIn("Charlie 2.0 skipped: pinned", text)
        self.assertIn("Delta 2.0 skipped: running: delta.exe, helper.exe", text)
        self.assertIn("Foxtrot 2.0 skipped: needs a manual download", text)
        self.assertEqual(self.window.requests, [])

    def test_a_site_update_with_a_fallback_is_fine(self):
        fox = self.window.add(make_status("Foxtrot", source="site", download_url=None,
                                          fallbacks=[Update("winget", "2.0", "Fox.Trot")]))
        self.bg.scan_finished()
        self.assertEqual(self.window.requests, [(fox.uid, True, True)])

    def test_apps_that_install_by_themselves_are_not_announced(self):
        self.window.add(make_status("Alpha"))
        self.window.add(make_status("Bravo"))
        self.window.add(make_status("Delta"))
        self.running["Delta"] = ["delta.exe"]                    # skipped, so the user has to hear about it
        self.bg.scan_finished()
        self.assertEqual(self.tray.messages, [("2 updates ready", "Bravo, Delta")])

    def test_one_attempt_per_version(self):
        alpha = self.window.add(make_status("Alpha"))
        self.bg.scan_finished()
        self.assertEqual(len(self.window.requests), 1)
        self.assertEqual(self.tray.messages, [])
        self.bg.scan_finished()                                  # still offered: the install did not happen
        self.assertEqual(len(self.window.requests), 1)
        self.assertEqual(self.tray.messages, [("1 update ready", "Alpha 2.0 is ready to install")])
        alpha.update.version = "2.1"
        self.bg.scan_finished()
        self.assertEqual(len(self.window.requests), 2)

    def test_switched_off_notifications_do_not_stop_automatic_updates(self):
        self.settings.data["notify_updates"] = False
        self.window.add(make_status("Alpha"))
        self.bg.scan_finished()
        self.assertEqual(len(self.window.requests), 1)

    def test_running_check_that_fails_means_not_now(self):
        self.window.add(make_status("Alpha"))
        echo = self.window.add(make_status("Echo"))
        self.find.side_effect = [OSError("snapshot failed"), []]
        with self.assertLogs("upstream.background", "INFO") as logs:
            self.bg.scan_finished()
        self.assertIn("could not tell whether it is running", "\n".join(logs.output))
        self.assertEqual(self.window.requests, [(echo.uid, True, True)])

    def test_a_request_that_fails_does_not_stop_the_rest(self):
        alpha = self.window.add(make_status("Alpha"))
        echo = self.window.add(make_status("Echo"))
        self.window.fail_requests = {alpha.uid}
        with self.assertLogs("upstream.background", "ERROR"):
            self.bg.scan_finished()
        self.assertEqual(self.window.requests, [(echo.uid, True, True)])

    def test_nothing_without_the_setting(self):
        self.window.add(make_status("Alpha"))
        for value in ([], None, "alpha", {"alpha": True}):
            with self.subTest(value=value):
                self.settings.data["auto_update"] = value
                self.bg.scan_finished()
        self.assertEqual(self.window.requests, [])
        self.find.assert_not_called()

    def test_a_pin_set_after_the_scan_still_holds(self):
        # The status the scan produced has no pin, but the user pinned the app while the scan was running.
        self.window.add(make_status("Alpha"))
        self.settings.data["pins"] = {"alpha": {"mode": "hold", "version": "1.0"}}
        with self.assertLogs("upstream.background", "INFO") as logs:
            self.bg.scan_finished()
        self.assertIn("Alpha 2.0 skipped: pinned", "\n".join(logs.output))
        self.assertEqual(self.window.requests, [])

    def test_a_machine_wide_update_needs_an_elevated_upstream(self):
        alpha = self.window.add(make_status("Alpha", scope="machine"))
        user = self.window.add(make_status("Charlie"))
        with mock.patch.object(background.elevation, "is_elevated", return_value=False):
            with self.assertLogs("upstream.background", "INFO") as logs:
                self.bg.scan_finished()
        self.assertIn("Alpha 2.0 skipped: needs administrator rights", "\n".join(logs.output))
        self.assertEqual(self.window.requests, [(user.uid, True, True)])
        self.assertEqual(self.tray.messages, [("1 update ready", "Alpha 2.0 is ready to install")])
        with mock.patch.object(background.elevation, "is_elevated", return_value=True):
            self.bg.scan_finished()
        self.assertIn((alpha.uid, True, True), self.window.requests)

    def test_a_store_or_per_user_update_never_needs_the_prompt(self):
        self.window.add(make_status("Charlie"))
        with mock.patch.object(background.elevation, "is_elevated", return_value=False):
            self.bg.scan_finished()
        self.assertEqual(len(self.window.requests), 1)

    def test_a_request_the_queue_refused_is_not_tried_for_good(self):
        alpha = self.window.add(make_status("Alpha"))
        self.window.reject = {alpha.uid}                         # an uninstall is running
        self.bg.scan_finished()
        self.assertEqual(self.window.requests, [(alpha.uid, True, True)])
        self.assertEqual(self.settings.data.get("auto_tried", []), [])
        self.assertEqual(self.tray.messages, [("1 update ready", "Alpha 2.0 is ready to install")])   # not silent
        self.window.reject = set()
        self.bg.scan_finished()
        self.assertEqual(len(self.window.requests), 2)                                                 # tried again
        self.assertEqual(self.settings.data["auto_tried"], ["alpha@2.0"])

    def test_a_request_that_raised_is_not_tried_for_good_either(self):
        alpha = self.window.add(make_status("Alpha"))
        self.window.fail_requests = {alpha.uid}
        with self.assertLogs("upstream.background", "ERROR"):
            self.bg.scan_finished()
        self.assertEqual(self.settings.data.get("auto_tried", []), [])

    def test_only_the_accepted_updates_are_remembered(self):
        alpha = self.window.add(make_status("Alpha"))
        echo = self.window.add(make_status("Echo"))
        self.window.reject = {alpha.uid}
        self.bg.scan_finished()
        self.assertEqual(self.settings.data["auto_tried"], ["echo@2.0"])
        self.assertIn(echo.uid, self.bg._auto_keys)

    def test_a_metered_or_battery_refusal_gives_the_version_another_chance(self):
        refusals = [net.REFUSED_METERED, net.REFUSED_BATTERY, net.REFUSED_BATTERY_SAVER]
        self.assertEqual(set(refusals), set(net.PREFLIGHT_REFUSALS))
        for template in refusals:
            with self.subTest(template):
                self.settings.data["auto_tried"] = []
                self.window.requests.clear()
                alpha = self.window.add(make_status("Alpha"))
                self.bg.scan_finished()
                self.assertEqual(self.settings.data["auto_tried"], ["alpha@2.0"])
                self.bg.auto_finished(alpha.uid, False, template)
                self.assertEqual(self.settings.data["auto_tried"], [])
                self.bg.scan_finished()
                self.assertEqual(len(self.window.requests), 2)

    def test_any_other_end_keeps_the_version_tried(self):
        for ok, template in [(True, "Installed {version}"), (False, "Install failed"),
                             (False, "Update cancelled: {name} is still running."), (False, ""), (False, None),
                             (False, "Not updated: the battery is at 5%")]:
            with self.subTest(ok=ok, template=template):
                self.settings.data["auto_tried"] = []
                self.window.requests.clear()
                alpha = self.window.add(make_status("Alpha"))
                self.bg.scan_finished()
                self.bg.auto_finished(alpha.uid, ok, template)
                self.assertEqual(self.settings.data["auto_tried"], ["alpha@2.0"])
                self.bg.scan_finished()
                self.assertEqual(len(self.window.requests), 1)

    def test_auto_finished_ignores_uids_it_did_not_queue(self):
        alpha = self.window.add(make_status("Alpha"))
        bravo = self.window.add(make_status("Bravo"))               # not in auto_update: the user's own update
        self.bg.scan_finished()
        before = dict(self.settings.data)
        self.bg.auto_finished(bravo.uid, False, net.REFUSED_METERED)
        self.bg.auto_finished("HKCU\\Nobody", False, net.REFUSED_METERED)
        self.bg.auto_finished("", True, "")
        self.assertEqual(self.settings.data, before)
        self.bg.auto_finished(alpha.uid, False, net.REFUSED_METERED)
        self.bg.auto_finished(alpha.uid, False, net.REFUSED_METERED)         # a second call does nothing more
        self.assertEqual(self.settings.data["auto_tried"], [])

    def test_a_refusal_only_removes_its_own_version(self):
        alpha = self.window.add(make_status("Alpha"))
        self.settings.data["auto_tried"] = ["other@1.0"]
        self.bg.scan_finished()
        self.bg.auto_finished(alpha.uid, False, net.REFUSED_METERED)
        self.assertEqual(self.settings.data["auto_tried"], ["other@1.0"])

    def test_never_asks_for_a_non_automatic_request(self):
        self.window.add(make_status("Alpha"))
        self.bg.scan_finished()
        uid, bulk, auto = self.window.requests[0]
        self.assertTrue(bulk and auto)


class TrayLifecycleTest(BackgroundCase):
    def setUp(self):
        super().setUp()
        available = mock.patch.object(background, "_tray_available", return_value=True)
        available.start()
        self.addCleanup(available.stop)
        self.addCleanup(self.bg.stop)

    def test_tray_enabled_shows_an_icon_and_keeps_the_app_alive_without_windows(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        self.assertIsInstance(self.bg.tray, TrayIcon)
        self.assertTrue(self.bg.tray.isVisible())
        self.assertFalse(app.quitOnLastWindowClosed())
        self.bg.stop()
        self.assertIsNone(self.bg.tray)
        self.assertTrue(app.quitOnLastWindowClosed())

    def test_no_tray_when_nothing_asks_for_one(self):
        self.settings.data.update(check_interval_hours=0)
        self.bg.start()
        self.assertIsNone(self.bg.tray)
        self.assertFalse(self.bg.has_tray)
        self.assertTrue(app.quitOnLastWindowClosed())

    def test_the_schedule_with_messages_needs_an_icon_to_speak_from(self):
        self.bg.start()
        self.assertIsNotNone(self.bg.tray)
        self.settings.data["notify_updates"] = False
        self.bg.settings_changed()
        self.assertIsNone(self.bg.tray)

    def test_unavailable_tray_degrades_quietly(self):
        self.settings.data.update(tray_enabled=True, close_to_tray=True)
        with mock.patch.object(background, "_tray_available", return_value=False):
            self.bg.start()
        self.assertIsNone(self.bg.tray)
        self.assertFalse(self.bg.should_hide_on_close())
        self.assertFalse(self.bg.notify("a", "b"))
        self.assertTrue(app.quitOnLastWindowClosed())

    def test_should_hide_on_close(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        self.assertFalse(self.bg.should_hide_on_close())
        self.settings.data["close_to_tray"] = True
        self.assertTrue(self.bg.should_hide_on_close())

    def test_removing_the_tray_brings_back_a_hidden_window(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        self.assertFalse(self.window.isVisible())
        self.settings.data.update(tray_enabled=False, check_interval_hours=0)
        self.bg.settings_changed()
        self.assertIsNone(self.bg.tray)
        self.assertEqual(self.window.raised, 1)
        self.assertTrue(self.window.isVisible())

    def test_stopping_does_not_pop_the_window_up(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        self.bg.stop()
        self.assertEqual(self.window.raised, 0)

    def test_minimized_start(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start(minimized=True)
        self.assertEqual(self.window.minimized, 0)       # the tray is the way back in
        self.bg.stop()
        with mock.patch.object(background, "_tray_available", return_value=False):
            self.bg.start(minimized=True)
        self.assertEqual(self.window.minimized, 1)       # no tray: a minimised taskbar button instead
        self.bg.start()
        self.assertEqual(self.window.minimized, 1)

    def test_closing_the_window_for_real_quits(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        self.window.show()
        self.window.close()
        pump()
        self.assertEqual(self.window.quits, 1)

    def test_closing_to_the_tray_does_not_quit(self):
        self.settings.data.update(tray_enabled=True, close_to_tray=True)
        self.bg.start()
        self.window.show()
        self.window.close()
        pump()
        self.assertEqual(self.window.quits, 0)

    def test_a_dialog_closing_while_the_window_is_hidden_does_not_end_the_app(self):
        """Qt would quit when the last visible window closes, hidden main window or not."""
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        dialog, reached = QWidget(), []
        dialog.show()
        QTimer.singleShot(30, dialog.close)
        QTimer.singleShot(300, lambda: (reached.append(True), app.quit()))
        app.exec()
        self.assertEqual(reached, [True])

    def test_tray_notify_reaches_the_icon(self):
        self.settings.data["tray_enabled"] = True
        self.bg.start()
        with mock.patch.object(QSystemTrayIcon, "supportsMessages", return_value=True), \
                mock.patch.object(TrayIcon, "showMessage") as show:
            self.assertTrue(self.bg.notify("Title", "Body"))
        self.assertEqual(show.call_args.args[:2], ("Title", "Body"))


class TrayIconTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: apply_language(app, "en"))
        self.window = FakeWindow()
        self.addCleanup(self.window.deleteLater)
        self.tray = TrayIcon(self.window)
        self.addCleanup(self.tray.dispose)

    def texts(self):
        return [a.text() for a in self.tray.menu.actions() if not a.isSeparator()]

    def test_menu_without_updates(self):
        self.assertEqual(self.texts(), ["Open Upstream", "Check now", "Update all", "Quit"])
        self.assertFalse(self.tray.update_action.isEnabled())
        self.assertTrue(self.tray.check_action.isEnabled())
        self.assertTrue(self.tray.open_action.font().bold())
        self.assertEqual(self.tray.toolTip(), "Upstream\nEverything is up to date")

    def test_menu_with_updates(self):
        for name in ("Alpha", "Bravo"):
            self.window.add(make_status(name))
        self.tray.menu.aboutToShow.emit()               # the menu re-reads the window when it opens
        self.assertEqual(self.texts()[2], "Update all (2)")
        self.assertTrue(self.tray.update_action.isEnabled())
        self.assertEqual(self.tray.toolTip(), "Upstream\n2 updates ready")

    def test_busy_window_disables_check_and_update(self):
        self.window.add(make_status("Alpha"))
        self.window.busy = True
        self.tray.refresh()
        self.assertFalse(self.tray.check_action.isEnabled())
        self.assertFalse(self.tray.update_action.isEnabled())

    def test_persian(self):
        fa = i18n.FA
        apply_language(app, "fa")
        self.window.add(make_status("Alpha"))
        self.window.add(make_status("Bravo"))
        self.tray.refresh()
        self.assertEqual([plain(t) for t in self.texts()],
                         [fa["Open Upstream"], fa["Check now"], fa["Update all ({n})"].format(n=i18n.num(2)),
                          fa["Quit"]])
        self.assertEqual(plain(self.tray.toolTip()), "Upstream\n" + fa["{n} updates ready"].format(n=i18n.num(2)))
        apply_language(app, "en")
        self.tray.refresh()
        self.assertEqual(self.texts()[0], "Open Upstream")

    def test_the_icon_shows_a_dot_while_updates_wait(self):
        without_dot = self.tray.icon().pixmap(32, 32).toImage()
        self.window.add(make_status("Alpha"))
        self.tray.refresh()
        badged = self.tray.icon().pixmap(32, 32).toImage()
        self.assertNotEqual(without_dot, badged)
        self.window.statuses.clear()
        self.tray.refresh()
        self.assertEqual(self.tray.icon().pixmap(32, 32).toImage(), without_dot)

    def test_menu_actions(self):
        self.window.add(make_status("Alpha"))
        self.tray.refresh()
        self.tray.open_action.trigger()
        self.assertEqual(self.window.raised, 1)
        self.tray.check_action.trigger()
        self.assertEqual((self.window.raised, self.window.scans), (2, 1))
        self.tray.update_action.trigger()
        self.assertEqual((self.window.raised, self.window.update_alls), (3, 1))
        self.tray.quit_action.trigger()
        self.assertEqual(self.window.quits, 1)

    def test_left_click_toggles_the_window(self):
        trigger = QSystemTrayIcon.ActivationReason.Trigger
        self.tray.activated.emit(trigger)
        self.assertTrue(self.window.isVisible())
        self.tray.activated.emit(trigger)
        self.assertFalse(self.window.isVisible())
        self.tray.activated.emit(trigger)
        self.assertTrue(self.window.isVisible())

    def test_a_minimised_window_is_raised_not_hidden(self):
        self.window.show()
        with mock.patch.object(FakeWindow, "isMinimized", return_value=True):
            self.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertEqual(self.window.raised, 1)

    def test_double_click_always_shows(self):
        self.window.show()
        self.tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.assertTrue(self.window.isVisible())

    def test_other_clicks_do_nothing(self):
        self.tray.activated.emit(QSystemTrayIcon.ActivationReason.MiddleClick)
        self.tray.activated.emit(QSystemTrayIcon.ActivationReason.Context)
        self.assertEqual(self.window.raised, 0)

    def test_clicking_a_message_opens_the_window(self):
        self.tray.messageClicked.emit()
        self.assertEqual(self.window.raised, 1)

    def test_messages_need_platform_support(self):
        with mock.patch.object(QSystemTrayIcon, "supportsMessages", return_value=False), \
                mock.patch.object(TrayIcon, "showMessage") as show:
            self.tray.notify("a", "b")
        show.assert_not_called()


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.name = f"UpstreamTest_{uuid.uuid4().hex[:12]}"
        self.addCleanup(self._remove)

    def _remove(self):
        assert self.name.startswith("UpstreamTest_")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, background.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.name)
        except OSError:
            pass

    def raw(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, background.RUN_KEY, 0, winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, self.name)
        except OSError:
            return None

    def test_round_trip(self):
        self.assertFalse(background.is_autostart(self.name))
        try:
            self.assertTrue(background.set_autostart(True, self.name))
            self.assertTrue(background.is_autostart(self.name))
            value, kind = self.raw()
            self.assertEqual((value, kind), (background.autostart_command(), winreg.REG_SZ))
            self.assertTrue(value.endswith(" --minimized"))
            self.assertTrue(background.set_autostart(True, self.name))          # twice is fine
            self.assertTrue(background.set_autostart(False, self.name))
            self.assertFalse(background.is_autostart(self.name))
            self.assertIsNone(self.raw())
            self.assertTrue(background.set_autostart(False, self.name))         # already gone is fine
        finally:
            self._remove()

    def test_task_manager_switch_wins(self):
        try:
            background.set_autostart(True, self.name)
            with mock.patch.object(background, "_task_manager_off", return_value=True):
                self.assertFalse(background.is_autostart(self.name))
        finally:
            self._remove()

    def test_task_manager_switch_reading(self):
        def fake(data):
            key = mock.MagicMock()
            key.__enter__.return_value = key
            return mock.patch.object(background.winreg, "OpenKey", return_value=key), \
                mock.patch.object(background.winreg, "QueryValueEx", return_value=(data, winreg.REG_BINARY))

        for data, off in ((b"\x02\x00\x00\x00" + bytes(8), False), (b"\x03\x00\x00\x00" + bytes(8), True),
                          (b"\x06\x00\x00\x00" + bytes(8), False), (b"", False), ("text", False)):
            with self.subTest(data=data):
                open_key, query = fake(data)
                with open_key, query:
                    self.assertEqual(background._task_manager_off(self.name), off)

    def test_enabling_clears_the_task_manager_switch(self):
        try:
            with mock.patch.object(background, "_clear_task_manager_switch") as clear:
                background.set_autostart(True, self.name)
            clear.assert_called_once_with(self.name)
        finally:
            self._remove()

    def test_failures_return_false(self):
        with mock.patch.object(background.winreg, "CreateKeyEx", side_effect=PermissionError("denied")), \
                self.assertLogs("upstream.background", "WARNING"):
            self.assertFalse(background.set_autostart(True, self.name))
        with mock.patch.object(background.winreg, "OpenKey", side_effect=PermissionError("denied")), \
                self.assertLogs("upstream.background", "WARNING"):
            self.assertFalse(background.set_autostart(False, self.name))
        self.assertIsNone(self.raw())

    def test_the_real_value_name_is_the_default(self):
        params = inspect.signature(background.set_autostart).parameters
        self.assertEqual(params["name"].default, "Upstream")
        self.assertEqual(inspect.signature(background.is_autostart).parameters["name"].default, "Upstream")

    def test_command_for_the_frozen_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Apps\Upstream\Upstream.exe"):
            self.assertEqual(background.autostart_command(), r'"C:\Apps\Upstream\Upstream.exe" --minimized')

    def test_command_from_source_uses_pythonw_and_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            python, pythonw = Path(tmp, "python.exe"), Path(tmp, "pythonw.exe")
            python.write_bytes(b"")
            with mock.patch.object(sys, "executable", str(python)):
                without = background.autostart_command()
                pythonw.write_bytes(b"")
                with_gui = background.autostart_command()
        main = Path(background.__file__).resolve().parent.parent / "main.py"
        self.assertEqual(without, f'"{python}" "{main}" --minimized')
        self.assertEqual(with_gui, f'"{pythonw}" "{main}" --minimized')
        self.assertTrue(main.is_file())


if __name__ == "__main__":
    unittest.main()
