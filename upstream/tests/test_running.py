"""Closing running apps. The process tests start their own throwaway program (a copy of pythonw.exe in a temp
folder that shows a window) and only ever close that; nothing else on the machine is touched."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from upstream import guard, running
from upstream.context import Question, UpdateContext
from upstream.errors import Cancelled
from upstream.inventory import InstalledApp

CHILD = r'''
import ctypes, os, sys, threading, time
from ctypes import wintypes

mode, ready = sys.argv[1], sys.argv[2]
timer = threading.Timer(90, lambda: os._exit(0))     # a crashed test run never leaves this behind
timer.daemon = True
timer.start()
if mode == "nowindow":
    open(ready, "w").close()
    time.sleep(90)

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32")
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetModuleHandleW.restype = wintypes.HINSTANCE
u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.DefWindowProcW.restype = ctypes.c_ssize_t
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + [ctypes.c_int] * 4 \
    + [wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
u32.CreateWindowExW.restype = wintypes.HWND
u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
u32.TranslateMessage.argtypes = u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]


def proc(hwnd, msg, wparam, lparam):
    if msg == 0x10 and mode == "stubborn":      # WM_CLOSE ignored
        return 0
    if msg == 0x2:                              # WM_DESTROY
        u32.PostQuitMessage(0)
        return 0
    return u32.DefWindowProcW(hwnd, msg, wparam, lparam)


callback = WNDPROC(proc)
cls = WNDCLASS()
cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = callback, k32.GetModuleHandleW(None), "UpstreamTestWindow"
u32.RegisterClassW(ctypes.byref(cls))
style = 0x00CF0000 | (0 if mode == "hidden" else 0x10000000)     # WS_OVERLAPPEDWINDOW [| WS_VISIBLE]
u32.CreateWindowExW(0, cls.lpszClassName, "Upstream test window", style, 100, 100, 300, 200, None, None,
                    cls.hInstance, None)
open(ready, "w").close()
msg = wintypes.MSG()
while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    u32.TranslateMessage(ctypes.byref(msg))
    u32.DispatchMessageW(ctypes.byref(msg))
'''

BASE = Path(sys.base_prefix)
RUNTIME = [BASE / "pythonw.exe", *map(Path, glob.glob(str(BASE / "python3*.dll"))),
           *map(Path, glob.glob(str(BASE / "vcruntime140*.dll")))]


class Asker:
    """Stands in for the window's question box; answers in order, No once they run out."""

    def __init__(self, *answers: bool):
        self.answers, self.questions = list(answers), []

    def __call__(self, question: Question) -> bool:
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else False


def make_app(folder: str = "", **over) -> InstalledApp:
    fields = dict(reg_key=r"HKLM64\Demo", name="Demo", version="1.0", publisher="Demo Inc", scope="machine",
                  arch="x64", install_location=folder)
    return InstalledApp(**{**fields, **over})


def make_ctx(policy: str, *answers: bool, ask=True) -> tuple[UpdateContext, Asker]:
    asker = Asker(*answers)
    return UpdateContext({"close_apps_policy": policy}, ask_fn=asker if ask else None), asker


def exempt(folder: str):
    """guard.py treats everything under %TEMP% as off limits (rightly); let the test's own folder through."""
    real, root = guard.protected_path, guard.normalize(folder)
    return mock.patch.object(guard, "protected_path",
                             lambda path: None if guard.inside(guard.normalize(path), root) else real(path))


