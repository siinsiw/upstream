"""Exit code meaning (exitcodes.py) and how Updater.run reacts to each outcome. No installer is ever started."""
from __future__ import annotations

import string
import unittest
from pathlib import Path
from unittest import mock

from upstream import exitcodes, i18n, updater
from upstream.context import UpdateContext
from upstream.exitcodes import Outcome, classify, classify_winget
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update
from upstream.verify import Verdict

GENERIC = "Installer exited with code {code}"
BUSY_TEXT = "Another installation is running. Try again when it has finished."


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class ClassifyTest(unittest.TestCase):
    def check(self, code, expect, installer="msi"):
        out = classify(code, installer)
        self.assertEqual(out.kind, expect, f"{installer} {code:#x} -> {out}")
        return out

    def test_success_and_restart(self):
        for installer in ("msi", "inno", "nullsoft", "burn", "exe"):
            self.assertTrue(classify(0, installer).ok)
            self.assertEqual(classify(0, installer).kind, "ok")
        for code in (3010, 1641, 0x80070BC2, 0x80070669):   # the HRESULT forms are what Burn returns
            out = self.check(code, "restart", "burn")
            self.assertTrue(out.ok and out.restart)

    def test_busy(self):
        for code in (1618, 0x80070652):
            out = self.check(code, "busy")
            self.assertTrue(out.busy)
            self.assertEqual(out.template, BUSY_TEXT)
            self.assertFalse(out.ok)

    def test_user_cancelled(self):
        for code in (1602, 1223, 0x80070642, 0x800704C7):
            self.assertTrue(self.check(code, "cancelled", "burn").cancelled)
        for code in (2, 5):                                  # Inno: before the install / during it
            self.check(code, "cancelled", "inno")
        self.check(1, "cancelled", "nullsoft")

    def test_framework_codes_do_not_leak_into_other_frameworks(self):
        self.check(5, "needs_admin", "exe")              # ERROR_ACCESS_DENIED, not Inno's cancel
        self.check(1, "failed", "exe")
        self.check(2, "failed", "msi")
        self.check(2, "failed", "nullsoft")              # NSIS 2 is a script abort, not a cancel
        self.check(1, "failed", "inno")                  # Inno 1: setup failed to initialize

    def test_inno_failures_have_reasons(self):
        for code in (1, 3, 4, 7):
            out = self.check(code, "failed", "inno")
            self.assertNotEqual(out.template, GENERIC)
            self.assertEqual(out.values, {"code": code})
        out = self.check(8, "failed", "inno")
        self.assertEqual(out.values, {})

    def test_hresult_form_is_not_unwrapped_for_inno_and_nsis(self):
        out = classify(0x80070005, "inno")
        self.assertEqual((out.kind, out.template, out.values), ("failed", GENERIC, {"code": "0x80070005"}))
        self.assertEqual(classify(0x80070005, "msi").kind, "needs_admin")

    def test_already_installed(self):
        for code in (1638, 0x80070666):
            self.assertEqual(self.check(code, "already_installed", "burn").present, True)

    def test_needs_admin_and_disk_full(self):
        self.check(740, "needs_admin")
        for code in (112, 39, 0x80070070):
            self.check(code, "disk_full")

    def test_well_known_failures_carry_a_readable_reason(self):
        for code in (1601, 1603, 1604, 1619, 1620, 1621, 1622, 1623, 1624, 1625, 1633, 1639, 1640, 1645):
            out = self.check(code, "failed")
            self.assertNotEqual(out.template, GENERIC, code)
            self.assertEqual(out.values, {"code": code})
        self.assertEqual(classify(0x80070643, "burn").values, {"code": "0x80070643"})   # 1603, shown as received

    def test_unknown_code_is_a_plain_failure_with_the_code(self):
        out = classify(12345, "inno")
        self.assertEqual((out.kind, out.template, out.values), ("failed", GENERIC, {"code": 12345}))
        out = classify(0xC0000005, "exe")
        self.assertEqual(out.values, {"code": "0xC0000005"})

    def test_signed_and_unsigned_values_agree(self):
        self.assertEqual(classify(-1073741819, "exe"), classify(0xC0000005, "exe"))
        self.assertEqual(classify(-1, "exe").values, {"code": "0xFFFFFFFF"})
        self.assertEqual(classify(-2147023294, "burn").kind, "cancelled")     # 0x80070642

    def test_outcome_flags(self):
        self.assertTrue(Outcome("ok").ok and not Outcome("ok").restart)
        self.assertTrue(Outcome("newer_present").present)
        self.assertFalse(Outcome("failed").present or Outcome("failed").ok)


