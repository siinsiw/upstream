"""The last-scan snapshot (instant start) and the scanner hooks around it: round trip, corruption, apps that are gone,
and the badge calls. The snapshot file is always a temp file: DATA_DIR is never touched."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from upstream import badges, scanner
from upstream.inventory import still_listed as real_still_listed
from upstream.scanner import AppStatus, Scanner, Update, load_snapshot, save_snapshot

from .test_pins import FakeCache, FakeSettings, api_release, github_with, make_app


class TempSnapshot(unittest.TestCase):
    """SNAPSHOT_FILE points into a temp folder for the whole test, and every app counts as still installed unless
    a test says otherwise."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.file = self.dir / "sub" / "last_scan.json"
        for patch in (mock.patch.object(scanner, "SNAPSHOT_FILE", self.file),
                      mock.patch.object(scanner, "still_listed", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, payload):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(payload if isinstance(payload, str) else json.dumps(payload), "utf-8")

    def doc(self):
        return json.loads(self.file.read_text("utf-8"))


def sample_statuses() -> list[AppStatus]:
    github = Update("github", "4.0.0", "o/demo", download_url="https://github.com/o/demo/releases/download/v4/D.exe",
                    file_name="D.exe", sha256="ab" * 32, page_url="https://github.com/o/demo/releases/tag/v4",
                    notes="Fixes CVE-2026-0001\n日本語 café", installer_type="inno")
    winget = Update("winget", "4.0.0", "Demo.App", page_url="https://demo.example")
    held = AppStatus(
        make_app("Held", "1.0", urls=["https://held.example"], install_location=r"C:\Held", product_code="{A-B}",
                 is_msi=True, install_kind="msi", size_kb=2048, install_date="20260101"),
        repo="o/held", winget_id="Held.App", winget_available="2.0")
    held.set_note("Held at {version}", version="1.0")
    held.pin = {"mode": "hold", "version": "1.0"}
    plain = AppStatus(make_app("Demo", "3.0", "Demo Inc ✓"), repo="o/demo", winget_id="Demo.App",
                      winget_available="4.0.0", winget_forced=True, update=github, fallbacks=[winget])
    plain.badges = [badges.Badge(kind="security", text="Security fix", tooltip="CVE-2026-0001")]
    store = AppStatus(make_app("Terminal", "1.22", "Microsoft", reg_key=r"WINGET\Microsoft.WindowsTerminal"),
                      winget_id="Microsoft.WindowsTerminal", winget_available="1.23")
    store.update = Update("winget", "1.23", "Microsoft.WindowsTerminal")
    nothing = AppStatus(make_app("Quiet", "9"))
    return [plain, held, store, nothing]


class RoundTripTest(TempSnapshot):
    def test_everything_a_row_needs_comes_back(self):
        statuses = sample_statuses()
        self.assertTrue(save_snapshot(statuses, taken=1_800_000_000.5))
        loaded = load_snapshot()
        self.assertEqual(loaded, (statuses, 1_800_000_000.5))
        plain = loaded[0][0]
        self.assertEqual(plain.fallbacks[0].source, "winget")
        self.assertEqual(plain.uid, statuses[0].uid)
        self.assertEqual(plain.update.notes, "Fixes CVE-2026-0001\n日本語 café")
        self.assertEqual((loaded[0][1].note, loaded[0][1].note_args), ("Held at {version}", {"version": "1.0"}))

    def test_the_time_defaults_to_now(self):
        before = time.time()
        save_snapshot(sample_statuses())
        taken = load_snapshot()[1]
        self.assertTrue(before <= taken <= time.time())

    def test_an_empty_scan_is_a_valid_snapshot(self):
        save_snapshot([], taken=5.0)
        self.assertEqual(load_snapshot(), ([], 5.0))

    def test_the_file_holds_only_the_scan(self):
        save_snapshot(sample_statuses(), taken=7.0)
        document = self.doc()
        self.assertEqual(set(document), {"schema", "time", "statuses"})
        self.assertEqual(document["schema"], scanner.SNAPSHOT_SCHEMA)
        self.assertEqual(set(document["statuses"][0]), {"app", "repo", "winget_id", "winget_available", "winget_forced",
                                                       "note", "note_args", "update", "fallbacks", "badges", "pin"})
        self.assertNotIn("token", self.file.read_text("utf-8").lower())
        self.assertIn("日本語", self.file.read_text("utf-8"), "kept as text, not \\u escapes")

    def test_a_big_scan_stays_small(self):
        statuses = [AppStatus(make_app(f"Application number {i}", "1.2.3", "Some Publisher Ltd",
                                       reg_key=rf"HKLM64\{{0000{i:04d}-AAAA-BBBB-CCCC-DDDDEEEEFFFF}}",
                                       install_location=rf"C:\Program Files\Application number {i}",
                                       uninstall_string=rf'"C:\Program Files\Application number {i}\unins000.exe"',
                                       urls=["https://example.com/app"]),
                              repo="o/r", winget_id=f"Pub.App{i}") for i in range(300)]
        save_snapshot(statuses)
        self.assertLess(self.file.stat().st_size, 300 * 900)
        self.assertEqual(len(load_snapshot()[0]), 300)

    def test_saving_replaces_the_old_file_and_leaves_no_temp_file(self):
        save_snapshot(sample_statuses(), taken=1.0)
        save_snapshot([], taken=2.0)
        self.assertEqual(load_snapshot(), ([], 2.0))
        self.assertEqual([p.name for p in self.file.parent.iterdir()], ["last_scan.json"])

    def test_a_failed_replace_keeps_the_old_snapshot_and_cleans_up(self):
        save_snapshot(sample_statuses(), taken=1.0)
        before = self.file.read_bytes()
        with mock.patch.object(scanner.os, "replace", side_effect=OSError("locked")), \
                self.assertLogs("upstream.scan", "WARNING"):
            self.assertFalse(save_snapshot([], taken=2.0))
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual([p.name for p in self.file.parent.iterdir()], ["last_scan.json"])

    def test_a_folder_that_cannot_be_made_is_not_an_error(self):
        blocker = self.dir / "blocker"
        blocker.write_text("a file where the folder should be")
        with mock.patch.object(scanner, "SNAPSHOT_FILE", blocker / "last_scan.json"), \
                self.assertLogs("upstream.scan", "WARNING"):
            self.assertFalse(save_snapshot(sample_statuses()))

    def test_a_value_json_cannot_hold_does_not_stop_the_save(self):
        status = AppStatus(make_app(), note_args={"when": Path("x")})
        self.assertTrue(save_snapshot([status]))
        self.assertEqual(load_snapshot()[0][0].note_args, {"when": "x"})


class GoneAppsTest(TempSnapshot):
    def test_apps_windows_no_longer_lists_are_dropped(self):
        statuses = sample_statuses()
        save_snapshot(statuses)
        gone = {statuses[0].app.reg_key}
        with mock.patch.object(scanner, "still_listed", side_effect=lambda app: app.reg_key not in gone):
            loaded = load_snapshot()[0]
        self.assertEqual([s.app.name for s in loaded], ["Held", "Terminal", "Quiet"])

    def test_store_rows_stay_because_only_winget_can_say(self):
        statuses = sample_statuses()
        save_snapshot(statuses)
        with mock.patch.object(scanner, "still_listed", return_value=False):
            loaded = load_snapshot()[0]
        self.assertEqual([s.app.name for s in loaded], ["Terminal"])

    def test_a_registry_that_cannot_be_asked_keeps_the_row(self):
        save_snapshot(sample_statuses())
        with mock.patch.object(scanner, "still_listed", side_effect=OSError("denied")), \
                self.assertLogs("upstream.scan", "WARNING"):
            self.assertEqual(len(load_snapshot()[0]), 4)

    def test_the_real_check_drops_an_app_with_no_uninstall_key(self):
        app = make_app("Ghost", reg_key=r"HKCU\UpstreamTest_no_such_key_3f9a1c")
        save_snapshot([AppStatus(app)])
        with mock.patch.object(scanner, "still_listed", real_still_listed):
            self.assertEqual(load_snapshot()[0], [])


class CorruptionTest(TempSnapshot):
    def test_no_snapshot_is_none(self):
        self.assertIsNone(load_snapshot())

    def test_unreadable_files_are_none(self):
        good = json.dumps({"schema": 1, "time": 5.0, "statuses": []})
        for label, content in [
            ("empty", ""), ("garbage", "not json at all"), ("truncated", good[:-6]), ("a list", "[]"), ("a string", '"x"'),
            ("null", "null"), ("another schema", good.replace('"schema": 1', '"schema": 2')),
            ("no schema", '{"time": 5, "statuses": []}'),
            ("no time", '{"schema": 1, "statuses": []}'), ("time is text", '{"schema": 1, "time": "5", "statuses": []}'),
            ("time is a flag", '{"schema": 1, "time": true, "statuses": []}'),
            ("time is NaN", '{"schema": 1, "time": NaN, "statuses": []}'),
            ("time is zero", '{"schema": 1, "time": 0, "statuses": []}'),
            ("statuses is a dict", '{"schema": 1, "time": 5, "statuses": {}}'),
            ("no statuses", '{"schema": 1, "time": 5}'),
            ("deeply nested", "[" * 100_000),
        ]:
            with self.subTest(label):
                self.write(content)
                self.assertIsNone(load_snapshot())

    def test_a_file_that_is_not_utf8_is_none(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'{"schema": 1, "time": 5, "statuses": [\xff\xfe]}')
        self.assertIsNone(load_snapshot())

    def test_a_folder_in_place_of_the_file_is_none(self):
        self.file.mkdir(parents=True)
        self.assertIsNone(load_snapshot())

    def entry(self, **changes):
        save_snapshot([sample_statuses()[0]])
        entry = self.doc()["statuses"][0]
        for key, value in changes.items():
            if value is ...:
                entry.pop(key, None)
            else:
                entry[key] = value
        return entry

    def load_entries(self, *entries):
        self.write({"schema": scanner.SNAPSHOT_SCHEMA, "time": 9.0, "statuses": list(entries)})
        return load_snapshot()[0]

    def test_a_bad_entry_is_skipped_and_the_good_ones_stay(self):
        good = self.entry()
        app = good["app"]
        bad = [
            "text", 5, None, [], {}, {"app": "x"}, {"app": {}},
            self.entry(app=...),
            self.entry(app={**app, "name": 5}),
            self.entry(app={**app, "reg_key": None}),
            self.entry(app={**app, "is_msi": "yes"}),
            self.entry(app={**app, "size_kb": True}),
            self.entry(app={**app, "urls": "https://x"}),
            self.entry(app={**app, "urls": [1]}),
            self.entry(update={"source": "github"}),
            self.entry(update="v4"),
            self.entry(update={**good["update"], "version": 4}),
            self.entry(update={**good["update"], "sha256": 5}),
            self.entry(fallbacks="x"),
            self.entry(fallbacks=[{"source": "winget"}]),
            self.entry(fallbacks=[None]),
            self.entry(repo=5),
            self.entry(winget_forced="no"),
            self.entry(note=None),
            self.entry(note_args=[]),
        ]
        loaded = self.load_entries(*bad[:10], good, *bad[10:])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].update.version, "4.0.0")

    def test_duplicate_rows_collapse(self):
        good = self.entry()
        self.assertEqual(len(self.load_entries(good, good)), 1)

    def test_a_file_from_an_older_version_without_the_new_fields_still_loads(self):
        old = self.entry(app={"reg_key": r"HKLM64\Old", "name": "Old", "version": "1", "publisher": "P",
                              "scope": "machine", "arch": "x64"}, update=None, fallbacks=[], badges=..., pin=...,
                         note=..., note_args=..., winget_forced=...)
        (status,) = self.load_entries(old)
        self.assertEqual((status.app.name, status.app.size_kb, status.update, status.badges, status.pin),
                         ("Old", 0, None, [], None))

    def test_unknown_keys_from_a_newer_version_are_ignored(self):
        entry = self.entry(future="x")
        entry["app"]["future_field"] = 1
        entry["update"]["future_field"] = 1
        (status,) = self.load_entries(entry)
        self.assertEqual(status.update.version, "4.0.0")

    def test_a_bad_badge_or_pin_costs_only_itself(self):
        entry = self.entry(badges=[{"kind": "security", "text": "Fix"}, {"kind": 1, "text": "x"}, "x", {}],
                           pin={"mode": "freeze", "version": "1"})
        (status,) = self.load_entries(entry)
        self.assertEqual([b.text for b in status.badges], ["Fix"])
        self.assertIsNone(status.pin)
        self.assertEqual(status.update.version, "4.0.0")

    def test_pin_and_badges_that_are_not_what_they_should_be_are_ignored(self):
        (status,) = self.load_entries(self.entry(badges="x", pin=[1]))
        self.assertEqual((status.badges, status.pin), ([], None))


