"""One UAC per batch: token checks, the relaunch command line, waiting for the old process, and which updates will
ask for administrator rights. Nothing here can raise a UAC prompt or start a process with runas: ShellExecuteEx is
replaced in every test, and the only process started is a throwaway Python sleeper that the test ends itself."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from upstream import elevation, i18n
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update

CREATE_NO_WINDOW = 0x08000000


class FakeShell:
    """Stands in for shell32: records what ShellExecuteExW was given and fails the way the user declining does."""

    def __init__(self, error=0):
        self.error = error
        self.calls: list[dict] = []
        self.ShellExecuteExW = self._execute

    def _execute(self, ref):
        info = ref._obj
        self.calls.append({"verb": info.lpVerb, "file": info.lpFile, "params": info.lpParameters,
                           "dir": info.lpDirectory, "show": info.nShow, "mask": info.fMask})
        if self.error:
            ctypes.set_last_error(self.error)
            return 0
        return 1


class FakeToken:
    """advapi32 + the two kernel32 calls around it, answering GetTokenInformation from a table."""

    def __init__(self, values=None, opens=True):
        self.values = values or {}        # TOKEN_INFORMATION_CLASS -> DWORD; a class that is absent makes the call fail
        self.opens = opens
        self.closed = 0
        self.advapi32 = types.SimpleNamespace(OpenProcessToken=self._open, GetTokenInformation=self._get)
        self.kernel32 = types.SimpleNamespace(GetCurrentProcess=lambda: -1, CloseHandle=self._close)

    def _open(self, process, access, ref):
        return 1 if self.opens else 0

    def _get(self, token, info_class, ref, size, size_ref):
        if info_class not in self.values:
            return 0
        ref._obj.value = self.values[info_class]
        return 1

    def _close(self, handle):
        self.closed += 1

    def patched(self):
        return mock.patch.multiple(elevation, _advapi32=self.advapi32, _kernel32=self.kernel32)


class ElevationTestCase(unittest.TestCase):
    def setUp(self):
        # Whatever a test forgets to replace still cannot show a prompt: shell32 itself is a fake that says "declined".
        self.shell = FakeShell(error=1223)
        patcher = mock.patch.object(elevation, "_shell32", self.shell)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(i18n.set_language, "en")


def make_status(name="Demo", *, scope="machine", reg_key=None, update="exe", install_kind="", **upd) -> AppStatus:
    app = InstalledApp(reg_key=reg_key or rf"HKLM64\{name}", name=name, version="1.0", publisher="Acme", scope=scope,
                       arch="x64", install_kind=install_kind)
    if update == "exe":
        update = Update("github", "2.0", "acme/demo", download_url="https://example.invalid/demo.exe",
                        file_name="demo-setup.exe", **upd)
    return AppStatus(app, update=update or None)


class TokenTests(ElevationTestCase):
    def test_elevated(self):
        fake = FakeToken({elevation._TOKEN_ELEVATION: 1})
        with fake.patched():
            self.assertTrue(elevation.is_elevated())
        self.assertEqual(fake.closed, 1)

    def test_not_elevated(self):
        fake = FakeToken({elevation._TOKEN_ELEVATION: 0})
        with fake.patched():
            self.assertFalse(elevation.is_elevated())
        self.assertEqual(fake.closed, 1)

    def test_token_cannot_be_read(self):
        for fake in (FakeToken(), FakeToken({elevation._TOKEN_ELEVATION: 1}, opens=False)):
            with fake.patched():
                self.assertFalse(elevation.is_elevated())
                self.assertFalse(elevation.can_relaunch_elevated())

    def test_can_relaunch_only_for_an_admin_account_that_is_not_elevated(self):
        for kind, expected in ((3, True), (2, False), (1, False)):   # limited, full, default (standard user)
            fake = FakeToken({elevation._TOKEN_ELEVATION_TYPE: kind})
            with self.subTest(kind=kind), fake.patched():
                self.assertEqual(elevation.can_relaunch_elevated(), expected)

    def test_the_real_token_answers_with_booleans(self):
        self.assertIsInstance(elevation.is_elevated(), bool)
        self.assertIsInstance(elevation.can_relaunch_elevated(), bool)
        if elevation.is_elevated():
            self.assertFalse(elevation.can_relaunch_elevated())


class RelaunchTests(ElevationTestCase):
    def run_relaunch(self, extra=(), error=0, command=None):
        calls = []

        def fake_execute(verb, file, params, directory):
            calls.append((verb, file, params, directory))
            return error

        with mock.patch.object(elevation, "_shell_execute", fake_execute):
            if command is not None:
                with mock.patch.object(elevation, "_own_command", return_value=command):
                    result = elevation.relaunch_elevated(extra)
            else:
                result = elevation.relaunch_elevated(extra)
        return result, calls

    def test_source_run_uses_pythonw_and_main_py(self):
        result, calls = self.run_relaunch()
        self.assertTrue(result)
        (verb, file, params, directory), = calls
        self.assertEqual(verb, "runas")
        self.assertIn(Path(file).name.lower(), ("pythonw.exe", Path(sys.executable).name.lower()))
        main = Path(directory, "main.py")
        self.assertTrue(main.is_file())
        self.assertEqual(params, subprocess.list2cmdline([str(main), "--elevated", "--wait-pid", str(os.getpid())]))

    def test_frozen_run_starts_the_exe_itself(self):
        exe = r"C:\Apps\Upstream\Upstream.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
            result, calls = self.run_relaunch(("--minimized",))
        self.assertTrue(result)
        self.assertEqual(calls, [("runas", exe, f"--elevated --wait-pid {os.getpid()} --minimized", r"C:\Apps\Upstream")])

    def test_extra_arguments_are_quoted(self):
        _, calls = self.run_relaunch(("--open", r"C:\My Folder\x.json", 7), command=("prog.exe", [], "."))
        self.assertEqual(calls[0][2], f'--elevated --wait-pid {os.getpid()} --open "C:\\My Folder\\x.json" 7')

    def test_declining_returns_false(self):
        with self.assertLogs("upstream.elevation", "INFO") as logs:
            result, calls = self.run_relaunch(error=1223)
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("declined", logs.output[0])

    def test_other_failures_return_false_and_are_logged(self):
        with self.assertLogs("upstream.elevation", "WARNING") as logs:
            result, _ = self.run_relaunch(error=2)
        self.assertFalse(result)
        self.assertIn("error 2", logs.output[0])

    def test_missing_main_py_is_not_attempted(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("upstream.elevation", "WARNING"):
            result, calls = self.run_relaunch(command=("pythonw.exe", [str(Path(tmp, "main.py"))], tmp))
        self.assertFalse(result)
        self.assertEqual(calls, [])

    def test_python_exe_is_swapped_for_pythonw(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "python.exe").write_bytes(b"")
            Path(tmp, "pythonw.exe").write_bytes(b"")
            with mock.patch.object(sys, "executable", str(Path(tmp, "python.exe"))):
                program, lead, directory = elevation._own_command()
        self.assertEqual(Path(program).name, "pythonw.exe")
        self.assertEqual(Path(lead[0]).name, "main.py")
        self.assertTrue(Path(directory, "main.py").is_file())

    def test_shell_execute_asks_for_runas_without_waiting(self):
        result = elevation._shell_execute("runas", r"C:\x\Upstream.exe", "--elevated", r"C:\x")
        self.assertEqual(result, 1223)            # the fake shell32 says declined
        (call,) = self.shell.calls
        self.assertEqual(call, {"verb": "runas", "file": r"C:\x\Upstream.exe", "params": "--elevated", "dir": r"C:\x",
                                "show": 1, "mask": elevation._SEE_MASK_NOASYNC})
        self.shell.error = 0
        self.assertEqual(elevation._shell_execute("runas", "a.exe", "", "."), 0)

    def test_nothing_ran_for_real(self):
        # The guard in setUp is what makes every test above safe: shell32 never saw a real call.
        self.assertIsInstance(elevation._shell32, FakeShell)


class WaitForExitTests(ElevationTestCase):
    def start_sleeper(self, seconds):
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"],
                                creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def test_times_out_while_the_process_runs_then_sees_it_end(self):
        proc = self.start_sleeper(60)
        started = time.monotonic()
        self.assertFalse(elevation.wait_for_exit(proc.pid, 0.3))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        proc.kill()
        proc.wait()
        self.assertTrue(elevation.wait_for_exit(proc.pid, 10))

    def test_returns_as_soon_as_the_process_exits(self):
        proc = self.start_sleeper(1)
        started = time.monotonic()
        self.assertTrue(elevation.wait_for_exit(proc.pid, 20))
        self.assertLess(time.monotonic() - started, 10)

    def test_a_process_that_does_not_exist_is_already_gone(self):
        self.assertTrue(elevation.wait_for_exit(0xFFFFFFF0, 0.2))

    def test_nothing_to_wait_for(self):
        self.assertTrue(elevation.wait_for_exit(0))
        self.assertTrue(elevation.wait_for_exit(-5))

    def test_a_process_that_cannot_be_opened_is_not_reported_gone(self):
        def access_denied(*args):
            ctypes.set_last_error(5)
            return None

        with mock.patch.object(elevation._kernel32, "OpenProcess", access_denied):
            self.assertFalse(elevation.wait_for_exit(1234, 0.1))


class NeedsAdminTests(ElevationTestCase):
    def test_machine_installers_ask(self):
        self.assertTrue(elevation.needs_admin(make_status()))
        self.assertTrue(elevation.needs_admin(make_status(install_kind="inno")))
        winget = Update("winget", "2.0", "Acme.Demo")
        self.assertTrue(elevation.needs_admin(make_status(update=winget)))

    def test_user_copies_do_not(self):
        self.assertFalse(elevation.needs_admin(make_status(scope="user", reg_key=r"HKCU\Demo")))

    def test_store_and_msix_do_not(self):
        self.assertFalse(elevation.needs_admin(make_status(reg_key=r"WINGET\Microsoft.WindowsTerminal")))
        self.assertFalse(elevation.needs_admin(make_status(install_kind="msix")))
        self.assertFalse(elevation.needs_admin(make_status(installer_type="msix")))
        self.assertFalse(elevation.needs_admin(make_status(installer_type="APPX")))
        msix = Update("github", "2.0", "acme/demo", download_url="https://example.invalid/d.MSIXBundle",
                      file_name="Demo_2.0.MSIXBundle")
        self.assertFalse(elevation.needs_admin(make_status(update=msix)))

    def test_nothing_pending_or_nothing_to_install(self):
        self.assertFalse(elevation.needs_admin(make_status(update=None)))
        link_only = Update("site", "2.0", "https://example.invalid/download")
        self.assertFalse(elevation.needs_admin(make_status(update=link_only)))
        with_file = Update("site", "2.0", "https://example.invalid/download", download_url="https://example.invalid/a.exe")
        self.assertTrue(elevation.needs_admin(make_status(update=with_file)))

    def test_count(self):
        statuses = [make_status("A"), make_status("B"), make_status("User", scope="user", reg_key=r"HKCU\User"),
                    make_status("Store", reg_key=r"WINGET\Pub.Store"), make_status("Done", update=None),
                    make_status("M", installer_type="msix")]
        with mock.patch.object(elevation, "is_elevated", return_value=False):
            self.assertEqual(elevation.machine_installs_pending(statuses), 2)
            self.assertEqual(elevation.machine_installs_pending([]), 0)

    def test_nothing_asks_once_elevated(self):
        with mock.patch.object(elevation, "is_elevated", return_value=True):
            self.assertEqual(elevation.machine_installs_pending([make_status("A"), make_status("B")]), 0)

    def test_accepts_any_iterable(self):
        with mock.patch.object(elevation, "is_elevated", return_value=False):
            self.assertEqual(elevation.machine_installs_pending(iter([make_status("A")])), 1)


class TextTests(ElevationTestCase):
    def test_banner_english(self):
        self.assertEqual(elevation.banner_text(1), "1 update needs administrator rights. "
                                                   "Restart Upstream as administrator to approve it with one prompt.")
        self.assertTrue(elevation.banner_text(3).startswith("3 updates need administrator rights."))

    def test_banner_persian(self):
        i18n.set_language("fa")
        text = elevation.banner_text(3)
        self.assertIn("\u06f3", text)
        self.assertIn("\u0628\u0647\u200c\u0631\u0648\u0632\u0631\u0633\u0627\u0646\u06cc", text)
        self.assertNotIn("administrator", text)
        self.assertNotEqual(i18n.tr(elevation.RESTART_LABEL), elevation.RESTART_LABEL)


if __name__ == "__main__":
    unittest.main()