class WingetTest(unittest.TestCase):
    def kind(self, code, line=""):
        return classify_winget(code, line).kind

    def test_known_codes(self):
        table = {
            0: "ok",
            0x8A15002B: "already_installed",     # update not applicable
            0x8A15010D: "already_installed",
            0x8A15010E: "newer_present",
            0x8A15010C: "cancelled",
            0x8A150005: "cancelled",
            0x8A150010: "failed",                # no applicable installer
            0x8A150014: "failed",                # package not found
            0x8A150017: "failed",
            0x8A150109: "restart",
            0x8A15010B: "restart",
            0x8A15010A: "failed",                # "restart, then try again": nothing was installed
            0x8A150102: "busy",
            0x8A150105: "disk_full",
            0x8A150019: "needs_admin",
        }
        for code, expect in table.items():
            self.assertEqual(self.kind(code), expect, hex(code))

    def test_restart_counts_as_success(self):
        self.assertTrue(classify_winget(0x8A150109, "").ok)
        self.assertFalse(classify_winget(0x8A15010A, "").ok)

    def test_signed_value(self):
        self.assertEqual(self.kind(-1978335189), "already_installed")

    def test_unknown_code_uses_the_last_line_then_the_code(self):
        out = classify_winget(0x8A150108, "Installer failed with exit code: 1603")
        self.assertEqual((out.kind, out.template, out.values),
                         ("failed", "{error}", {"error": "Installer failed with exit code: 1603"}))
        out = classify_winget(0x8A150108, "")
        self.assertEqual((out.template, out.values), ("winget exited with {code}", {"code": "0x8A150108"}))

    def test_known_code_wins_over_the_last_line(self):
        out = classify_winget(0x8A15002B, "No available upgrade found.")
        self.assertEqual(out.kind, "already_installed")
        self.assertNotEqual(out.template, "{error}")


class TranslationTest(unittest.TestCase):
    def templates(self):
        found = set()
        for table in (exitcodes._WINDOWS, exitcodes._INNO, exitcodes._NSIS, exitcodes._WINGET):
            found.update(template for _, template in table.values())
        return found

    def test_every_message_has_a_persian_text_with_the_same_placeholders(self):
        for template in self.templates():
            self.assertIn(template, i18n.FA, template)
            self.assertEqual(fields(template), fields(i18n.FA[template]), template)

    def test_values_fill_every_placeholder(self):
        for code, entry in {**exitcodes._WINDOWS, **exitcodes._INNO, **exitcodes._NSIS}.items():
            out = exitcodes._outcome(entry, code)
            self.assertEqual(fields(out.template), set(out.values), code)
            i18n.english(out.template, out.values)


def make_status(*fallbacks):
    app = InstalledApp(reg_key=r"HKLM64\Demo", name="Demo", version="1.0", publisher="Demo Inc", scope="machine",
                       arch="x64")
    update = Update("github", "2.0", "o/r", download_url="https://example.invalid/demo.exe", file_name="demo.exe")
    return AppStatus(app, update=update, fallbacks=[Update(s, "2.0", "Demo.Demo") for s in fallbacks])


def verdict(kind, ok=True):
    return Verdict(kind, "2.0", "Installed {version}", {"version": "2.0"}, ok)


