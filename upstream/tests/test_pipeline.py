"""The update pipeline with every external step replaced by a fake: order of steps, cancel, ask, what is compared
with what, and what becomes of the downloaded installer."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from upstream import i18n, installer, updater
from upstream.context import CancelToken, UpdateContext
from upstream.errors import Cancelled, InstallError, StoppedWaiting
from upstream.exitcodes import Outcome
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update

DECLINED = "Not installed: you declined a file that wasn't signed by the app's usual publisher."
INSTALLED = types.SimpleNamespace(kind="installed", ok=True, template="Installed {version}",
                                  values={"version": "2.0"}, version="2.0")


def make_status(source="github"):
    app = InstalledApp(reg_key=r"HKLM64\X", name="Demo", version="1.0", publisher="Demo Inc", scope="machine", arch="x64")
    upd = Update(source, "2.0", "o/r", download_url="https://example.invalid/demo.exe", file_name="demo.exe")
    return AppStatus(app, update=upd)


def asking(action="ask"):
    return mock.Mock(action=action, template="{name} was signed by someone else", values={"name": "Demo"}, detail="Be careful")


def run_direct(test: unittest.TestCase, steps=(0,), signature=("Valid", "Demo Inc"), decision=None, answer=None,
               detect="msi", refuse=None, close=None, ctx=None, verdict=INSTALLED, delete_fails=False):
    """Run one direct update on a real file in a temp folder. `steps` are what installer.install does on each run
    (an exit code, or an exception to raise). Returns what happened: result, calls, path, remember, checks, questions."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "demo.exe"
    path.write_bytes(b"MZ")
    run = types.SimpleNamespace(path=path, calls=[], checks=[], questions=[], snapshots=[], remember=mock.Mock())
    queue = list(steps)

    def install(*a, **k):
        run.calls.append("install")
        step = queue.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def snapshot(app):
        run.calls.append("snapshot")
        run.snapshots.append(object())
        return run.snapshots[-1]

    def check(app, before, outcome, expected, cancel=None):
        run.checks.append((before, outcome, cancel))
        return verdict

    def detect_kind(p, hint=None):
        run.calls.append("detect")
        if isinstance(detect, BaseException):
            raise detect
        return detect

    def close_apps(ctx_, app, progress):
        run.calls.append("close")
        if close is not None:
            raise close

    def ask_fn(question):
        run.questions.append(question)
        return answer

    ctx = ctx or UpdateContext(ask_fn=ask_fn)
    plan = mock.Mock(warnings=[], refuse=refuse, extra_args="")
    patches = [
        mock.patch.object(updater.net, "preflight", lambda c: run.calls.append("preflight")),
        mock.patch.object(updater.net, "download", lambda *a, **k: (run.calls.append("download"), path)[1]),
        mock.patch.object(updater.installer, "signature", lambda p: signature),
        mock.patch.object(updater.installer, "detect", detect_kind),
        mock.patch.object(updater.installer, "install", install),
        mock.patch.object(updater.trust, "evaluate", lambda *a: decision or mock.Mock(action="allow")),
        mock.patch.object(updater.trust, "remember", run.remember),
        mock.patch.object(updater.inplace, "plan", lambda *a: plan),
        mock.patch.object(updater.running, "close_for_update", close_apps),
        mock.patch.object(updater.verify, "snapshot", snapshot),
        mock.patch.object(updater.verify, "check", check),
        mock.patch.object(updater, "BUSY_WAIT_SECONDS", 0),
    ]
    if delete_fails:
        patches.append(mock.patch.object(Path, "unlink", side_effect=PermissionError("held by the antivirus")))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        if delete_fails:
            stack.enter_context(test.assertLogs("upstream.update", "WARNING"))
        run.result = updater.Updater().run(make_status(), lambda *a, **k: None, ctx)
    run.ctx = ctx
    return run


