"""Version pins (hold, stay on a major line) and rollback. Offline: GitHub and winget answers are canned,
no settings file or winget command is touched."""
from __future__ import annotations

import copy
import string
import sys
import time
import unittest
from unittest import mock

from upstream import pins, scanner, winget
from upstream.github import GitHub
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Scanner, Update
from upstream.translations import pins as pins_fa


def make_app(name="Demo", version="3.12.0", publisher="Demo Inc", reg_key=None, **extra):
    return InstalledApp(reg_key=reg_key or rf"HKLM64\{name}", name=name, version=version, publisher=publisher,
                        scope="machine", arch="x64", **extra)


class FakeSettings:
    """settings.json in memory: get/set/save like config.Settings, plus the calls the scanner makes."""

    def __init__(self, values=None, overrides=None):
        self.values = copy.deepcopy(values or {})
        self.overrides = overrides or {}
        self.saved = 0

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        self.saved += 1

    def override(self, ident):
        return copy.deepcopy(self.overrides.get(ident, {}))

    def is_ignored(self, ident):
        return False


class FakeCache:
    def __init__(self):
        self.store = {}

    def entry(self, section, key):
        value = self.store.get((section, key))
        return copy.deepcopy(value) if value is not None else None

    def put(self, section, key, value):
        self.store[(section, key)] = dict(value, ts=time.time())

    def save(self):
        pass


def api_release(tag, files=("Demo-Setup-x64.exe",)):
    return {"tag_name": tag, "name": tag, "html_url": f"https://github.com/o/demo/releases/tag/{tag}",
            "prerelease": False, "draft": False, "body": f"Notes for {tag}",
            "assets": [{"name": f, "browser_download_url": f"https://github.com/o/demo/releases/download/{tag}/{f}",
                        "size": 1000, "digest": None} for f in files]}


def github_with(releases_by_repo):
    """A GitHub client whose HTTP layer answers from releases_by_repo; .calls lists the repos asked for."""
    gh = GitHub(FakeCache(), None)
    gh.calls = []

    def request(path, etag):
        repo = path.split("/repos/", 1)[1].split("/releases", 1)[0]
        gh.calls.append(repo)
        return 200, releases_by_repo[repo], '"etag"'

    gh._request = request
    return gh


class PinStoreTest(unittest.TestCase):
    def test_a_pin_round_trips_through_the_settings(self):
        settings = FakeSettings()
        pins.set_pin(settings, "demo", "hold", "3.12.0")
        self.assertEqual(pins.pin_for(settings, "demo"), {"mode": "hold", "version": "3.12.0"})
        self.assertEqual(settings.values["pins"], {"demo": {"mode": "hold", "version": "3.12.0"}})
        self.assertEqual(settings.saved, 1)
        pins.set_pin(settings, "demo", "major", "3.12.0")
        self.assertEqual(pins.pin_for(settings, "demo")["mode"], "major")

    def test_other_apps_pins_are_left_alone_and_none_removes_one(self):
        settings = FakeSettings({"pins": {"other": {"mode": "hold", "version": "1.0"}}})
        pins.set_pin(settings, "demo", "major", "2.0")
        pins.set_pin(settings, "demo", None)
        self.assertIsNone(pins.pin_for(settings, "demo"))
        self.assertEqual(pins.pin_for(settings, "other"), {"mode": "hold", "version": "1.0"})
        pins.set_pin(settings, "never-pinned", None)   # removing what is not there is fine
        self.assertEqual(settings.saved, 3)

    def test_an_unknown_mode_is_refused_and_changes_nothing(self):
        settings = FakeSettings()
        with self.assertRaises(ValueError):
            pins.set_pin(settings, "demo", "freeze", "1.0")
        self.assertEqual((settings.values, settings.saved), ({}, 0))

    def test_damaged_settings_read_as_not_pinned(self):
        for label, value in [("not a dict", ["demo"]), ("None", None), ("entry not a dict", {"demo": "hold"}),
                             ("unknown mode", {"demo": {"mode": "freeze", "version": "1"}}),
                             ("no mode", {"demo": {"version": "1"}})]:
            with self.subTest(label):
                self.assertIsNone(pins.pin_for(FakeSettings({"pins": value}), "demo"))

    def test_a_missing_or_odd_version_reads_as_empty(self):
        for raw in ({"mode": "hold"}, {"mode": "hold", "version": 3}, {"mode": "hold", "version": None}):
            self.assertEqual(pins.pin_for(FakeSettings({"pins": {"d": raw}}), "d"), {"mode": "hold", "version": ""})

    def test_set_pin_survives_a_damaged_pins_value(self):
        settings = FakeSettings({"pins": "oops"})
        pins.set_pin(settings, "demo", "hold", "1.0")
        self.assertEqual(settings.values["pins"], {"demo": {"mode": "hold", "version": "1.0"}})