class UpdaterOutcomeTest(unittest.TestCase):
    """Updater.run with the two sources replaced by fakes that return a chosen Outcome."""

    def run_updater(self, direct, winget=None, check=None, fallbacks=("winget",)):
        tried = []

        def fake_direct(self_, app, upd, progress, ctx):
            tried.append("direct")
            return updater.Attempt(direct, signer="Demo Inc", sig_status="Valid")

        def fake_winget(self_, app, upd, progress, ctx):
            tried.append("winget")
            return updater.Attempt(winget or Outcome("ok"))

        def fake_check(app, before, outcome, expected, cancel=None):
            return (check or {}).get(outcome.kind, verdict("installed"))

        with mock.patch.object(updater.net, "preflight"), \
             mock.patch.object(updater.verify, "snapshot"), \
             mock.patch.object(updater.verify, "check", fake_check), \
             mock.patch.object(updater.trust, "remember") as remember, \
             mock.patch.object(updater.Updater, "_direct", fake_direct), \
             mock.patch.object(updater.Updater, "_winget", fake_winget):
            result = updater.Updater().run(make_status(*fallbacks), lambda *a, **k: None, UpdateContext())
        return result, tried, remember

    def test_failed_source_falls_back_and_the_last_error_is_kept(self):
        (ok, template, values, _), tried, _ = self.run_updater(
            Outcome("failed", GENERIC, {"code": 1603}), Outcome("needs_admin", "The installer needs administrator permission"))
        self.assertFalse(ok)
        self.assertEqual(tried, ["direct", "winget"])
        self.assertEqual(template, "The installer needs administrator permission")

    def test_already_installed_counts_only_when_the_new_version_is_listed(self):
        present = Outcome("already_installed", "Another version of this app is already installed, so this installer can't continue")
        (ok, template, _, version), tried, _ = self.run_updater(present, check={"already_installed": verdict("installed")})
        self.assertEqual((ok, tried, version), (True, ["direct"], "2.0"))

        (ok, template, _, _), tried, _ = self.run_updater(
            present, Outcome("ok"), check={"already_installed": verdict("unchanged", ok=False)})
        self.assertEqual((ok, tried), (True, ["direct", "winget"]))

        (ok, template, _, _), tried, _ = self.run_updater(
            present, Outcome("failed", GENERIC, {"code": 1}),
            check={"already_installed": verdict("unchanged", ok=False)})
        self.assertFalse(ok)
        self.assertEqual(template, GENERIC)

    def test_already_installed_is_not_a_success_for_an_unverifiable_row(self):
        present = Outcome("newer_present", "winget says a newer version of this app is already installed")
        (ok, template, _, _), tried, _ = self.run_updater(present, check={"newer_present": verdict("unknown")},
                                                          fallbacks=())
        self.assertFalse(ok)
        self.assertEqual(template, present.template)

    def test_unchanged_after_a_clean_exit_keeps_the_update_and_stops(self):
        (ok, _, _, version), tried, remember = self.run_updater(
            Outcome("ok"), check={"ok": verdict("unchanged", ok=False)})
        self.assertFalse(ok)
        self.assertEqual(tried, ["direct"])
        remember.assert_not_called()

    def test_restart_is_a_success_and_remembers_the_publisher(self):
        (ok, _, _, _), tried, remember = self.run_updater(Outcome("restart"), check={"restart": verdict("restart")})
        self.assertTrue(ok)
        remember.assert_called_once()

    def test_an_installer_that_reports_the_app_as_already_there_installed_nothing_to_remember(self):
        present = Outcome("already_installed", "Another version of this app is already installed, so this installer can't continue")
        (ok, _, _, _), _, remember = self.run_updater(present, check={"already_installed": verdict("installed")})
        self.assertTrue(ok)
        remember.assert_not_called()

    def test_a_publisher_that_cannot_be_saved_does_not_fail_the_install(self):
        fake_direct = lambda self, *a: updater.Attempt(Outcome("ok"), signer="Demo Inc", sig_status="Valid")
        with mock.patch.object(updater.net, "preflight"), \
             mock.patch.object(updater.verify, "check", lambda *a, **k: verdict("installed")), \
             mock.patch.object(updater.trust, "remember", side_effect=TypeError("unexpected keyword")), \
             mock.patch.object(updater.Updater, "_direct", fake_direct), \
             self.assertLogs("upstream.update", "ERROR"):
            ok, _, _, version = updater.Updater().run(make_status(), lambda *a, **k: None, UpdateContext())
        self.assertEqual((ok, version), (True, "2.0"))

    def test_side_by_side_is_reported_but_does_not_remember_the_publisher(self):
        (ok, _, _, _), _, remember = self.run_updater(Outcome("ok"), check={"ok": verdict("side_by_side")})
        self.assertTrue(ok)
        remember.assert_not_called()

    def test_busy_and_disk_full_do_not_start_another_source(self):
        for kind, template in (("busy", BUSY_TEXT), ("disk_full", "There isn't enough free disk space to install")):
            (ok, got, _, _), tried, _ = self.run_updater(Outcome(kind, template))
            self.assertEqual((ok, got, tried), (False, template, ["direct"]), kind)

    def test_cancelled_never_starts_another_source(self):
        (ok, template, _, _), tried, _ = self.run_updater(Outcome("cancelled", "The installation was cancelled"))
        self.assertEqual((ok, template, tried), (False, "The installation was cancelled", ["direct"]))


class BusyRetryTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(updater, "BUSY_WAIT_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)
        self.messages = []
        self.progress = lambda template, pct=-1, **v: self.messages.append(template)

    def attempts(self, *outcomes):
        queue = list(outcomes)
        calls = []

        def attempt():
            calls.append(1)
            return queue.pop(0)
        return attempt, calls

    def test_retries_until_the_other_installation_finishes(self):
        attempt, calls = self.attempts(Outcome("busy"), Outcome("busy"), Outcome("ok"))
        out = updater.Updater._while_busy(attempt, self.progress, UpdateContext())
        self.assertTrue(out.ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.messages, ["Another installation is running, waiting…"] * 2)

    def test_gives_up_after_a_few_tries(self):
        attempt, calls = self.attempts(*[Outcome("busy")] * (updater.BUSY_RETRIES + 5))
        out = updater.Updater._while_busy(attempt, self.progress, UpdateContext())
        self.assertTrue(out.busy)
        self.assertEqual(len(calls), updater.BUSY_RETRIES + 1)

    def test_other_outcomes_are_not_retried(self):
        attempt, calls = self.attempts(Outcome("failed", GENERIC, {"code": 1}), Outcome("ok"))
        out = updater.Updater._while_busy(attempt, self.progress, UpdateContext())
        self.assertEqual((out.kind, len(calls)), ("failed", 1))
        self.assertEqual(self.messages, [])

    def test_cancel_ends_the_wait(self):
        ctx = UpdateContext()
        calls = []

        def attempt():
            calls.append(1)
            ctx.cancel.cancel()
            return Outcome("busy")

        with mock.patch.object(updater, "BUSY_WAIT_SECONDS", 60):   # would hang if the token were ignored
            with self.assertRaises(updater.installer.Cancelled):
                updater.Updater._while_busy(attempt, self.progress, ctx)
        self.assertEqual(len(calls), 1)

    def test_direct_install_runs_the_same_installer_again(self):
        codes = [1618, 1618, 0]
        installs = []

        def install(path, kind, scope, plan, cancel):
            installs.append(path)
            return codes.pop(0)

        status = make_status()
        plan = mock.Mock(warnings=[], refuse=None, extra_args="")
        patches = [
            mock.patch.object(updater.net, "preflight"),
            mock.patch.object(updater.net, "download", lambda *a, **k: Path("demo.exe")),
            mock.patch.object(updater.installer, "signature", lambda p: ("Valid", "Demo Inc")),
            mock.patch.object(updater.installer, "detect", lambda p, h=None: "msi"),
            mock.patch.object(updater.installer, "install", install),
            mock.patch.object(updater.trust, "evaluate", lambda *a: mock.Mock(action="allow")),
            mock.patch.object(updater.inplace, "plan", lambda *a: plan),
            mock.patch.object(updater.running, "close_for_update"),
            mock.patch.object(updater.verify, "snapshot"),
            mock.patch.object(updater.verify, "check", lambda *a, **k: verdict("installed")),
            mock.patch.object(updater.trust, "remember"),
            mock.patch.object(Path, "unlink"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ok, _, _, version = updater.Updater().run(status, self.progress, UpdateContext())
        self.assertEqual((ok, version, len(installs)), (True, "2.0", 3))
        self.assertEqual(self.messages.count("Another installation is running, waiting…"), 2)


if __name__ == "__main__":
    unittest.main()