class PipelineTest(unittest.TestCase):
    def test_steps_run_in_order(self):
        run = run_direct(self)
        ok, template, values, version = run.result
        self.assertTrue(ok)
        self.assertEqual(run.calls, ["preflight", "download", "detect", "close", "snapshot", "install"])
        self.assertEqual(version, "2.0")

    def test_failed_exit_code_is_reported(self):
        run = run_direct(self, steps=[1603])
        ok, template, values, version = run.result
        self.assertFalse(ok)
        self.assertEqual(values, {"code": 1603})
        self.assertEqual(version, "1.0")

    def test_cancelled_outcome_stops_without_trying_next_source(self):
        status = make_status()
        status.fallbacks = [Update("winget", "2.0", "Demo.Demo")]
        tried = []
        with mock.patch.object(updater.net, "preflight"), \
             mock.patch.object(updater.Updater, "_direct", lambda self, *a: updater.Attempt(Outcome("cancelled", "Cancelled."))), \
             mock.patch.object(updater.Updater, "_winget", lambda self, *a: tried.append("winget")):
            ok, template, values, _ = updater.Updater().run(status, lambda *a, **k: None, UpdateContext())
        self.assertFalse(ok)
        self.assertEqual(tried, [])


class SnapshotTest(unittest.TestCase):
    """verify.snapshot is the baseline verify.check compares with, so it must be taken as late as possible."""

    def test_taken_right_before_the_installer_not_before_the_download_and_the_questions(self):
        run = run_direct(self, decision=asking(), answer=True)
        self.assertEqual(run.calls[-2:], ["snapshot", "install"])
        self.assertEqual(run.calls.count("snapshot"), 1)
        before, outcome, cancel = run.checks[0]
        self.assertIs(before, run.snapshots[0])
        self.assertIs(cancel, run.ctx.cancel)

    def test_taken_again_for_every_run_after_a_busy_wait(self):
        run = run_direct(self, steps=[1618, 1618, 0])
        self.assertEqual(run.calls[-6:], ["snapshot", "install"] * 3)
        self.assertEqual(len(run.snapshots), 3)
        self.assertIs(run.checks[0][0], run.snapshots[-1])

    def test_winget_takes_it_right_before_winget_runs(self):
        status = AppStatus(make_status().app, update=Update("winget", "2.0", "Demo.Demo"))
        calls = []
        snapshot = object()
        checked = []

        def fake_winget(origin, mode, on_progress, on_line, **kwargs):
            calls.append("winget")
            return 0, ""

        with mock.patch.object(updater.net, "preflight"), \
             mock.patch.object(updater.running, "close_for_update", lambda *a: calls.append("close")), \
             mock.patch.object(updater.verify, "snapshot", lambda app: (calls.append("snapshot"), snapshot)[1]), \
             mock.patch.object(updater.winget, "update", fake_winget), \
             mock.patch.object(updater.verify, "check",
                               lambda app, before, outcome, expected, cancel=None: (checked.append(before), INSTALLED)[1]):
            ok, _, _, _ = updater.Updater().run(status, lambda *a, **k: None, UpdateContext())
        self.assertTrue(ok)
        self.assertEqual(calls, ["close", "snapshot", "winget"])
        self.assertEqual(checked, [snapshot])


class RollbackTest(unittest.TestCase):
    def test_winget_is_told_the_version_and_verify_that_it_is_a_rollback(self):
        status = AppStatus(make_status().app, update=Update("winget", "1.5", "Demo.Demo", winget_mode="rollback"))
        seen = {}

        def fake_winget(origin, mode, on_progress, on_line, **kwargs):
            seen["winget"] = (mode, kwargs.get("version"))
            return 0, ""

        def fake_check(app, before, outcome, expected, cancel=None, **extra):
            seen["check"] = extra
            return INSTALLED

        with mock.patch.object(updater.net, "preflight"),              mock.patch.object(updater.running, "close_for_update", lambda *a: None),              mock.patch.object(updater.verify, "snapshot", lambda app: object()),              mock.patch.object(updater.winget, "update", fake_winget),              mock.patch.object(updater.verify, "check", fake_check):
            ok, _, _, _ = updater.Updater().run(status, lambda *a, **k: None, UpdateContext())
        self.assertTrue(ok)
        self.assertEqual(seen["winget"], ("rollback", "1.5"))
        self.assertEqual(seen["check"], {"downgrade_to": "1.5"})

    def test_an_ordinary_upgrade_sends_neither(self):
        status = AppStatus(make_status().app, update=Update("winget", "2.0", "Demo.Demo"))
        seen = {}

        def fake_winget(origin, mode, on_progress, on_line, **kwargs):
            seen["version"] = kwargs.get("version", "absent")
            return 0, ""

        with mock.patch.object(updater.net, "preflight"),              mock.patch.object(updater.running, "close_for_update", lambda *a: None),              mock.patch.object(updater.verify, "snapshot", lambda app: object()),              mock.patch.object(updater.winget, "update", fake_winget),              mock.patch.object(updater.verify, "check", lambda app, before, outcome, expected, cancel=None: INSTALLED):
            updater.Updater().run(status, lambda *a, **k: None, UpdateContext())
        self.assertIsNone(seen["version"])