class BlocksTest(unittest.TestCase):
    HOLD = {"mode": "hold", "version": "3.12.0"}
    MAJOR = {"mode": "major", "version": "3.12.0"}

    def test_decisions(self):
        cases = [
            # (label, pin, installed, candidate, blocked)
            ("no pin", None, "3.12.0", "4.0.0", False),
            ("hold blocks a patch", self.HOLD, "3.12.0", "3.12.1", True),
            ("hold blocks a major", self.HOLD, "3.12.0", "4.0.0", True),
            ("hold blocks what it cannot read", self.HOLD, "3.12.0", "latest", True),
            ("major allows the same line", self.MAJOR, "3.12.0", "3.13.0", False),
            ("major allows a patch", self.MAJOR, "3.12.0", "3.12.9", False),
            ("major blocks the next major", self.MAJOR, "3.12.0", "4.0.0", True),
            ("major blocks 4.0 written 4", self.MAJOR, "3.12.0", "4", True),
            ("major blocks what it cannot read", self.MAJOR, "3.12.0", "stable", True),
            ("major blocks an empty candidate", self.MAJOR, "3.12.0", "", True),
            ("major follows the installed line, not the pinned one", self.MAJOR, "4.1.0", "4.2.0", False),
            ("... and blocks the one after it", self.MAJOR, "4.1.0", "5.0.0", True),
            ("major falls back to the pinned version", self.MAJOR, "", "3.13.0", False),
            ("... and blocks outside it", self.MAJOR, "", "4.0.0", True),
            ("major with nothing to go by blocks", {"mode": "major", "version": ""}, "", "3.13.0", True),
            ("major reads a v prefix", self.MAJOR, "v3.12.0", "v3.14.1", False),
            ("major with Git for Windows numbering", {"mode": "major", "version": "2.51.0"}, "2.51.0.2",
             "2.52.0.windows.1", False),
            ("major on date versions: same year", {"mode": "major", "version": "2024.03.1"}, "2024.03.1", "2024.11.2",
             False),
            ("major on date versions: next year", {"mode": "major", "version": "2024.03.1"}, "2024.03.1", "2025.1.0",
             True),
            ("a damaged pin blocks nothing", {"mode": "freeze", "version": "3"}, "3.12.0", "4.0.0", False),
            ("a pin that is not a dict blocks nothing", "hold", "3.12.0", "4.0.0", False),
        ]
        for label, pin, installed, candidate, blocked in cases:
            with self.subTest(label):
                self.assertEqual(pins.blocks(pin, installed, candidate), blocked)

    def test_major_line(self):
        self.assertEqual(pins.major_line(self.MAJOR, "7.1"), 7)
        self.assertEqual(pins.major_line(self.MAJOR, ""), 3)
        self.assertEqual(pins.major_line(self.MAJOR, "unknown"), 3)
        self.assertIsNone(pins.major_line(None, "unknown"))


WINGET_VERSIONS = """\
\r   - \r   \\ \r                                                                                                                        \rFound Git [Git.Git]
Version
--------
2.56.0.4
2.56.0.3
2.56.0
2.55.0.5
2.55.0.5
1.9.5.msysgit.1
"""