def resolved_scanner(settings=None, releases=None):
    sc = Scanner(settings or FakeSettings(overrides={"demo": {"github": "o/demo"}}), FakeCache())
    sc._gh = github_with({"o/demo": releases if releases is not None else [api_release("v4.0.0"), api_release("v3.0.0")]})
    return sc


class ScanTest(TempSnapshot):
    """Scanner.scan() with the registry, winget and GitHub replaced by canned answers."""

    def setUp(self):
        super().setUp()
        self.apps = [make_app("Demo", "3.0.0"), make_app("Other", "1.0", reg_key=r"HKLM64\Other")]
        for patch in (mock.patch.object(scanner, "read_installed_apps", side_effect=lambda hidden: list(self.apps)),
                      mock.patch.object(scanner.winget, "available", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.progress = lambda *a: None

    def test_a_finished_scan_leaves_a_snapshot_that_loads_back_identical(self):
        sc = resolved_scanner()
        statuses = sc.scan(self.progress)
        self.assertEqual({s.app.name: s.update and s.update.version for s in statuses}, {"Demo": "4.0.0", "Other": None})
        loaded, taken = load_snapshot()
        self.assertEqual(loaded, statuses)
        self.assertLess(abs(taken - time.time()), 60)

    def test_ignored_apps_are_in_the_snapshot_too(self):
        settings = FakeSettings(overrides={"demo": {"github": "o/demo"}})
        settings.is_ignored = lambda ident: ident == "other"
        sc = resolved_scanner(settings)
        sc.scan(self.progress)
        self.assertEqual([s.app.name for s in load_snapshot()[0]], ["Demo", "Other"])

    def test_a_snapshot_that_cannot_be_saved_does_not_fail_the_scan(self):
        sc = resolved_scanner()
        with mock.patch.object(scanner.os, "replace", side_effect=OSError("locked")), \
                self.assertLogs("upstream.scan", "WARNING"):
            statuses = sc.scan(self.progress)
        self.assertEqual(len(statuses), 2)
        self.assertFalse(self.file.exists())

    def test_the_scan_does_not_write_the_real_snapshot(self):
        real = scanner.DATA_DIR / "last_scan.json"
        existed = real.exists() and real.stat().st_mtime_ns
        resolved_scanner().scan(self.progress)
        self.assertEqual(real.exists() and real.stat().st_mtime_ns, existed)


class BadgeHookTest(TempSnapshot):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.badge = badges.Badge(kind="security", text="Security fix", tooltip="CVE-2026-0001")
        for patch in (mock.patch.object(scanner, "read_installed_apps", side_effect=lambda hidden: [make_app("Demo", "3.0.0")]),
                      mock.patch.object(scanner.winget, "available", return_value=False),
                      mock.patch.object(scanner.badges, "refresh_eol", side_effect=self.refresh),
                      mock.patch.object(scanner.badges, "compute", side_effect=self.compute)):
            patch.start()
            self.addCleanup(patch.stop)

    def refresh(self, cache, statuses):
        self.calls.append(("refresh", [s.app.name for s in statuses]))

    def compute(self, status, cache=None, *rest):
        self.calls.append(("compute", status.app.name, status.update and status.update.version))
        return [self.badge]

    def scanner(self, **values):
        return resolved_scanner(FakeSettings(values, overrides={"demo": {"github": "o/demo"}}))

    def test_eol_data_is_refreshed_once_before_any_app_is_resolved(self):
        statuses = self.scanner().scan(lambda *a: None)
        self.assertEqual(self.calls, [("refresh", ["Demo"]), ("compute", "Demo", "4.0.0")])
        self.assertEqual(statuses[0].badges, [self.badge])

    def test_badges_are_saved_in_the_snapshot(self):
        self.scanner().scan(lambda *a: None)
        self.assertEqual(load_snapshot()[0][0].badges, [self.badge])

    def test_badges_off_means_no_calls_and_no_badges(self):
        statuses = self.scanner(security_badges=False).scan(lambda *a: None)
        self.assertEqual((self.calls, statuses[0].badges), ([], []))

    def test_a_badge_error_never_breaks_the_scan(self):
        with mock.patch.object(scanner.badges, "compute", side_effect=RuntimeError("boom")), \
                self.assertLogs("upstream.scan", "ERROR"):
            statuses = self.scanner().scan(lambda *a: None)
        self.assertEqual((statuses[0].update.version, statuses[0].badges), ("4.0.0", []))

    def test_an_end_of_life_refresh_error_never_breaks_the_scan(self):
        with mock.patch.object(scanner.badges, "refresh_eol", side_effect=RuntimeError("boom")), \
                self.assertLogs("upstream.scan", "ERROR"):
            statuses = self.scanner().scan(lambda *a: None)
        self.assertEqual(statuses[0].badges, [self.badge])

    def test_badges_see_the_update_the_pin_left(self):
        sc = self.scanner(pins={"demo": {"mode": "hold", "version": "3.0.0"}})
        (status,) = sc.scan(lambda *a: None)
        self.assertEqual(self.calls[-1], ("compute", "Demo", None))
        self.assertEqual(status.pin["mode"], "hold")

    def test_recheck_refreshes_and_recomputes(self):
        sc = self.scanner()
        status = AppStatus(make_app("Demo", "3.0.0"), badges=[])
        sc.recheck(status)
        self.assertEqual(self.calls, [("refresh", ["Demo"]), ("compute", "Demo", "4.0.0")])
        self.assertEqual(status.badges, [self.badge])

    def test_recheck_with_badges_switched_off_clears_them(self):
        sc = self.scanner(security_badges=False)
        status = AppStatus(make_app("Demo", "3.0.0"), badges=[self.badge])
        sc.recheck(status)
        self.assertEqual((self.calls, status.badges), ([], []))


if __name__ == "__main__":
    unittest.main()
