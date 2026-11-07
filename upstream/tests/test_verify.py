"""Install verification: what verify.snapshot / verify.check conclude from fake registry listings."""
from __future__ import annotations

import string
import time
import unittest
from unittest import mock

from upstream import i18n, verify
from upstream.context import CancelToken
from upstream.exitcodes import Outcome
from upstream.inventory import InstalledApp
from upstream.verify import Snapshot


def entry(key: str, version: str, name: str = "Demo", scope: str = "machine", arch: str = "x64") -> InstalledApp:
    return InstalledApp(reg_key=key, name=name, version=version, publisher="Demo Inc", scope=scope, arch=arch)


OLD = entry(r"HKLM64\{OLD}", "1.0")
OK = Outcome("ok")


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class FakePC:
    """Replaces the registry reads. `listings` are served one per read (the last one repeats)."""

    def __init__(self, *listings, flags=None):
        self.listings = list(listings)
        self.flags = flags or {}
        self.reads = 0

    def read(self):
        index = min(self.reads, len(self.listings) - 1)
        self.reads += 1
        return list(self.listings[index])

    def __enter__(self):
        self.patches = [mock.patch.object(verify, "read_installed_apps", self.read),
                        mock.patch.object(verify, "_pending_flags", lambda: dict(self.flags)),
                        mock.patch.object(verify, "SETTLE_SECONDS", 0)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def judge(after, outcome=OK, before=None, app=OLD, expected="2.0", flags=None):
    before = before if before is not None else Snapshot([app])
    with FakePC(after, flags=flags):
        return verify.check(app, before, outcome, expected)


class SnapshotTest(unittest.TestCase):
    def test_keeps_only_the_apps_own_family(self):
        other = entry(r"HKLM64\{OTHER}", "9.0", name="Something Else")
        twin = entry(r"HKCU\Demo", "1.0", scope="user")
        with FakePC([other, OLD, twin], flags={"servicing": 1}):
            snap = verify.snapshot(OLD)
        self.assertEqual([e.reg_key for e in snap.entries], [OLD.reg_key, twin.reg_key])
        self.assertTrue(snap.pending_reboot)
        self.assertEqual(snap.pending, {"servicing": 1})

    def test_the_app_itself_is_always_recorded(self):
        with FakePC([]):
            snap = verify.snapshot(OLD)
        self.assertEqual(snap.entries, [OLD])
        self.assertFalse(snap.pending_reboot)

    def test_store_rows_have_no_registry_entry_to_read(self):
        store = entry("WINGET\\Demo.Demo", "1.0")
        with mock.patch.object(verify, "read_installed_apps", side_effect=AssertionError("read")), \
             mock.patch.object(verify, "_pending_flags", lambda: {}):
            self.assertEqual(verify.snapshot(store).entries, [])

    def test_never_raises(self):
        with mock.patch.object(verify, "read_installed_apps", side_effect=OSError("denied")), \
             mock.patch.object(verify, "_pending_flags", lambda: {}), self.assertLogs("upstream.verify", "ERROR"):
            self.assertEqual(verify.snapshot(OLD).entries, [OLD])

    def test_pending_flags_can_be_read(self):
        flags = verify._pending_flags()
        self.assertTrue(all(isinstance(k, str) and isinstance(v, int) and v > 0 for k, v in flags.items()))


class CheckTest(unittest.TestCase):
    def test_updated_in_place(self):
        v = judge([entry(OLD.reg_key, "2.0")])
        self.assertEqual((v.kind, v.version, v.ok, v.template, v.values),
                         ("installed", "2.0", True, "Installed {version}", {"version": "2.0"}))

    def test_upgrade_that_registers_a_new_key(self):
        v = judge([entry(r"HKLM64\{NEW}", "2.0")])
        self.assertEqual((v.kind, v.version, v.ok), ("installed", "2.0", True))

    def test_reports_the_version_windows_lists_not_the_expected_one(self):
        v = judge([entry(OLD.reg_key, "2.0.1")], expected="2.0")
        self.assertEqual(v.version, "2.0.1")

    def test_second_copy_next_to_the_old_one(self):
        v = judge([OLD, entry(r"HKLM64\{NEW}", "2.0")])
        self.assertEqual((v.kind, v.version, v.ok), ("side_by_side", "2.0", True))
        self.assertEqual(v.values, {"version": "2.0", "name": "Demo"})

    def test_per_user_copy_next_to_a_machine_copy_is_side_by_side(self):
        v = judge([OLD, entry(r"HKCU\Demo", "2.0", scope="user")])
        self.assertEqual(v.kind, "side_by_side")

    def test_a_second_entry_that_is_not_newer_changes_nothing(self):
        v = judge([OLD, entry(r"HKLM64\{NEW}", "1.0")])
        self.assertEqual((v.kind, v.ok), ("unchanged", False))

    def test_old_version_still_listed(self):
        v = judge([OLD])
        self.assertEqual((v.kind, v.version, v.ok), ("unchanged", "1.0", False))
        self.assertEqual(v.template, "Installer finished, but Windows still reports {version}")
        self.assertEqual(v.values, {"version": "1.0"})

    def test_version_spelled_differently_is_still_unchanged(self):
        old = entry(OLD.reg_key, "1.2.3")
        v = judge([entry(OLD.reg_key, "1.2.3.0")], app=old, before=Snapshot([old]), expected="1.3.0")
        self.assertEqual(v.kind, "unchanged")

    def test_entries_that_were_already_there_are_not_a_second_copy(self):
        twin = entry(r"HKLM32\{TWIN}", "3.0", arch="x86")
        before = Snapshot([OLD, twin])
        self.assertEqual(judge([OLD, twin], before=before).kind, "unchanged")
        self.assertEqual(judge([entry(OLD.reg_key, "2.0"), twin], before=before).kind, "installed")

    def test_other_apps_are_ignored(self):
        other = entry(r"HKLM64\{OTHER}", "5.0", name="Something Else")
        self.assertEqual(judge([OLD, other]).kind, "unchanged")

    def test_a_missing_baseline_still_works(self):
        v = judge([entry(OLD.reg_key, "2.0")], before=Snapshot())
        self.assertEqual(v.kind, "installed")

    def test_numbering_scheme_change_counts_when_the_text_changed(self):
        v = judge([entry(OLD.reg_key, "2024.03.1")], expected="2024.03.1")
        self.assertEqual((v.kind, v.version), ("installed", "2024.03.1"))

    def test_registry_catches_up_a_moment_after_the_installer_returns(self):
        fresh = [entry(OLD.reg_key, "2.0")]
        ticks = iter(range(100))
        fake_time = mock.Mock(monotonic=lambda: next(ticks), sleep=lambda s: None)
        with FakePC([OLD], fresh) as pc, mock.patch.object(verify, "SETTLE_SECONDS", 5), \
             mock.patch.object(verify, "time", fake_time):
            v = verify.check(OLD, Snapshot([OLD]), OK, "2.0")
        self.assertEqual(v.kind, "installed")
        self.assertEqual(pc.reads, 2)

    def test_settling_uses_the_cancel_token_instead_of_sleeping(self):
        ticks = iter(range(100))
        fake_time = mock.Mock(monotonic=lambda: next(ticks), sleep=mock.Mock())
        cancel = mock.Mock(wait=mock.Mock(return_value=False))
        with FakePC([OLD], [entry(OLD.reg_key, "2.0")]) as pc, mock.patch.object(verify, "SETTLE_SECONDS", 5), \
             mock.patch.object(verify, "time", fake_time):
            v = verify.check(OLD, Snapshot([OLD]), OK, "2.0", cancel=cancel)
        self.assertEqual((v.kind, pc.reads), ("installed", 2))
        cancel.wait.assert_called_once_with(1)
        fake_time.sleep.assert_not_called()

    def test_cancelling_ends_the_settle_wait_at_once(self):
        ticks = iter(range(100))
        fake_time = mock.Mock(monotonic=lambda: next(ticks), sleep=lambda s: None)
        cancel = mock.Mock(wait=mock.Mock(side_effect=[False, True]))
        with FakePC([OLD]) as pc, mock.patch.object(verify, "SETTLE_SECONDS", 5), mock.patch.object(verify, "time", fake_time):
            v = verify.check(OLD, Snapshot([OLD]), OK, "2.0", cancel=cancel)
        self.assertEqual((v.kind, v.ok, pc.reads), ("unchanged", False, 2))     # the first look, one more, then it stops
        self.assertEqual(cancel.wait.call_count, 2)

    def test_a_token_that_is_already_cancelled_costs_no_time_at_all(self):
        token = CancelToken()
        token.cancel()
        with FakePC([OLD]) as pc, mock.patch.object(verify, "SETTLE_SECONDS", 30):
            started = time.monotonic()
            v = verify.check(OLD, Snapshot([OLD]), OK, "2.0", cancel=token)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual((v.kind, pc.reads), ("unchanged", 1))

    def test_an_installer_that_only_said_the_app_is_there_is_not_waited_for(self):
        for kind in ("already_installed", "newer_present"):
            ticks = iter(range(100))
            fake_time = mock.Mock(monotonic=lambda: next(ticks), sleep=mock.Mock())
            with FakePC([OLD], [entry(OLD.reg_key, "2.0")]) as pc, mock.patch.object(verify, "SETTLE_SECONDS", 5), \
                 mock.patch.object(verify, "time", fake_time):
                v = verify.check(OLD, Snapshot([OLD]), Outcome(kind), "2.0")
            self.assertEqual((v.kind, pc.reads), ("unchanged", 1), kind)
            fake_time.sleep.assert_not_called()

    def test_unreadable_registry_trusts_the_expected_version(self):
        with mock.patch.object(verify, "read_installed_apps", side_effect=OSError("denied")), \
             mock.patch.object(verify, "_pending_flags", lambda: {}), mock.patch.object(verify, "SETTLE_SECONDS", 0), \
             self.assertLogs("upstream.verify", "ERROR"):
            v = verify.check(OLD, Snapshot([OLD]), OK, "2.0")
        self.assertEqual((v.kind, v.version, v.ok), ("unknown", "2.0", True))


class RestartTest(unittest.TestCase):
    def test_exit_code_asks_for_a_restart(self):
        v = judge([entry(OLD.reg_key, "2.0")], Outcome("restart"))
        self.assertEqual((v.kind, v.version, v.ok), ("restart", "2.0", True))

    def test_files_replaced_at_restart_keep_the_old_version_listed_for_now(self):
        v = judge([OLD], Outcome("restart"))
        self.assertEqual((v.kind, v.version, v.ok), ("restart", "2.0", True))

    def test_new_pending_flag(self):
        v = judge([entry(OLD.reg_key, "2.0")], flags={"file operations": 3})
        self.assertEqual(v.kind, "restart")

    def test_growing_pending_list(self):
        before = Snapshot([OLD], True, {"file operations": 3})
        self.assertEqual(judge([entry(OLD.reg_key, "2.0")], before=before, flags={"file operations": 5}).kind, "restart")

    def test_flag_that_was_already_set_is_not_ours(self):
        before = Snapshot([OLD], True, {"servicing": 1, "file operations": 3})
        v = judge([entry(OLD.reg_key, "2.0")], before=before, flags={"servicing": 1, "file operations": 3})
        self.assertEqual(v.kind, "installed")

    def test_side_by_side_is_reported_before_the_restart(self):
        v = judge([OLD, entry(r"HKLM64\{NEW}", "2.0")], Outcome("restart"))
        self.assertEqual(v.kind, "side_by_side")

    def test_an_unrelated_new_flag_does_not_turn_a_failed_install_into_a_restart(self):
        # Exit 0, the registry still lists the old version, and Windows Update asked for a restart meanwhile.
        for flags in ({"windows update": 1}, {"file operations": 4}):
            v = judge([OLD], flags=flags)
            self.assertEqual((v.kind, v.version, v.ok), ("unchanged", "1.0", False), flags)

    def test_a_new_flag_does_not_rescue_an_installer_that_only_said_the_app_is_already_there(self):
        for kind in ("already_installed", "newer_present"):
            self.assertEqual(judge([OLD], Outcome(kind), flags={"servicing": 1}).kind, "unchanged", kind)
            v = judge([entry(OLD.reg_key, "2.0")], Outcome(kind), flags={"servicing": 1})
            self.assertEqual((v.kind, v.ok), ("installed", True), kind)

    def test_the_exit_code_alone_is_enough_for_a_restart_even_when_the_registry_is_unchanged(self):
        v = judge([OLD], Outcome("restart"), flags={})
        self.assertEqual((v.kind, v.ok), ("restart", True))


class UnknownTest(unittest.TestCase):
    def test_store_or_winget_only_rows_trust_the_expected_version(self):
        store = entry("WINGET\\Demo.Demo", "1.0")
        with mock.patch.object(verify, "read_installed_apps", side_effect=AssertionError("read")), \
             mock.patch.object(verify, "_pending_flags", lambda: {}):
            v = verify.check(store, Snapshot(), OK, "2.0")
        self.assertEqual((v.kind, v.version, v.ok), ("unknown", "2.0", True))
        self.assertEqual(v.values, {"version": "2.0"})

    def test_app_no_longer_listed(self):
        v = judge([])
        self.assertEqual((v.kind, v.version, v.ok), ("unknown", "2.0", True))

    def test_missing_expected_version_falls_back_to_the_old_one_not_an_empty_string(self):
        v = judge([], expected="")
        self.assertEqual(v.version, "1.0")


class OrderTest(unittest.TestCase):
    def test_moved_on(self):
        cases = [
            ("2.0", "1.0", True), ("1.0", "2.0", False), ("1.2.3.0", "1.2.3", False),
            ("1.2.3", "1.2.3-beta.1", True), ("1.1.1k", "1.1.1", True), ("1.2.3a", "1.2.3", False),
            ("2024.03.1", "1.0", True),            # another numbering scheme: the text changed
            ("2.0", "", True), ("", "2.0", False),
            ("latest", "2.0", False),              # no number to go by
            ("build B", "build A", True), ("build A", "build A", False),
        ]
        for new, old, expect in cases:
            self.assertEqual(verify._moved_on(new, old), expect, f"{new!r} vs {old!r}")


class TranslationTest(unittest.TestCase):
    def test_every_message_has_a_persian_text_with_the_same_placeholders(self):
        new = entry(r"HKLM64\{NEW}", "2.0")
        verdicts = [judge([entry(OLD.reg_key, "2.0")]), judge([OLD, new]), judge([OLD]), judge([]),
                    judge([OLD], Outcome("restart"))]
        self.assertEqual({v.kind for v in verdicts}, {"installed", "side_by_side", "unchanged", "unknown", "restart"})
        for v in verdicts:
            self.assertIn(v.template, i18n.FA, v.template)
            self.assertEqual(fields(v.template), fields(i18n.FA[v.template]), v.template)
            self.assertEqual(fields(v.template), set(v.values), v.template)


if __name__ == "__main__":
    unittest.main()