class DownloadedFileTest(unittest.TestCase):
    """The installer is deleted once its source is finished, whatever happened, unless it may still be running."""

    def assertDeleted(self, run, message=""):
        self.assertFalse(run.path.exists(), message or "the downloaded installer was left behind")

    def test_deleted_after_success(self):
        self.assertDeleted(run_direct(self))

    def test_deleted_after_every_outcome_that_is_not_a_success(self):
        cases = {
            "failed": [1603], "needs_admin": [740], "disk_full": [112], "already_installed": [1638],
            "cancelled": [1602], "busy for good": [1618] * (updater.BUSY_RETRIES + 1),
        }
        for name, steps in cases.items():
            with self.subTest(name):
                run = run_direct(self, steps=steps, verdict=types.SimpleNamespace(
                    kind="unchanged", ok=False, template="Still {version}", values={"version": "1.0"}, version="1.0"))
                self.assertFalse(run.result[0])
                self.assertDeleted(run)

    def test_deleted_when_uac_was_declined(self):
        run = run_direct(self, steps=[Cancelled("Administrator permission (UAC) was declined.")])
        self.assertEqual((run.result[0], run.result[1]), (False, "Administrator permission (UAC) was declined."))
        self.assertDeleted(run)

    def test_deleted_when_the_file_is_not_an_installer(self):
        run = run_direct(self, detect=InstallError("The downloaded file is not a Windows installer."))
        self.assertEqual(run.result[1], "The downloaded file is not a Windows installer.")
        self.assertNotIn("install", run.calls)
        self.assertDeleted(run)

    def test_deleted_when_the_signature_is_broken_blocked_declined_or_the_plan_refuses(self):
        cases = {
            "tampered": dict(signature=("HashMismatch", "Demo Inc")),
            "blocked": dict(decision=mock.Mock(action="block", template="Blocked: {name}", values={"name": "Demo"})),
            "declined": dict(decision=asking(), answer=False),
            "refused": dict(refuse=("Would install a second copy of {name}", {"name": "Demo"})),
            "app still open": dict(close=Cancelled("Update cancelled: {name} is still running.", name="Demo")),
        }
        for name, extra in cases.items():
            with self.subTest(name):
                run = run_direct(self, **extra)
                self.assertFalse(run.result[0])
                self.assertNotIn("install", run.calls)
                self.assertDeleted(run)

    def test_kept_when_upstream_stopped_waiting_for_an_installer_that_may_still_be_running(self):
        for error in (StoppedWaiting("Stopped waiting for the installer. It may still be running."),
                      StoppedWaiting("The installer was still running after {minutes} minutes, so Upstream stopped "
                                     "waiting for it. It may still be running.", minutes="30")):
            with self.subTest(error.template):
                run = run_direct(self, steps=[error])
                self.assertEqual((run.result[0], run.result[1]), (False, error.template))
                self.assertTrue(run.path.exists())

    def test_stopping_the_wait_is_a_cancellation_from_the_installer_module_too(self):
        self.assertTrue(issubclass(StoppedWaiting, Cancelled))
        self.assertIs(installer.StoppedWaiting, StoppedWaiting)

    def test_a_file_that_cannot_be_deleted_does_not_turn_success_into_failure(self):
        run = run_direct(self, delete_fails=True)
        self.assertEqual((run.result[0], run.result[3]), (True, "2.0"))

    def test_a_file_that_cannot_be_deleted_does_not_hide_a_declined_question(self):
        run = run_direct(self, decision=asking(), answer=False, delete_fails=True)
        self.assertEqual((run.result[0], run.result[1]), (False, DECLINED))

    def test_discard_swallows_and_logs_os_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.exe"
            updater._discard(path)                       # nothing there
            path.write_bytes(b"MZ")
            updater._discard(path)
            self.assertFalse(path.exists())
            with mock.patch.object(Path, "unlink", side_effect=PermissionError("in use")), \
                 self.assertLogs("upstream.update", "WARNING"):
                updater._discard(path)


