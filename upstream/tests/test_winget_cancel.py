"""Cancelling winget.update(): winget is ended while it only prepares and downloads, and left alone once it
installs. The 'winget' here is a throwaway python child that prints what winget prints; the real one never runs."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from upstream import winget
from upstream.context import CancelToken
from upstream.errors import Cancelled

STOPPED = "Stopped waiting for the installer. It may still be running."

# What the fake prints. Each script ends by sleeping (or finishing) on its own, so a test that fails never
# leaves a process behind for long.
BANNER = 'print("Found Demo [Demo.App] Version 2.0", flush=True)\n'
GRANDCHILD = (
    "import subprocess, sys\n"
    'kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"])\n'
    'print("CHILD", kid.pid, flush=True)\n'
)
BAR = 'print("  ##########  {done} MB / {total} MB", end="\\r", flush=True)\n'
PERCENT = 'print("  ####  40%", flush=True)\n'
SLEEP = "import time; time.sleep(40)\n"
INSTALL_TIME = "time.sleep(2.0)\n"
INSTALLING = (
    "import pathlib, time\n" + BANNER + BAR.format(done="10.0", total="10.0")
    + 'print("Starting package install...", flush=True)\n'
    + INSTALL_TIME + 'pathlib.Path(r"{marker}").write_text("finished")\n'
)


def alive(pid: int) -> bool:
    """Is that process still running? (read only: nothing is touched)"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259                           # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def until(condition, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class FakeWingetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.procs: list[subprocess.Popen] = []      # the fake winget processes, started here
        self.kids: list[int] = []                    # what the fake started in turn
        self.progress: list[int] = []
        self.lines: list[str] = []
        self.winget_args: list[str] = []
        self.script = ""
        self.addCleanup(self.reap)
        real = subprocess.Popen

        def fake_popen(args, **kwargs):
            if args and args[0] == "winget":      # only winget is replaced; taskkill runs for real
                self.winget_args = args
                args = [sys.executable, "-u", "-c", self.script]
                proc = real(args, **kwargs)
                self.procs.append(proc)
                return proc
            return real(args, **kwargs)

        patch = mock.patch.object(winget.subprocess, "Popen", side_effect=fake_popen)
        self.popen = patch.start()
        self.addCleanup(patch.stop)

    def reap(self):
        for proc in self.procs:                    # only processes this test started
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
        for pid in self.kids:
            if alive(pid):
                os.kill(pid, 15)

    def run_update(self, script: str, token=None, on_progress=None, on_line=None, **hooks):
        self.script = script.replace("{marker}", str(Path(self.dir.name, "finished.txt")))

        def progress(pct):
            self.progress.append(pct)
            if on_progress:
                on_progress(pct)

        def line(text):
            self.lines.append(text)
            if text.startswith("CHILD "):
                self.kids.append(int(text.split()[1]))
            if on_line:
                on_line(text)

        return winget.update("Demo.App", "upgrade", progress, line, cancel=token, **hooks)

    # -- cancelling while winget only prepares or downloads
    def test_cancel_during_the_download_ends_winget_and_what_it_started(self):
        token = CancelToken()
        with self.assertRaises(Cancelled) as raised:
            self.run_update(BANNER + GRANDCHILD + BAR.format(done="1.00", total="10.0") + SLEEP, token,
                            on_progress=lambda pct: token.cancel(),
                            on_install=lambda: self.fail("the download had not finished"))
        self.assertEqual(raised.exception.template, "Cancelled.")
        (proc,) = self.procs
        self.assertIsNotNone(proc.poll(), "winget must be gone")
        self.assertTrue(self.kids, "the fake never reported its child")
        self.assertTrue(until(lambda: not any(alive(pid) for pid in self.kids)), "its child must be gone too")

    def test_cancel_before_any_output_ends_winget(self):
        token = CancelToken()
        timer = threading.Timer(0.5, token.cancel)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(Cancelled) as raised:
            self.run_update(SLEEP, token)
        self.assertEqual(raised.exception.template, "Cancelled.")
        (proc,) = self.procs
        self.assertIsNotNone(proc.poll())

    def test_a_token_cancelled_beforehand_never_starts_winget(self):
        token = CancelToken()
        token.cancel()
        with self.assertRaises(Cancelled):
            self.run_update(BANNER + SLEEP, token)
        self.popen.assert_not_called()

    # -- cancelling once it installs
    def test_cancel_during_the_install_stops_waiting_and_leaves_winget_running(self):
        token = CancelToken()
        installs = []
        with self.assertRaises(Cancelled) as raised:
            self.run_update(INSTALLING, token, on_install=lambda: (installs.append(1), token.cancel()))
        self.assertEqual(raised.exception.template, STOPPED)
        self.assertEqual(installs, [1])
        (proc,) = self.procs
        self.assertIsNone(proc.poll(), "winget was not killed")
        self.assertEqual(proc.wait(15), 0)                       # it went on and finished by itself
        self.assertEqual(Path(self.dir.name, "finished.txt").read_text(), "finished")

    def test_after_stopping_the_pipe_is_still_drained(self):
        chatty = INSTALLING.replace(INSTALL_TIME, "[print('x' * 200) for _ in range(2000)]\n")
        token = CancelToken()
        with self.assertRaises(Cancelled):
            self.run_update(chatty, token, on_install=token.cancel)
        (proc,) = self.procs
        self.assertEqual(proc.wait(20), 0, "winget must not block on a pipe nobody reads")

    def test_a_cancel_after_winget_exited_does_not_hide_its_result(self):
        token = CancelToken()

        def cancel_once_gone(text):
            self.assertTrue(until(lambda: self.procs[0].poll() is not None))
            token.cancel()

        code, last = self.run_update(BANNER + "import sys; sys.exit(0)\n", token, on_line=cancel_once_gone)
        self.assertEqual((code, last), (0, "Found Demo [Demo.App] Version 2.0"))

    # -- recognising the install
    def test_a_finished_download_is_the_start_of_the_install(self):
        events = []
        code, last = self.run_update(
            BANNER + BAR.format(done="5.00", total="10.0") + BAR.format(done="10.0", total="10.0")
            + 'print("Something else", flush=True)\nimport sys; sys.exit(3)\n',
            on_install=lambda: events.append(len(self.progress)))
        self.assertEqual((code, last), (3, "Something else"))
        self.assertEqual(self.progress, [50, 100])
        self.assertEqual(events, [2], "told once, after the last download progress")

    def test_a_percent_bar_is_the_installers_and_not_a_download(self):
        events = []
        self.run_update(BANNER + BAR.format(done="5.00", total="10.0") + PERCENT + BAR.format(done="6.00", total="10.0"),
                        on_install=lambda: events.append("install"))
        self.assertEqual(self.progress, [50])
        self.assertEqual(events, ["install"])

    def test_any_text_after_the_download_started_means_the_install_in_every_language(self):
        events = []
        self.run_update(BANNER + BAR.format(done="5.00", total="10.0") + 'print("not english at all", flush=True)\n',
                        on_install=lambda: events.append("install"))
        self.assertEqual(events, ["install"])

    def test_preparing_text_is_not_the_install(self):
        events = []
        self.run_update(BANNER + 'print("This application is licensed to you by its owner.", flush=True)\n',
                        on_install=lambda: events.append("install"))
        self.assertEqual(events, [])

    def test_the_english_install_line_is_enough(self):
        events = []
        self.run_update(BANNER + 'print("Starting package install...", flush=True)\n',
                        on_install=lambda: events.append("install"))
        self.assertEqual(events, ["install"])

    # -- compatibility
    def test_the_old_call_shape_still_works(self):
        self.script = BANNER + BAR.format(done="1.00", total="4.00") + "import sys; sys.exit(0)\n"
        progress = []
        code, last = winget.update("Demo.App", "upgrade", progress.append, lambda text: None)
        self.assertEqual((code, last), (0, "Found Demo [Demo.App] Version 2.0"))
        self.assertEqual(progress, [25])
        self.assertEqual(self.winget_args[:3], ["winget", "upgrade", "--id"])

    def test_an_unused_token_changes_nothing(self):
        code, _ = self.run_update(BANNER + "import sys; sys.exit(7)\n", CancelToken())
        self.assertEqual(code, 7)


class KillTreeTest(unittest.TestCase):
    def test_a_stubborn_taskkill_falls_back_to_kill(self):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired("winget", 5)
        with mock.patch.object(winget.subprocess, "run", side_effect=OSError("no taskkill")),                 self.assertLogs("upstream.winget", "WARNING"):
            winget._kill_tree(proc)
        proc.kill.assert_called_once_with()

    def test_taskkill_gets_the_one_pid_with_its_tree(self):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        with mock.patch.object(winget.subprocess, "run") as run:
            winget._kill_tree(proc)
        command = run.call_args.args[0]
        self.assertEqual(Path(command[0]).name.lower(), "taskkill.exe")
        self.assertEqual(command[1:], ["/T", "/F", "/PID", "4242"])
        proc.kill.assert_not_called()

    def test_an_exited_process_is_left_alone(self):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = 0
        with mock.patch.object(winget.subprocess, "run") as run:
            winget._kill_tree(proc)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