class VersionListTest(unittest.TestCase):
    def test_parse_what_winget_prints(self):
        lines = winget._lines(WINGET_VERSIONS.replace("\n", "\r\n").encode())
        self.assertEqual(winget.parse_versions(lines),
                         ["2.56.0.4", "2.56.0.3", "2.56.0", "2.55.0.5", "1.9.5.msysgit.1"])

    def test_a_translated_header_and_a_longer_dash_line_parse_the_same(self):
        lines = ["Gefunden: Git [Git.Git]", "Version", "-" * 30, "2.1", "2.0"]
        self.assertEqual(winget.parse_versions(lines), ["2.1", "2.0"])

    def test_the_list_ends_at_the_first_blank_line(self):
        self.assertEqual(winget.parse_versions(["Found X [X.X]", "Version", "-------", "1.0", "", "A footer"]), ["1.0"])

    def test_no_table_means_no_versions(self):
        for lines in ([], [""], ["No package found matching input criteria."], ["-------"]):
            with self.subTest(lines):
                self.assertEqual(winget.parse_versions(lines), [])

    def test_versions_asks_winget_for_exactly_that_package(self):
        with mock.patch.object(winget, "_run", return_value=["x", "Version", "---", "2.0", "1.0"]) as run:
            self.assertEqual(winget.versions("Demo.App"), ["2.0", "1.0"])
        self.assertEqual(run.call_args.args[0], ["show", "--id", "Demo.App", "--exact", "--source", "winget", "--versions"])

    def test_available_versions(self):
        with mock.patch.object(winget, "available", return_value=True), \
                mock.patch.object(winget, "versions", return_value=["2.0", "Unknown", "1.0"]) as versions:
            self.assertEqual(pins.available_versions("Demo.App"), ["2.0", "1.0"])
        versions.assert_called_once_with("Demo.App")

    def test_available_versions_is_empty_when_winget_is_missing_or_fails(self):
        with mock.patch.object(winget, "available", return_value=False), \
                mock.patch.object(winget, "versions", side_effect=AssertionError("must not run")):
            self.assertEqual(pins.available_versions("Demo.App"), [])
            self.assertEqual(pins.available_versions(""), [])
        with mock.patch.object(winget, "available", return_value=True):
            for error in (FileNotFoundError("winget"), OSError("denied"), RuntimeError("boom")):
                with mock.patch.object(winget, "versions", side_effect=error), self.assertLogs("upstream.pins", "WARNING"):
                    self.assertEqual(pins.available_versions("Demo.App"), [], repr(error))