class FlowTest(unittest.TestCase):
    """The policy and the order of questions, with the process work replaced by fakes."""

    def setUp(self):
        self.app = make_app(r"C:\Program Files\Demo")
        self.procs = [running.RunningProcess(101, "demo.exe", r"C:\Program Files\Demo\demo.exe", 5),
                      running.RunningProcess(102, "helper.exe", r"C:\Program Files\Demo\helper.exe", 6)]
        self.progress = mock.Mock()

    def patched(self, **fakes):
        base = {"find_running": lambda app: self.procs, "_post_close": lambda procs: 1,
                "_wait_gone": lambda ctx, procs, seconds: [], "_terminate": mock.Mock(), "_alive": lambda p: False}
        base.update(fakes)
        stack = mock.patch.multiple(running, **base)
        return stack

    def test_never_does_nothing_and_does_not_even_look(self):
        ctx, asker = make_ctx("never")
        with self.patched(find_running=mock.Mock(side_effect=AssertionError("must not scan"))):
            running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual(asker.questions, [])
        self.progress.assert_not_called()

    def test_nothing_running_means_no_question(self):
        ctx, asker = make_ctx("ask")
        with self.patched(find_running=lambda app: []):
            running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual(asker.questions, [])
        self.progress.assert_not_called()

    def test_ask_names_the_app_and_lists_the_processes(self):
        ctx, asker = make_ctx("ask", True)
        with self.patched():
            running.close_for_update(ctx, self.app, self.progress)
        (q,) = asker.questions
        self.assertEqual(q.template, "{name} is running. Close it to update?")
        self.assertEqual(q.values["name"], "Demo")
        self.assertEqual(q.values["processes"], "demo.exe, helper.exe")
        self.assertIn("{processes}", q.detail)
        self.assertFalse(q.danger)
        self.progress.assert_called_once_with("Closing {name}…", -1, name="Demo")

    def test_declining_cancels_and_closes_nothing(self):
        ctx, _ = make_ctx("ask", False)
        post = mock.Mock(return_value=1)
        with self.patched(_post_close=post):
            with self.assertRaises(Cancelled) as raised:
                running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual((raised.exception.template, raised.exception.values),
                         ("Update cancelled: {name} is still running.", {"name": "Demo"}))
        post.assert_not_called()

    def test_without_a_window_to_answer_the_default_is_no(self):
        ctx, _ = make_ctx("ask", ask=False)
        with self.patched():
            with self.assertRaises(Cancelled):
                running.close_for_update(ctx, self.app, self.progress)

    def test_auto_closes_without_asking(self):
        ctx, asker = make_ctx("auto")
        post = mock.Mock(return_value=2)
        with self.patched(_post_close=post):
            running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual(asker.questions, [])
        post.assert_called_once_with(self.procs)

    def test_unknown_policy_behaves_like_ask(self):
        ctx, asker = make_ctx("whatever", False)
        with self.patched():
            with self.assertRaises(Cancelled):
                running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual(len(asker.questions), 1)

    def test_force_is_a_second_dangerous_question_and_hits_only_the_survivors(self):
        ctx, asker = make_ctx("ask", True, True)
        survivors = self.procs[1:]
        terminate = mock.Mock()
        waits = iter([survivors, []])
        with self.patched(_wait_gone=lambda ctx, procs, seconds: next(waits), _terminate=terminate):
            running.close_for_update(ctx, self.app, self.progress)
        first, second = asker.questions
        self.assertTrue(second.danger)
        self.assertEqual(second.template, "{name} did not close. Force it to close?")
        self.assertEqual(second.values["processes"], "helper.exe")
        terminate.assert_called_once_with(survivors)

    def test_declining_the_force_question_cancels_without_terminating(self):
        ctx, _ = make_ctx("auto", False)
        terminate = mock.Mock()
        with self.patched(_wait_gone=lambda ctx, procs, seconds: self.procs[:1], _terminate=terminate):
            with self.assertRaises(Cancelled) as raised:
                running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual(raised.exception.values, {"name": "Demo"})
        terminate.assert_not_called()

    def test_a_process_that_survives_termination_cancels_the_update(self):
        ctx, _ = make_ctx("auto", True)
        with self.patched(_wait_gone=lambda ctx, procs, seconds: self.procs[:1]):
            with self.assertRaises(Cancelled):
                running.close_for_update(ctx, self.app, self.progress)

    def test_no_window_to_close_skips_the_grace_period(self):
        ctx, _ = make_ctx("auto", True)
        wait = mock.Mock(return_value=[])
        with self.patched(_post_close=lambda procs: 0, _alive=lambda p: p.pid == 101, _wait_gone=wait):
            running.close_for_update(ctx, self.app, self.progress)
        self.assertEqual(wait.call_count, 1)       # only the check after the forced termination
        self.assertEqual(wait.call_args.args[2], running.KILL_SECONDS)

    def test_long_process_lists_are_shortened(self):
        many = [running.RunningProcess(i, f"p{i:02}.exe", rf"C:\x\p{i:02}.exe") for i in range(12)]
        text = running._names(many)
        self.assertTrue(text.startswith("p00.exe, p01.exe") and text.endswith(", …"))
        self.assertEqual(running._names(many[:1] * 3), "p00.exe")