class PublisherTest(unittest.TestCase):
    def test_the_declined_question_says_which_files_it_is_about(self):
        run = run_direct(self, decision=asking(), answer=False)
        self.assertEqual((run.result[0], run.result[1]), (False, DECLINED))
        self.assertIn(DECLINED, i18n.FA)
        self.assertEqual(len(run.questions), 1)
        run.remember.assert_not_called()

    def test_remember_is_told_what_the_user_approved(self):
        for name, extra, approved in (("asked and said yes", dict(decision=asking(), answer=True), True),
                                      ("not asked", {}, False)):
            with self.subTest(name):
                run = run_direct(self, **extra)
                self.assertTrue(run.result[0])
                run.remember.assert_called_once()
                self.assertEqual(run.remember.call_args.args[1:], (make_status().app, "Demo Inc", "Valid"))
                self.assertEqual(run.remember.call_args.kwargs, {"approved": approved})

    def test_a_signature_that_is_not_valid_is_still_passed_on_for_trust_to_judge(self):
        run = run_direct(self, signature=("NotTrusted", "Demo Inc"), decision=asking(), answer=True)
        self.assertEqual(run.remember.call_args.args[3], "NotTrusted")
        self.assertEqual(run.remember.call_args.kwargs, {"approved": True})

    def test_an_unsigned_file_has_no_publisher_to_remember(self):
        run = run_direct(self, signature=("NotSigned", ""))
        self.assertTrue(run.result[0])
        run.remember.assert_not_called()

    def test_nothing_is_remembered_when_the_install_did_not_happen(self):
        run = run_direct(self, steps=[1638])           # "another version is already installed", registry shows 2.0
        self.assertTrue(run.result[0])
        run.remember.assert_not_called()


class StoppingTheWaitTest(unittest.TestCase):
    def test_cancelling_and_the_time_limit_raise_stopped_waiting(self):
        token = CancelToken()
        token.cancel()
        with self.assertRaises(StoppedWaiting) as raised:
            installer._wait_for(lambda seconds: False, token)
        self.assertEqual(raised.exception.template, "Stopped waiting for the installer. It may still be running.")
        with self.assertRaises(StoppedWaiting) as raised:
            installer._wait_for(lambda seconds: time.sleep(seconds) or False, None, 0.0001)
        self.assertIn("{minutes}", raised.exception.template)

    def test_a_declined_uac_prompt_is_a_plain_cancellation_so_its_file_is_deleted(self):
        shell32 = mock.Mock(ShellExecuteExW=mock.Mock(return_value=False))
        with mock.patch.object(installer, "_shell32", shell32), \
             mock.patch.object(installer.ctypes, "get_last_error", return_value=1223):
            with self.assertRaises(Cancelled) as raised:
                installer._shell_run("setup.exe", "/S")
        self.assertNotIsInstance(raised.exception, StoppedWaiting)


