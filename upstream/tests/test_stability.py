"""Stability: the single-instance hand-over, crash reports, rotating log, housekeeping, launch arguments and the
crash dialog. Everything runs against temp folders and throwaway names: the real data folder, the real log and
the real instance lock are never touched, and no second copy of the program is started."""
from __future__ import annotations

import contextlib
import faulthandler
import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer, qCritical, qDebug, qInfo, qInstallMessageHandler, qWarning     # noqa: E402
from PySide6.QtGui import QGuiApplication, QPalette                                # noqa: E402
from PySide6.QtNetwork import QLocalServer                                         # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel                        # noqa: E402

from upstream import app as app_module                                             # noqa: E402
from upstream import config, i18n, stability                                       # noqa: E402
from upstream.ui import crash as crash_ui                                          # noqa: E402
from upstream.ui import theme                                                      # noqa: E402
from upstream.ui.crash import CrashReportDialog                                    # noqa: E402
from upstream.ui.locale import apply_language                                      # noqa: E402

qt_app = QApplication.instance() or QApplication([])
DAY = 86400


def touch(path: Path, age_days: float = 0, text: str = "x", now: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    when = (time.time() if now is None else now) - age_days * DAY
    os.utime(path, (when, when))
    return path


class TempData(unittest.TestCase):
    """stability.py looks its folders up at call time, so patching the module names is enough."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "data"
        self.downloads = self.data / "downloads"
        self.log_file = self.data / "upstream.log"
        for name, value in (("DATA_DIR", self.data), ("DOWNLOAD_DIR", self.downloads), ("LOG_FILE", self.log_file)):
            patch = mock.patch.object(stability, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.crashes = self.data / "crashes"

    def reports(self, pending: bool = False) -> list[str]:
        names = sorted(p.name for p in self.crashes.glob("crash-*.txt")) if self.crashes.exists() else []
        return [n for n in names if not (pending and n.endswith(".reported.txt"))]


class ScrubTest(unittest.TestCase):
    def test_user_name_leaves_every_path_spelling(self):
        cases = [
            (r"C:\Users\Alice Smith\AppData\Local\Upstream", r"C:\Users\<user>\AppData\Local\Upstream"),
            (r"File 'C:\\Users\\alice\\x.py', line 3", r"File 'C:\\Users\\<user>\\x.py', line 3"),
            ("c:/USERS/alice/proj/a.py", "c:/USERS/<user>/proj/a.py"),
            (r'"C:\Users\alice"', r'"C:\Users\<user>"'),
            (r"\\server\share\Users\alice.corp\doc", r"\\server\share\Users\<user>\doc"),
        ]
        for text, expected in cases:
            self.assertEqual(stability.scrub(text), expected, text)

    def test_profile_outside_users_and_prose_untouched(self):
        with mock.patch.object(stability, "_user_names", return_value=["alice"]):
            self.assertEqual(stability.scrub(r"D:\home\alice\work and Alice said hi"),
                             r"D:\home\<user>\work and Alice said hi")
            self.assertEqual(stability.scrub(r"C:\Program Files\Foo\bar.exe"), r"C:\Program Files\Foo\bar.exe")

    def test_access_tokens_are_removed(self):
        token = "ghp_" + "a1B2c3D4e5" * 4
        pat = "github_pat_" + "x1Y2z3" * 8
        out = stability.scrub(f"Authorization failed for {token} and {pat}")
        self.assertNotIn(token, out)
        self.assertNotIn(pat, out)
        self.assertEqual(out.count("<token>"), 2)


class ReportTest(TempData):
    def test_report_has_versions_traceback_and_scrubbed_log_tail(self):
        self.data.mkdir(parents=True)
        user = Path.home().name
        lines = [f"2026-11-08 12:00:{i % 60:02d} INFO upstream.updater: step {i} in C:\\Users\\{user}\\AppData\\x"
                 for i in range(250)]
        self.log_file.write_text("\n".join(lines) + "\n", "utf-8")
        try:
            raise ValueError(rf"bad path C:\Users\{user}\secret.txt")
        except ValueError:
            import traceback
            details = traceback.format_exc()
        text = stability.build_report("An unhandled exception ended Upstream.", details)
        for needle in ("Upstream crash report", "An unhandled exception ended Upstream.", f"Upstream: {stability.__version__}",
                       "Python: 3.", "Qt: 6.", "Windows: ", "ValueError: bad path", "Last 100 log lines:"):
            self.assertIn(needle, text)
        self.assertIn("step 249 ", text)
        self.assertIn("step 150 ", text)
        self.assertNotIn("step 149 ", text, "only the last 100 lines")
        self.assertNotIn(user, text.replace("<user>", ""), "the user name must be scrubbed everywhere")
        self.assertGreaterEqual(text.count("<user>"), 101)

    def test_missing_log_still_gives_a_report(self):
        text = stability.build_report("x", "details")
        self.assertIn("details", text)
        self.assertTrue(text.rstrip().endswith("Last 100 log lines:"))

    def test_write_report_names_and_never_overwrites(self):
        when = 1_800_000_000
        first = stability.write_report("a", "one", when, pid=4321)
        second = stability.write_report("a", "two", when, pid=4321)
        self.assertEqual(first.parent, self.crashes)
        self.assertRegex(first.name, r"^crash-\d{8}-\d{6}-4321\.txt$")
        self.assertNotEqual(first, second)
        self.assertIn("one", first.read_text("utf-8"))
        self.assertIn("two", second.read_text("utf-8"))
        self.assertGreater(second.name, first.name, "later report sorts later")

    def test_write_report_failure_is_none(self):
        blocker = self.data
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("a file where the folder should be", "utf-8")
        self.assertIsNone(stability.write_report("a", "b"))


class PendingTest(TempData):
    def make(self, n: int) -> list[Path]:
        return [stability.write_report("h", f"report {i}", 1_800_000_000 + i * 60, pid=100 + i) for i in range(n)]

    def test_nothing_pending_without_a_folder(self):
        self.assertIsNone(stability.pending_crash_report())

    def test_newest_pending_and_marking_covers_older_ones(self):
        old, mid, new = self.make(3)
        self.assertEqual(stability.pending_crash_report(), new)
        stability.mark_reported(new)
        self.assertIsNone(stability.pending_crash_report())
        self.assertEqual(self.reports(pending=True), [])
        self.assertEqual(len(self.reports()), 3, "reported reports stay on disk")
        self.assertTrue(all(n.endswith(".reported.txt") for n in self.reports()))

    def test_marking_an_older_one_leaves_newer_pending(self):
        old, mid, new = self.make(3)
        stability.mark_reported(mid)
        self.assertEqual(stability.pending_crash_report(), new)
        self.assertEqual(self.reports(pending=True), [new.name])
        stability.mark_reported(mid)            # twice is harmless
        self.assertEqual(self.reports(pending=True), [new.name])

    def test_mark_reported_of_a_missing_file_is_quiet(self):
        with self.assertNoLogs("upstream.stability", "WARNING"):
            stability.mark_reported(self.crashes / "crash-20260101-000000-1.txt")

    def test_a_report_that_cannot_be_renamed_is_logged_not_raised(self):
        path = stability.write_report("h", "x", pid=3)
        with mock.patch.object(Path, "replace", side_effect=PermissionError("locked")),                 self.assertLogs("upstream.stability", "WARNING"):
            stability.mark_reported(path)
        self.assertEqual(stability.pending_crash_report(), path)


class ExceptionHandlerTest(TempData):
    @staticmethod
    def raised(exc: Exception):
        try:
            raise exc
        except type(exc):
            return sys.exc_info()

    def test_exception_that_ends_the_program_gets_a_report(self):
        with mock.patch.object(stability, "_event_loop_running", return_value=False), \
                self.assertLogs("upstream.stability", "CRITICAL"):
            stability._on_exception(*self.raised(ValueError("boom")))
        report = stability.pending_crash_report()
        self.assertIsNotNone(report)
        text = report.read_text("utf-8")
        self.assertIn("ValueError: boom", text)
        self.assertIn("ended Upstream", text)

    def test_exception_in_a_slot_or_thread_is_only_logged(self):
        with mock.patch.object(stability, "_event_loop_running", return_value=True), \
                self.assertLogs("upstream.stability", "ERROR") as cm:
            stability._on_exception(*self.raised(ValueError("slot")))
        self.assertIn("ValueError: slot", cm.output[0])
        with mock.patch.object(stability, "_event_loop_running", return_value=False), \
                self.assertLogs("upstream.stability", "ERROR") as cm:
            stability._on_exception(*self.raised(KeyError("worker")), thread="Thread-9")
        self.assertIn("in thread Thread-9", cm.output[0])
        self.assertIsNone(stability.pending_crash_report())
        self.assertEqual(self.reports(), [])

    def test_keyboard_interrupt_is_ignored(self):
        with mock.patch.object(stability, "_event_loop_running", return_value=False):
            stability._on_exception(*self.raised(KeyboardInterrupt()))
        self.assertEqual(self.reports(), [])

    def test_a_failing_report_never_raises(self):
        with mock.patch.object(stability, "_event_loop_running", return_value=False), \
                mock.patch.object(stability, "write_report", side_effect=RuntimeError("disk")):
            stability._on_exception(*self.raised(ValueError("boom")))
        self.assertFalse(stability._reporting, "the guard must reset")

    def test_real_event_loop_counts_as_running(self):
        """A slot that raises reaches sys.excepthook with the loop running: no report, only the log."""
        seen = []
        old_hook = sys.excepthook
        sys.excepthook = lambda *a: (seen.append(stability._event_loop_running()), stability._on_exception(*a))
        self.addCleanup(setattr, sys, "excepthook", old_hook)

        def boom():
            raise ValueError("from a slot")

        self.assertFalse(stability._event_loop_running())
        QTimer.singleShot(0, boom)
        QTimer.singleShot(80, qt_app.quit)
        with self.assertLogs("upstream.stability", "ERROR"):
            qt_app.exec()
        self.assertEqual(seen, [True])
        self.assertEqual(self.reports(), [])


class InstallHandlersTest(TempData):
    def setUp(self):
        super().setUp()
        saved = (sys.excepthook, threading.excepthook, faulthandler.is_enabled())
        self.addCleanup(self._restore, *saved)
        stability._installed = False
        stability._qt_counts.clear()

    @staticmethod
    def _restore(excepthook, thread_hook, fault_was_on):
        stability._close_fault_file()
        qInstallMessageHandler(None)
        stability._qt_handler = None
        stability._installed = False
        sys.excepthook, threading.excepthook = excepthook, thread_hook
        if fault_was_on:
            faulthandler.enable()

    def test_install_wires_hooks_fault_file_and_qt_handler(self):
        before = (sys.excepthook, threading.excepthook)
        stability.install_crash_handlers()
        self.assertNotEqual((sys.excepthook, threading.excepthook), before)
        fault = self.crashes / f"fault-{os.getpid()}.log"
        self.assertTrue(fault.exists())
        self.assertTrue(faulthandler.is_enabled())

        hooks = (sys.excepthook, threading.excepthook)
        stability.install_crash_handlers()
        self.assertEqual((sys.excepthook, threading.excepthook), hooks, "installing twice changes nothing")

        def worker():
            raise OSError("thread failed")

        with self.assertLogs("upstream.stability", "ERROR") as cm, contextlib.redirect_stderr(io.StringIO()):
            t = threading.Thread(target=worker, name="probe")
            t.start()
            t.join()
        self.assertIn("in thread probe", cm.output[0])
        self.assertEqual(self.reports(), [], "a dying worker thread does not make a crash report")

        with self.assertLogs("upstream.qt", "WARNING") as cm, contextlib.redirect_stderr(io.StringIO()):
            qWarning("QFont: something odd")
        self.assertEqual(cm.output, ["WARNING:upstream.qt:QFont: something odd"])

        stability._close_fault_file()
        self.assertFalse(fault.exists(), "a clean exit leaves no fault file behind")

    def test_a_repeating_qt_message_is_logged_a_few_times_then_summarised(self):
        stability._install_qt_handler()
        with self.assertLogs("upstream.qt", "WARNING") as cm, contextlib.redirect_stderr(io.StringIO()):
            for _ in range(50):
                qWarning("QPainter: same thing again")
            qWarning("another message")
        self.assertEqual(len(cm.output), stability.QT_REPEATS + 1 + 1)
        self.assertIn("further copies are not logged", cm.output[stability.QT_REPEATS])
        self.assertIn("another message", cm.output[-1])

    def test_qt_message_levels(self):
        stability._install_qt_handler()
        with self.assertLogs("upstream.qt", "WARNING") as cm, contextlib.redirect_stderr(io.StringIO()):
            qWarning("w")
            qCritical("c")
            qInfo("i")
            qDebug("d")
        self.assertEqual(cm.output, ["WARNING:upstream.qt:w", "ERROR:upstream.qt:c"])


class FaultFileTest(TempData):
    def test_non_empty_dump_becomes_a_report_and_empty_ones_go(self):
        self.crashes.mkdir(parents=True)
        dump = "Windows fatal exception: access violation\n\nCurrent thread 0x00001a2c (most recent call first):\n" \
               "  File \"C:\\Users\\alice\\x.py\", line 9 in run\n"
        crashed = self.crashes / "fault-4242.log"
        crashed.write_text(dump, "utf-8")
        when = 1_800_000_000
        os.utime(crashed, (when, when))
        empty = touch(self.crashes / "fault-4243.log", text="")
        own = touch(self.crashes / f"fault-{os.getpid()}.log", text="still being written")
        self.log_file.write_text("2026-11-08 10:00:00 INFO upstream.app: last line of the crashed run\n", "utf-8")

        stability._adopt_fault_files()

        self.assertFalse(crashed.exists())
        self.assertFalse(empty.exists())
        self.assertTrue(own.exists(), "this process's own file is not a crash")
        (name,) = self.reports()
        self.assertRegex(name, r"^crash-\d{8}-\d{6}-4242\.txt$")
        text = (self.crashes / name).read_text("utf-8")
        self.assertIn("access violation", text)
        self.assertIn("last line of the crashed run", text)
        self.assertIn("C:\\Users\\<user>\\x.py", text)
        self.assertIn("Recovered at the next start", text)

    def test_dump_is_kept_when_the_report_cannot_be_saved(self):
        self.crashes.mkdir(parents=True)
        crashed = touch(self.crashes / "fault-77.log", text="fatal stuff")
        with mock.patch.object(stability, "write_report", return_value=None):
            stability._adopt_fault_files()
        self.assertTrue(crashed.exists())

    def test_a_file_another_instance_holds_open_is_left_alone(self):
        self.crashes.mkdir(parents=True)
        other = touch(self.crashes / "fault-88.log", text="")
        with open(other, "ab"):
            stability._adopt_fault_files()
            self.assertTrue(other.exists())
        self.assertEqual(self.reports(), [])

    def test_missing_folder_is_fine(self):
        stability._adopt_fault_files()
        self.assertEqual(self.reports(), [])


class HousekeepingTest(TempData):
    def test_old_downloads_go_recent_ones_and_subfolders_stay(self):
        now = time.time()
        old = [touch(self.downloads / n, 8, now=now) for n in ("Setup-1.exe", "Setup-1.msi", "abc123.part", "abc123.part.json")]
        recent = [touch(self.downloads / n, 6, now=now) for n in ("Setup-2.exe", "def.part")]
        fresh = touch(self.downloads / "Setup-3.exe", 0, now=now)
        nested = touch(self.downloads / "extracted" / "old.dll", 20, now=now)
        stability.housekeeping(now)
        self.assertEqual([p.exists() for p in old], [False] * 4)
        self.assertTrue(all(p.exists() for p in recent + [fresh, nested]))

    def test_only_the_downloads_and_crash_folders_are_touched(self):
        now = time.time()
        others = [touch(self.data / "cache.json", 90, now=now), touch(self.data / "venv" / "lib.py", 90, now=now),
                  touch(self.data / "quarantine" / "1" / "manifest.json", 90, now=now)]
        stability.housekeeping(now)
        self.assertTrue(all(p.exists() for p in others))

    def test_junction_is_not_followed(self):
        import _winapi
        now = time.time()
        target = self.data / "elsewhere"
        precious = touch(target / "keep.exe", 400, now=now)
        self.downloads.mkdir(parents=True)
        try:
            _winapi.CreateJunction(str(target), str(self.downloads / "link"))
        except OSError as e:
            self.skipTest(f"cannot create a junction here: {e}")
        stability.housekeeping(now)
        self.assertTrue(precious.exists())
        self.assertTrue((self.downloads / "link").exists())

    def test_crash_reports_keep_the_newest_five_and_drop_the_old_rest(self):
        now = 1_800_000_000
        paths = []
        for i, age in enumerate([1, 2, 3, 40, 41, 42, 43, 44, 45]):        # days old, newest first
            path = stability.write_report("h", f"r{i}", now - age * DAY, pid=500 + i)
            os.utime(path, (now - age * DAY, now - age * DAY))
            paths.append(path)
        stability.mark_reported(paths[4])        # marks it and everything older; reported ones count the same
        stability.housekeeping(now)
        expected = {p.name for p in paths[:4]} | {paths[4].name.replace(".txt", ".reported.txt")}
        self.assertEqual({p.name for p in self.crashes.glob("crash-*.txt")}, expected)

    def test_few_old_reports_are_all_kept(self):
        now = 1_800_000_000
        for i in range(4):
            path = stability.write_report("h", "x", now - 90 * DAY, pid=600 + i)
            os.utime(path, (now - 90 * DAY, now - 90 * DAY))
        stability.housekeeping(now)
        self.assertEqual(len(self.reports()), 4)

    def test_never_raises(self):
        stability.housekeeping()                                     # no folders at all
        now = time.time()
        stuck = touch(self.downloads / "in-use.exe", 30, now=now)
        with open(stuck, "rb"):                                       # Windows refuses to delete an open file
            stability.housekeeping(now)
            self.assertTrue(stuck.exists())
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            stability.housekeeping(now)
        with mock.patch.object(stability, "_purge_older_than", side_effect=RuntimeError("odd")), \
                self.assertLogs("upstream.stability", "ERROR"):
            stability.housekeeping(now)


class LoggingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.logger = logging.getLogger(f"upstream.test.{uuid.uuid4().hex}")
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)

    def handler(self, max_bytes: int, backups: int = 2):
        h = config.RotatingLogHandler(self.dir / "u.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self.logger.addHandler(h)
        self.addCleanup(h.close)
        return h

    def test_setup_logging_uses_a_rotating_handler(self):
        root = logging.getLogger("upstream")
        saved, level = root.handlers[:], root.level
        root.handlers = []
        self.addCleanup(lambda: (root.handlers.clear(), root.handlers.extend(saved), root.setLevel(level)))
        with mock.patch.object(config, "DATA_DIR", self.dir), mock.patch.object(config, "LOG_FILE", self.dir / "upstream.log"):
            config.setup_logging()
            config.setup_logging()
            (handler,) = root.handlers
            self.addCleanup(handler.close)
            self.assertIsInstance(handler, config.RotatingLogHandler)
            self.assertEqual((handler.maxBytes, handler.backupCount), (1_000_000, 2))
            logging.getLogger("upstream.test").info("caf\u00e9 \u2713")
        self.assertIn("caf\u00e9 \u2713", (self.dir / "upstream.log").read_text("utf-8"))

    def test_rotation_keeps_three_files(self):
        self.handler(2000)
        for i in range(400):
            self.logger.info("line %03d with a bit of padding to fill the file", i)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(names, ["u.log", "u.log.1", "u.log.2"])
        for name in names:
            self.assertLessEqual((self.dir / name).stat().st_size, 2200)
        self.assertIn("line 399", (self.dir / "u.log").read_text("utf-8"))

    def test_a_log_held_open_by_another_process_does_not_stop_logging(self):
        self.handler(500)
        for i in range(5):
            self.logger.info("early %d", i)
        holder = open(self.dir / "u.log", "rb")        # what a second Upstream process holds
        try:
            for i in range(60):
                self.logger.info("while held %02d padding padding padding", i)    # rotation fails, nothing raises
            self.assertFalse((self.dir / "u.log.1").exists())
            self.assertIn("while held 59", (self.dir / "u.log").read_text("utf-8"))
        finally:
            holder.close()
        for i in range(60):
            self.logger.info("after %02d padding padding padding", i)
        self.assertTrue((self.dir / "u.log.1").exists(), "rotation resumes once the file is free")
        self.assertIn("after 59", (self.dir / "u.log").read_text("utf-8"))


class SingleInstanceTest(unittest.TestCase):
    def setUp(self):
        self.name = f"UpstreamTest-{uuid.uuid4().hex}"
        self.addCleanup(stability.release_single_instance)
        for attr, value in (("HANDOVER_TIMEOUT_MS", 1500), ("HANDOVER_ATTEMPTS", 4), ("HANDOVER_RETRY_MS", 20)):
            patch = mock.patch.object(stability, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.activated = 0

    def first(self, on_activate=None) -> bool:
        def count():
            self.activated += 1
        return stability.acquire_single_instance(on_activate or count, name=self.name)

    def test_second_launch_hands_over_and_exits(self):
        self.assertTrue(self.first())
        self.assertIsNotNone(stability._server, "the first instance listens for later launches")
        other = mock.Mock()
        self.assertFalse(stability.acquire_single_instance(other, name=self.name))
        self.assertEqual(self.activated, 1, "the first instance was asked to come forward")
        other.assert_not_called()
        self.assertFalse(stability.acquire_single_instance(other, name=self.name))
        self.assertEqual(self.activated, 2)

    def test_quiet_launch_checks_without_activating(self):
        self.assertTrue(self.first())
        self.assertFalse(stability.acquire_single_instance(mock.Mock(), name=self.name, activate=False))
        self.assertEqual(self.activated, 0)

    def test_after_release_the_next_launch_is_the_instance(self):
        self.assertTrue(self.first())
        stability.release_single_instance()
        self.assertIsNone(stability._server)
        self.assertTrue(self.first())

    def test_a_failing_activation_still_answers(self):
        self.assertTrue(self.first(mock.Mock(side_effect=RuntimeError("no window yet"))))
        with self.assertLogs("upstream.stability", "ERROR"):
            self.assertFalse(stability.acquire_single_instance(mock.Mock(), name=self.name))

    def test_a_stranger_on_the_channel_does_not_trigger_activation(self):
        self.assertTrue(self.first())
        self.assertNotEqual(stability._ask(self.name, b"hello\n"), "ok")
        self.assertNotEqual(stability._ask(self.name, b"x" * 200), "ok")
        self.assertEqual(self.activated, 0)
        self.assertEqual(stability._ask(self.name, stability._PING), "ok", "and the channel still works")

    def test_names_are_per_user_and_safe(self):
        name = stability.instance_name()
        self.assertRegex(name, r"^Upstream-[0-9a-f]{12}-\d+$")
        self.assertEqual(name, stability.instance_name())
        with mock.patch.dict(os.environ, {"USERNAME": "somebody-else-" + uuid.uuid4().hex}):
            self.assertNotEqual(stability.instance_name(), name)

    def test_lock_held_by_a_process_that_cannot_be_reached_starts_normally(self):
        state, handle = stability._take_lock(self.name)
        self.assertEqual(state, "acquired")
        self.addCleanup(stability._k32().CloseHandle, handle)
        with self.assertLogs("upstream.stability", "WARNING") as cm:
            self.assertTrue(self.first())
        self.assertIn("cannot be reached", cm.output[-1])
        self.assertIsNone(stability._server)
        self.assertEqual(stability._mutex, 0, "this process does not claim a lock it never got")

    def test_an_elevated_first_instance_is_carried_past(self):
        with mock.patch.object(stability, "_take_lock", return_value=("denied", 0)), \
                self.assertLogs("upstream.stability", "WARNING") as cm:
            self.assertTrue(self.first())
        self.assertIn("denied", cm.output[-1])

    def test_lock_freed_while_retrying_makes_this_the_instance(self):
        state, handle = stability._take_lock(self.name)
        QTimer.singleShot(15, lambda: stability._k32().CloseHandle(handle))
        self.assertTrue(self.first())
        self.assertNotEqual(stability._mutex, 0)
        self.assertIsNotNone(stability._server)
        self.assertFalse(stability.acquire_single_instance(mock.Mock(), name=self.name), "and it now answers")

    def test_a_channel_that_cannot_open_still_leaves_a_working_instance(self):
        with mock.patch.object(QLocalServer, "listen", return_value=False), \
                self.assertLogs("upstream.stability", "WARNING"):
            self.assertTrue(self.first())
        self.assertIsNone(stability._server)
        self.assertNotEqual(stability._mutex, 0)

    def test_the_check_itself_failing_never_blocks_the_start(self):
        with mock.patch.object(stability, "_acquire", side_effect=RuntimeError("boom")), \
                self.assertLogs("upstream.stability", "ERROR"):
            self.assertTrue(self.first())


class WaitForExitTest(unittest.TestCase):
    def sleeper(self, seconds: float) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.kill() if proc.poll() is None else None, proc.wait()))
        return proc

    def test_no_pid_means_nothing_to_wait_for(self):
        self.assertTrue(stability.wait_for_exit(0))
        self.assertTrue(stability.wait_for_exit(-5))

    def test_running_process_times_out(self):
        proc = self.sleeper(30)
        started = time.time()
        self.assertFalse(stability.wait_for_exit(proc.pid, timeout=0.3))
        self.assertLess(time.time() - started, 5)

    def test_returns_when_the_process_exits(self):
        proc = self.sleeper(0.6)
        started = time.time()
        self.assertTrue(stability.wait_for_exit(proc.pid, timeout=20))
        self.assertLess(time.time() - started, 10)

    def test_finished_process_is_gone(self):
        proc = self.sleeper(0)
        proc.wait()
        self.assertTrue(stability.wait_for_exit(proc.pid, timeout=5))


class LaunchTest(unittest.TestCase):
    def test_arguments(self):
        L = app_module.Launch
        cases = [
            ([], L()),
            (["--minimized"], L(minimized=True)),
            (["--elevated", "--wait-pid", "1234"], L(elevated=True, wait_pid=1234)),
            (["--wait-pid=77", "--minimized"], L(minimized=True, wait_pid=77)),
            (["--wait-pid"], L()),
            (["--wait-pid", "abc"], L()),
            (["--wait-pid", "²"], L()),
            (["--wait-pid", "-4"], L()),
            (["-platform", "offscreen", "--style=fusion"], L()),
        ]
        for argv, expected in cases:
            self.assertEqual(app_module.parse_launch(argv), expected, argv)

    def test_window_visibility_at_start(self):
        for launch, tray, expected in ((app_module.Launch(), False, "show"),
                                       (app_module.Launch(), True, "show"),
                                       (app_module.Launch(minimized=True), True, None),
                                       (app_module.Launch(minimized=True), False, "showMinimized")):
            window, settings = mock.Mock(), mock.Mock()
            settings.get.side_effect = lambda key, default=None, tray=tray: tray if key == "tray_enabled" else default
            app_module._show_window(window, settings, launch)
            self.assertEqual([c[0] for c in window.method_calls], [expected] if expected else [], (launch, tray))


class CrashReportAtStartTest(TempData):
    def window(self, visible=True):
        return mock.Mock(isVisible=mock.Mock(return_value=visible))

    def test_no_report_no_dialog(self):
        with mock.patch.object(app_module, "CrashReportDialog") as dialog:
            app_module._show_crash_report(self.window())
        dialog.assert_not_called()

    def test_report_is_shown_once_and_marked(self):
        path = stability.write_report("h", "boom", pid=9)
        window = self.window()
        with mock.patch.object(app_module, "CrashReportDialog") as dialog:
            app_module._show_crash_report(window)
        dialog.assert_called_once_with(path, window)
        dialog.return_value.exec.assert_called_once()
        self.assertIsNone(stability.pending_crash_report())
        with mock.patch.object(app_module, "CrashReportDialog") as dialog:
            app_module._show_crash_report(window)
        dialog.assert_not_called()

    def test_hidden_window_keeps_the_report_for_later(self):
        path = stability.write_report("h", "boom", pid=9)
        with mock.patch.object(app_module, "CrashReportDialog") as dialog:
            app_module._show_crash_report(self.window(visible=False))
        dialog.assert_not_called()
        self.assertEqual(stability.pending_crash_report(), path)

    def test_a_failing_dialog_never_breaks_the_start(self):
        stability.write_report("h", "boom", pid=9)
        with mock.patch.object(app_module, "CrashReportDialog", side_effect=RuntimeError("ui")), \
                self.assertLogs("upstream.app", "ERROR"):
            app_module._show_crash_report(self.window())


class CrashDialogTest(TempData):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(theme, "DATA_DIR", Path(self._tmp.name))        # theme glyphs are cached there
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._reset)
        self.path = stability.write_report("An unhandled exception ended Upstream.", "Traceback...\nValueError: boom", pid=7)

    @staticmethod
    def _reset():
        apply_language(qt_app, "en")
        qt_app.setStyleSheet("")
        qt_app.setPalette(QPalette())
        theme._current = theme.THEMES["paper"]
        QGuiApplication.clipboard().clear()

    def make(self, path=None) -> CrashReportDialog:
        d = CrashReportDialog(path or self.path)
        self.addCleanup(d.close)
        return d

    def test_english_content_and_buttons(self):
        theme.apply_theme(qt_app, "paper")
        d = self.make()
        d.show()
        qt_app.processEvents()
        text = self.path.read_text("utf-8")
        self.assertEqual(d.view.toPlainText(), text)
        self.assertTrue(d.view.isReadOnly())
        self.assertEqual(d.view.layoutDirection(), Qt.LeftToRight)
        self.assertEqual([b.text() for b in (d.open_btn, d.copy_btn, d.dismiss_btn)],
                         ["Open reports folder", "Copy report", "Dismiss"])
        self.assertEqual(d.findChild(QLabel, "dlgTitle").text(), "Upstream closed unexpectedly last time")
        self.assertIn("Nothing was sent anywhere", d.findChild(QLabel, "hint").text())
        self.assertTrue(d.dismiss_btn.isDefault())

    def test_persian_is_translated_and_right_to_left(self):
        apply_language(qt_app, "fa")
        theme.apply_theme(qt_app, "paper")
        d = self.make()
        d.show()
        qt_app.processEvents()
        self.assertEqual(d.layoutDirection(), Qt.RightToLeft)
        self.assertEqual(d.view.layoutDirection(), Qt.LeftToRight, "the traceback stays left-to-right")
        fa = i18n.FA
        self.assertEqual([b.text() for b in (d.open_btn, d.copy_btn, d.dismiss_btn)],
                         [fa["Open reports folder"], fa["Copy report"], fa["Dismiss"]])
        title = d.findChild(QLabel, "dlgTitle").text()
        self.assertEqual(title, fa["Upstream closed unexpectedly last time"])
        self.assertTrue("\u0600" <= title[0] <= "\u06ff", "a Persian string must not open with a Latin word")
        self.assertEqual(d.findChild(QLabel, "hint").text(), fa[next(k for k in fa if k.startswith("A report about it"))])
        self.assertLess(d.dismiss_btn.geometry().center().x(), d.open_btn.geometry().center().x(),
                        "the primary button is at the trailing (left) edge in RTL")

    def test_copy_puts_the_whole_report_on_the_clipboard_and_shows_feedback(self):
        d = self.make()
        d.copy_btn.click()
        self.assertEqual(QGuiApplication.clipboard().text().replace("\r\n", "\n"), self.path.read_text("utf-8"))
        self.assertEqual(d.copy_btn.text(), "Report copied")
        d._copied.timeout.emit()
        self.assertEqual(d.copy_btn.text(), "Copy report")

    def test_open_folder_opens_the_report_folder(self):
        d = self.make()
        with mock.patch.object(crash_ui.QDesktopServices, "openUrl") as open_url:
            d.open_btn.click()
        (url,), _ = open_url.call_args
        self.assertEqual(Path(url.toLocalFile()), self.path.parent)

    def test_dismiss_accepts(self):
        d = self.make()
        d.show()
        d.dismiss_btn.click()
        self.assertEqual(d.result(), QDialog.Accepted)

    def test_unreadable_report_is_handled(self):
        d = self.make(self.crashes / "crash-gone.txt")
        self.assertEqual(d.view.toPlainText(), "")
        self.assertIn("could not be read", d.view.placeholderText())
        self.assertFalse(d.copy_btn.isEnabled())
        self.assertTrue(d.open_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