class RootsTest(unittest.TestCase):
    """Which folders count as 'the app's own'. Reads real folder locations; touches no process."""

    def test_folders_that_hold_many_apps_do_not_count(self):
        for folder in (os.environ["ProgramFiles"], os.environ["SystemRoot"], os.path.join(os.environ["SystemRoot"], "System32"),
                       os.environ["USERPROFILE"], os.environ["SystemDrive"] + "\\", os.environ["LOCALAPPDATA"],
                       os.path.join(os.environ["ProgramFiles"], "Common Files")):
            with self.subTest(folder=folder):
                self.assertEqual(running.install_roots(make_app(folder)), [])

    def test_a_too_broad_folder_finds_no_process_and_does_not_scan(self):
        with mock.patch.object(running, "_snapshot", side_effect=AssertionError("must not scan")):
            self.assertEqual(running.find_running(make_app(os.environ["ProgramFiles"])), [])
            self.assertEqual(running.find_running(make_app("")), [])
            self.assertEqual(running.find_running(make_app("relative\\folder")), [])

    def test_the_app_folder_is_used(self):
        folder = os.path.join(os.environ["ProgramFiles"], "Demo App")
        self.assertEqual(running.install_roots(make_app(folder + "\\")), [guard.normalize(folder)])

    def test_icon_and_uninstaller_folders_fill_in_when_there_is_no_location(self):
        folder = os.path.join(os.environ["ProgramFiles"], "Demo App")
        app = make_app("", uninstall_string=f'"{folder}\\unins000.exe" /SILENT',
                       quiet_uninstall_string=f"{folder}\\unins000.exe /VERYSILENT")
        self.assertEqual(running.install_roots(app), [guard.normalize(folder)])

    def test_windows_installer_commands_name_no_folder(self):
        app = make_app("", uninstall_string="MsiExec.exe /X{11111111-2222-3333-4444-555555555555}")
        self.assertEqual(running.install_roots(app), [])
        app = make_app("", uninstall_string=r"C:\Windows\System32\msiexec.exe /X{11111111-2222-3333-4444-555555555555}")
        self.assertEqual(running.install_roots(app), [])

    def test_program_of_a_command_line(self):
        self.assertEqual(running._program_of(r'"C:\A B\u.exe" /S'), r"C:\A B\u.exe")
        self.assertEqual(running._program_of(r"C:\A B\u.exe /S"), r"C:\A B\u.exe")
        self.assertEqual(running._program_of(""), "")


class OwnProcessTest(unittest.TestCase):
    def test_upstream_and_its_parents_are_never_listed(self):
        rows = running._snapshot()
        chain = running._own_chain(rows)
        self.assertIn(os.getpid(), chain)
        folders = set()
        for pid in chain:
            handle = running._k32.OpenProcess(running._QUERY_LIMITED, False, pid)
            if handle:
                folders.add(os.path.dirname(running._describe(handle)[0]))
                running._k32.CloseHandle(handle)
        folders.discard("")
        app = make_app(next(iter(folders)))
        with mock.patch.object(running, "install_roots", return_value=[guard.normalize(f) for f in folders]), \
             mock.patch.object(guard, "protected_path", return_value=None):
            with mock.patch.object(running, "_own_chain", return_value=set()):
                self.assertIn(os.getpid(), {p.pid for p in running.find_running(app)})   # the test can fail
            self.assertFalse(chain & {p.pid for p in running.find_running(app)})