class SystemToolTest(unittest.TestCase):
    def test_powershell_and_msiexec_are_started_by_full_path(self):
        system32 = Path(os.environ["SystemRoot"], "System32")
        self.assertEqual(Path(installer._system_tool("msiexec.exe")), system32 / "msiexec.exe")
        self.assertEqual(Path(installer._system_tool("powershell.exe", "WindowsPowerShell", "v1.0")),
                         system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        with mock.patch.object(installer, "_shell_run", return_value=0) as run:
            installer.install(Path("setup.msi"), "msi", "machine")
        self.assertEqual(Path(run.call_args.args[0]), system32 / "msiexec.exe")
        self.assertTrue(run.call_args.args[1].startswith('/i "setup.msi"'))

    def test_the_bare_name_is_the_fallback_when_the_file_is_missing(self):
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(os.environ, {"SystemRoot": empty}):
            self.assertEqual(installer._system_tool("msiexec.exe"), "msiexec.exe")
            self.assertEqual(installer._system_tool("powershell.exe", "WindowsPowerShell", "v1.0"), "powershell.exe")

    def test_other_installers_run_by_their_own_path_and_the_uninstaller_call_keeps_its_shape(self):
        with mock.patch.object(installer, "_shell_run", return_value=0) as run:
            installer.install(Path(r"C:\temp\setup.exe"), "nullsoft", "machine")
        self.assertEqual(run.call_args.args, (r"C:\temp\setup.exe", "/S"))


class PathHandlingTest(unittest.TestCase):
    """The file's path travels in an environment variable, never in the PowerShell text."""

    @classmethod
    def setUpClass(cls):
        system32 = Path(os.environ["SystemRoot"], "System32")
        cls.signed = next((c for c in (system32 / "notepad.exe", system32 / "cmd.exe", Path(sys.executable))
                           if c.is_file() and installer.signature(c)[0] == "Valid"), None)

    def copy_into(self, folder_name: str) -> Path:
        if self.signed is None:
            self.skipTest("no signed executable to copy")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / folder_name
        folder.mkdir()
        return Path(shutil.copy2(self.signed, folder / "setup.exe"))

    def test_folders_with_quote_characters_are_read_properly(self):
        for name in ("O\u2019Neil", "O'Neil", "a [1] & b $x %1"):
            with self.subTest(name):
                self.assertEqual(installer.signature(self.copy_into(name))[0], "Valid")

    def test_a_crafted_folder_name_cannot_run_a_command(self):
        marker = "injected.txt"
        crafted = f"\u2019; Set-Content {marker} x; \u2018"
        path = self.copy_into(crafted)
        here = Path.cwd()
        with tempfile.TemporaryDirectory() as cwd:
            os.chdir(cwd)                                   # the injected command would write its file here
            try:
                installer.signature(path)
                self.assertFalse(Path(cwd, marker).exists())
            finally:
                os.chdir(here)

    def test_signature_keeps_the_path_out_of_the_command_text(self):
        path = Path("C:/temp/O\u2019Neil/setup.exe")
        done = subprocess.CompletedProcess([], 0, stdout="Valid|CN=Demo Inc, O=Demo\n", stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=done) as run:
            self.assertEqual(installer.signature(path), ("Valid", "Demo Inc"))
        command = run.call_args.args[0]
        self.assertNotIn(str(path), " ".join(command))
        self.assertIn("$env:UPSTREAM_PATH", command[-1])
        self.assertEqual(run.call_args.kwargs["env"]["UPSTREAM_PATH"], str(path))
        self.assertEqual(Path(command[0]).name.lower(), "powershell.exe")
        self.assertTrue(Path(command[0]).is_absolute())

    def test_add_appx_reads_utf8_and_keeps_the_path_out_of_the_command_text(self):
        path = Path("C:/temp/O\u2019Neil/app.msix")
        proc = mock.Mock(returncode=1)
        proc.stderr.read.return_value = "Deployment failed with HRESULT: 0x80073CF0\nNOTE: more\n"
        with mock.patch.object(installer.subprocess, "Popen", return_value=proc) as popen:
            with self.assertRaises(InstallError) as raised:
                installer._add_appx(path, None, None)
        command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertNotIn(str(path), " ".join(command))
        self.assertIn("[Console]::OutputEncoding = [Text.Encoding]::UTF8", command[-1])
        self.assertIn("$env:UPSTREAM_PATH", command[-1])
        self.assertEqual(kwargs["env"]["UPSTREAM_PATH"], str(path))
        self.assertEqual((kwargs["encoding"], kwargs["errors"]), ("utf-8", "replace"))
        self.assertEqual(raised.exception.values, {"error": "Deployment failed with HRESULT: 0x80073CF0"})

    def test_add_appx_without_error_text_still_reports_the_failure(self):
        proc = mock.Mock(returncode=1)
        proc.stderr.read.return_value = None
        with mock.patch.object(installer.subprocess, "Popen", return_value=proc):
            with self.assertRaises(InstallError) as raised:
                installer._add_appx(Path("app.msix"), None, None)
        self.assertEqual(raised.exception.template, "Couldn't install the MSIX package.")


if __name__ == "__main__":
    unittest.main()