class RollbackTest(unittest.TestCase):
    FLAGS = ["--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]

    def test_the_command_lines(self):
        self.assertEqual(winget._update_args("Demo.App", "upgrade", None),
                         ["winget", "upgrade", "--id", "Demo.App", "--exact", "--source", "winget", *self.FLAGS])
        self.assertEqual(winget._update_args("Demo.App", "install", None),
                         ["winget", "install", "--id", "Demo.App", "--exact", "--source", "winget", *self.FLAGS])
        self.assertEqual(winget._update_args("Demo.App", "rollback", "2.0.1"),
                         ["winget", "install", "--id", "Demo.App", "--version", "2.0.1", "--exact", "--source",
                          "winget", "--force", *self.FLAGS])

    def test_a_rollback_without_a_version_and_an_unknown_mode_are_refused(self):
        for mode, version in [("rollback", None), ("rollback", ""), ("downgrade", "1.0"), ("", None)]:
            with self.subTest(mode=mode, version=version), self.assertRaises(ValueError):
                winget._update_args("Demo.App", mode, version)

    def test_update_runs_that_command_and_nothing_else(self):
        started = []
        real = winget.subprocess.Popen

        def fake_popen(args, **kwargs):
            started.append(args)
            return real([sys.executable, "-u", "-c", "print('Successfully installed')"], **kwargs)

        with mock.patch.object(winget.subprocess, "Popen", side_effect=fake_popen):
            code, last = winget.update("Demo.App", "rollback", lambda pct: None, lambda line: None, version="2.0.1")
        self.assertEqual((code, last), (0, "Successfully installed"))
        self.assertEqual(started, [winget._update_args("Demo.App", "rollback", "2.0.1")])

    def test_update_refuses_a_rollback_before_starting_winget(self):
        with mock.patch.object(winget.subprocess, "Popen", side_effect=AssertionError("must not start")):
            with self.assertRaises(ValueError):
                winget.update("Demo.App", "rollback", lambda pct: None, lambda line: None)

    def test_rollback_update_describes_the_install_of_that_version(self):
        status = AppStatus(make_app(), winget_id="Demo.App")
        self.assertEqual(pins.rollback_update(status, "2.0.1"),
                         Update("winget", "2.0.1", "Demo.App", winget_mode="rollback"))

    def test_no_winget_id_or_version_means_no_rollback(self):
        self.assertIsNone(pins.rollback_update(AppStatus(make_app()), "2.0.1"))
        self.assertIsNone(pins.rollback_update(AppStatus(make_app(), winget_id="Demo.App"), ""))


class ScannerPinTest(unittest.TestCase):
    """The scanner offers what the pin lets through, from GitHub, a site rule and winget alike."""

    RELEASES = [api_release("v4.0.0"), api_release("v3.13.0"), api_release("v3.12.4"), api_release("v3.12.0")]

    def scanner(self, pin=None, releases=None, **settings):
        values = {"pins": {"demo": pin}} if pin else {}
        sc = Scanner(FakeSettings(values, overrides={"demo": {"github": "o/demo"}}), FakeCache())
        sc._gh = github_with({"o/demo": self.RELEASES if releases is None else releases})
        return sc

    def resolve(self, pin=None, version="3.12.0", releases=None, winget_available="", repo=True):
        sc = self.scanner(pin, releases)
        if not repo:
            sc.settings.overrides = {}
            sc._gh.discover = lambda *a, **k: None
        status = AppStatus(make_app(version=version), winget_id="Demo.App" if winget_available else None,
                           winget_available=winget_available)
        sc.resolve(status, False)
        return sc, status

    def test_without_a_pin_the_newest_release_is_offered(self):
        _, status = self.resolve()
        self.assertEqual((status.update.source, status.update.version), ("github", "4.0.0"))
        self.assertIsNone(status.pin)
        self.assertEqual(status.note, "")

    def test_hold_offers_nothing_and_says_why(self):
        sc, status = self.resolve({"mode": "hold", "version": "3.12.0"})
        self.assertIsNone(status.update)
        self.assertEqual(status.fallbacks, [])
        self.assertEqual(status.pin, {"mode": "hold", "version": "3.12.0"})
        self.assertEqual((status.note, status.note_args), ("Held at {version}", {"version": "3.12.0"}))
        self.assertEqual(sc._gh.calls, ["o/demo"])

    def test_hold_without_a_pinned_version_names_the_installed_one(self):
        _, status = self.resolve({"mode": "hold", "version": ""})
        self.assertEqual(status.note_args, {"version": "3.12.0"})

    def test_hold_with_nothing_newer_says_nothing(self):
        _, status = self.resolve({"mode": "hold", "version": "3.12.0"}, version="4.0.0")
        self.assertIsNone(status.update)
        self.assertEqual(status.note, "")
        self.assertEqual(status.pin["mode"], "hold")

    def test_major_offers_the_newest_release_of_the_line(self):
        sc, status = self.resolve({"mode": "major", "version": "3.12.0"})
        self.assertEqual((status.update.source, status.update.version), ("github", "3.13.0"))
        self.assertEqual(status.update.download_url,
                         "https://github.com/o/demo/releases/download/v3.13.0/Demo-Setup-x64.exe")
        self.assertEqual(status.update.notes, "Notes for v3.13.0")
        self.assertEqual(status.fallbacks, [])
        self.assertEqual(sc._gh.calls, ["o/demo"], "looking through the releases costs no second request")

    def test_major_with_nothing_newer_in_the_line_offers_nothing(self):
        _, status = self.resolve({"mode": "major", "version": "3.12.0"}, version="3.13.0")
        self.assertIsNone(status.update)
        self.assertEqual((status.note, status.note_args), ("Staying on version {line}", {"line": "3.x"}))

    def test_major_follows_the_line_the_app_is_on(self):
        _, status = self.resolve({"mode": "major", "version": "3.12.0"}, version="4.0.0",
                                 releases=[api_release("v5.0.0"), api_release("v4.2.0"), api_release("v4.0.0")])
        self.assertEqual(status.update.version, "4.2.0")

    def test_major_in_the_newest_line_is_not_held_back(self):
        _, status = self.resolve({"mode": "major", "version": "4.0.0"}, version="4.0.0",
                                 releases=[api_release("v4.1.0"), api_release("v4.0.0")])
        self.assertEqual(status.update.version, "4.1.0")
        self.assertEqual(status.note, "")

    def test_the_line_skips_a_release_without_a_windows_installer(self):
        _, status = self.resolve({"mode": "major", "version": "3.12.0"},
                                 releases=[api_release("v4.0.0"), api_release("v3.13.0", ("Demo-3.13.0.tar.gz",)),
                                           api_release("v3.12.4")])
        self.assertEqual(status.update.version, "3.12.4")

    def test_major_winget_update_inside_the_line_is_offered(self):
        _, status = self.resolve({"mode": "major", "version": "3.12.0"}, winget_available="3.14.0", repo=False)
        self.assertEqual((status.update.source, status.update.version), ("winget", "3.14.0"))
        self.assertEqual(status.note, "")

    def test_major_winget_update_into_the_next_major_behaves_like_hold(self):
        _, status = self.resolve({"mode": "major", "version": "3.12.0"}, winget_available="4.0.0", repo=False)
        self.assertIsNone(status.update)
        self.assertEqual(status.note, "Staying on version {line}")

    def test_hold_blocks_every_source(self):
        _, status = self.resolve({"mode": "hold", "version": "3.12.0"}, winget_available="4.0.0")
        self.assertEqual((status.update, status.fallbacks), (None, []))

    def test_a_blocked_winget_version_is_not_left_as_a_fallback(self):
        _, status = self.resolve({"mode": "major", "version": "3.12.0"}, winget_available="4.0.0")
        self.assertEqual(status.update.version, "3.13.0")
        self.assertEqual(status.fallbacks, [])

    def test_a_forced_winget_install_is_held_too(self):
        sc = self.scanner({"mode": "hold", "version": "3.12.0"}, releases=[])
        sc.cache.put("winget_meta", "Demo.App", {"v": winget.META_VERSION, "version": "9.0", "urls": []})
        status = AppStatus(make_app(), winget_id="Demo.App", winget_forced=True)
        sc.resolve(status, False)
        self.assertIsNone(status.update)
        self.assertEqual(status.note, "Held at {version}")

    def test_a_site_rule_is_held_too(self):
        sc = self.scanner({"mode": "major", "version": "3.12.0"}, releases=[])
        sc.settings.overrides = {"demo": {"site": {"url": "https://demo.example/dl", "version_regex": "x"}}}
        for found, offered in [("4.1.0", None), ("3.20.0", "3.20.0")]:
            with self.subTest(found), mock.patch.object(scanner.site, "check", return_value=(found, "https://d/x.exe")):
                status = AppStatus(make_app())
                sc.resolve(status, False)
                self.assertEqual(status.update and status.update.version, offered)

    def test_removing_the_pin_restores_the_update_on_the_next_check(self):
        sc = self.scanner({"mode": "hold", "version": "3.12.0"})
        status = AppStatus(make_app())
        sc.resolve(status, False)
        self.assertIsNone(status.update)
        pins.set_pin(sc.settings, "demo", None)
        sc.resolve(status, False)
        self.assertEqual((status.update.version, status.pin, status.note), ("4.0.0", None, ""))

    def test_a_github_failure_does_not_hide_the_hold_note(self):
        sc = self.scanner({"mode": "hold", "version": "3.12.0"})
        status = AppStatus(make_app(), winget_id="Demo.App", winget_available="4.0.0")
        sc._gh.check = mock.Mock(side_effect=OSError("offline"))
        with self.assertLogs("upstream.scan", "WARNING"):
            sc.resolve(status, False)
        self.assertEqual(status.note, "Held at {version}")


class TranslationTest(unittest.TestCase):
    def test_the_notes_have_persian_entries_with_the_same_placeholders(self):
        fields = lambda t: {name for _, name, _, _ in string.Formatter().parse(t) if name}
        self.assertEqual(set(pins_fa.FA), {"Held at {version}", "Staying on version {line}"})
        for english, persian in pins_fa.FA.items():
            self.assertEqual(fields(english), fields(persian), english)


if __name__ == "__main__":
    unittest.main()