@unittest.skipUnless(len(RUNTIME) >= 3 and all(p.exists() for p in RUNTIME), "needs the base Python's pythonw.exe")
class ProcessTest(unittest.TestCase):
    """Real processes, all started here, from a temp folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upstream-running-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        guard_patch = exempt(self.tmp)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self.reap)
        Path(self.tmp, "child.py").write_text(CHILD, "utf-8")
        for patch in (mock.patch.object(running, "GRACE_SECONDS", 4.0), mock.patch.object(running, "KILL_SECONDS", 3.0)):
            patch.start()
            self.addCleanup(patch.stop)

    def reap(self):
        for child in self.children:      # only processes this test started
            if child.poll() is None:
                child.kill()
            child.wait(10)

    def launch(self, mode: str, name: str = "app") -> tuple[InstalledApp, subprocess.Popen]:
        folder = Path(self.tmp, name)
        folder.mkdir()
        for source in RUNTIME:
            shutil.copy2(source, folder)
        ready = folder / "ready"
        env = {**os.environ, "PYTHONHOME": str(BASE)}
        env.pop("PYTHONPATH", None)
        child = subprocess.Popen([str(folder / "pythonw.exe"), str(Path(self.tmp, "child.py")), mode, str(ready)],
                                 env=env, cwd=self.tmp, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.children.append(child)
        deadline = time.monotonic() + 20
        while not ready.exists():
            self.assertIsNone(child.poll(), "the throwaway program died on start")
            self.assertLess(time.monotonic(), deadline, "the throwaway program never became ready")
            time.sleep(0.05)
        return make_app(str(folder)), child

    def test_find_running_returns_exactly_the_throwaway(self):
        app, child = self.launch("close")
        other = make_app(str(Path(self.tmp, "elsewhere")))
        (proc,) = running.find_running(app)
        self.assertEqual((proc.pid, proc.name), (child.pid, "pythonw.exe"))
        self.assertEqual(guard.normalize(proc.path), guard.normalize(str(Path(app.install_location, "pythonw.exe"))))
        self.assertTrue(proc.started)
        self.assertEqual(running.find_running(other), [])

    def test_ask_closes_the_window_gracefully(self):
        app, child = self.launch("close")
        ctx, asker = make_ctx("ask", True)
        progress = mock.Mock()
        running.close_for_update(ctx, app, progress)
        self.assertEqual(child.wait(10), 0)                 # left through its own message loop
        self.assertEqual(len(asker.questions), 1)           # no force question
        self.assertEqual(asker.questions[0].values["processes"], "pythonw.exe")
        progress.assert_called_once_with("Closing {name}…", -1, name="Demo")

    def test_auto_closes_a_hidden_window_too(self):
        app, child = self.launch("hidden")
        ctx, asker = make_ctx("auto")
        running.close_for_update(ctx, app, mock.Mock())
        self.assertEqual(child.wait(10), 0)
        self.assertEqual(asker.questions, [])

    def test_never_leaves_it_running(self):
        app, child = self.launch("close")
        ctx, asker = make_ctx("never")
        running.close_for_update(ctx, app, mock.Mock())
        self.assertEqual(asker.questions, [])
        time.sleep(0.5)
        self.assertIsNone(child.poll())

    def test_declining_leaves_it_running(self):
        app, child = self.launch("close")
        ctx, _ = make_ctx("ask", False)
        with self.assertRaises(Cancelled):
            running.close_for_update(ctx, app, mock.Mock())
        time.sleep(0.5)
        self.assertIsNone(child.poll())

    def test_a_program_that_ignores_close_is_only_killed_after_a_second_yes(self):
        app, child = self.launch("stubborn")
        ctx, asker = make_ctx("auto", True)
        with mock.patch.object(running, "GRACE_SECONDS", 1.0):
            running.close_for_update(ctx, app, mock.Mock())
        self.assertEqual(child.wait(10), 1)                 # TerminateProcess(…, 1)
        (force,) = asker.questions
        self.assertTrue(force.danger)

    def test_declining_the_force_question_keeps_it_alive(self):
        app, child = self.launch("stubborn")
        ctx, asker = make_ctx("ask", True, False)
        with mock.patch.object(running, "GRACE_SECONDS", 1.0):
            with self.assertRaises(Cancelled) as raised:
                running.close_for_update(ctx, app, mock.Mock())
        self.assertEqual(raised.exception.template, "Update cancelled: {name} is still running.")
        self.assertEqual(len(asker.questions), 2)
        self.assertIsNone(child.poll())

    def test_a_program_without_windows_goes_straight_to_the_force_question(self):
        app, child = self.launch("nowindow")
        ctx, asker = make_ctx("auto", True)
        started = time.monotonic()
        with mock.patch.object(running, "GRACE_SECONDS", 30.0):
            running.close_for_update(ctx, app, mock.Mock())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(child.wait(10), 1)
        self.assertTrue(asker.questions[0].danger)

    def test_cancelling_while_waiting_stops_at_once(self):
        app, child = self.launch("stubborn")
        ctx, asker = make_ctx("auto")
        threading.Timer(0.6, ctx.cancel.cancel).start()
        started = time.monotonic()
        with mock.patch.object(running, "GRACE_SECONDS", 30.0):
            with self.assertRaises(Cancelled):
                running.close_for_update(ctx, app, mock.Mock())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(asker.questions, [])
        self.assertIsNone(child.poll())

    def test_only_the_apps_own_processes_are_closed(self):
        app, child = self.launch("close", "one")
        _, bystander = self.launch("close", "two")
        ctx, _ = make_ctx("auto")
        running.close_for_update(ctx, app, mock.Mock())
        self.assertEqual(child.wait(10), 0)
        time.sleep(0.3)
        self.assertIsNone(bystander.poll())

    def test_a_reused_pid_is_not_mistaken_for_the_process(self):
        app, child = self.launch("stubborn")
        (real,) = running.find_running(app)
        wrong_program = running.RunningProcess(real.pid, "other.exe", r"C:\Elsewhere\other.exe", real.started)
        wrong_start = running.RunningProcess(real.pid, real.name, real.path, real.started + 1)
        for impostor in (wrong_program, wrong_start):
            with self.subTest(impostor=impostor):
                self.assertFalse(running._alive(impostor))
                self.assertEqual(running._post_close([impostor]), 0)
                running._terminate([impostor])
                time.sleep(0.3)
                self.assertIsNone(child.poll())
        self.assertTrue(running._alive(real))


if __name__ == "__main__":
    unittest.main()
