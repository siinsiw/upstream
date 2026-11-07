"""Security and end-of-life badges: release-note parsing and negations, release-line matching, dates against a
fake clock, the outdated rule, which apps map to which product, and refreshing the tables from a local server."""
from __future__ import annotations

import copy
import http.server
import json
import string
import threading
import time
import unittest
import urllib.error
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from upstream import badges, i18n, net
from upstream.translations import badges as badge_translations

TODAY = date(2026, 11, 8)


def make_status(name="Demo App", version="1.0", winget_id=None, offered=None, notes=""):
    update = SimpleNamespace(version=offered, notes=notes) if offered is not None else None
    return SimpleNamespace(app=SimpleNamespace(name=name, version=version, publisher=""), winget_id=winget_id,
                           update=update)


def release(notes, version="2.0"):
    return make_status(offered=version, notes=notes)


class FakeCache:
    """Just the part of config.Cache that badges.py uses."""

    def __init__(self, **tables):
        self.data = {"eol": {slug: {"cycles": cycles, "ts": time.time()} for slug, cycles in tables.items()}}

    def entry(self, section, key):
        value = self.data[section].get(key)
        return copy.deepcopy(value) if value is not None else None

    def put(self, section, key, value):
        self.data[section][key] = dict(value, ts=time.time())


class QuietCase(unittest.TestCase):
    """The badge code logs a warning when it swallows an error; these tests provoke that on purpose."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(badges, "log")
        patcher.start()
        self.addCleanup(patcher.stop)


def cycle(name, eol, lts=False):
    return {"cycle": name, "eol": eol, "lts": lts}


def iso(days_from_today):
    return (TODAY + timedelta(days=days_from_today)).isoformat()


NODE_SECURITY_RELEASE = """## 2024-02-14, Version 20.11.1 'Iron' (LTS), @RafaelGSS

This is a security release.

### Notable Changes

* CVE-2024-21892 - Code injection and privilege escalation through Linux capabilities- (High)
* CVE-2024-22019 - http: Reading unprocessed HTTP request with unbounded chunk extension allows DoS attacks (High)
* CVE-2023-46809 - Node.js is vulnerable to the Marvin Attack (timing variant of the Bleichenbacher attack) (Medium)
* CVE-2024-22025 - Denial of Service by resource exhaustion in fetch() brotli decode (Medium)
* CVE-2024-21892 - repeated"""

PLAIN_GITHUB_NOTES = """## What's Changed
* Fix crash when opening large files by @alice in #123
* Bump lodash from 4.17.20 to 4.17.21 by @dependabot in #124
* Improve start-up time

**Full Changelog**: https://github.com/octo/demo/compare/v1.2.3...v1.2.4"""


class NotesTest(QuietCase):
    FOUND = [
        ("Fixes CVE-2024-3094 and CVE-2024-3095 in the parser.", ["CVE-2024-3094", "CVE-2024-3095"], False),
        ("see cve-2023-12345 for details", ["CVE-2023-12345"], False),
        ("Fixed a vulnerability in the PDF reader (GHSA-J8R2-6x86-q33q).", ["GHSA-j8r2-6x86-q33q"], True),
        ("Security: this release fixes a vulnerability in the auto-updater (CVE-2023-1234).", ["CVE-2023-1234"], True),
        ("No longer affected by CVE-2024-5555 after the rewrite.", ["CVE-2024-5555"], False),
        ("This release includes a security fix for the updater.", [], True),
        ("Security update: bump openssl", [], True),
        ("Includes several security patches.", [], True),
        ("This is a security release.", [], True),
        ("Patched multiple vulnerabilities in the installer.", [], True),
        ("No new features, only security fixes.", [], True),
        ("No bug fixes, but security updates.", [], True),
        ("Not only bug fixes: also a security patch.", [], True),
        ("Fixed a bug. Security fixes are included.", [], True),
        ("Security-fix for the zip extractor", [], True),
    ]
    NOTHING = [
        "No security fixes in this release.",
        "This release contains no security changes.",
        "Does not include any security updates.",
        "Not a security release; bug fixes only.",
        "There are no known vulnerabilities.",
        "Without any security fixes this is a small update.",
        "isn't a security patch",
        "Security fixes: none",
        "Vulnerabilities: N/A",
        "Security updates - none",
        "To report a security vulnerability, see SECURITY.md.",
        "Please report vulnerabilities to the maintainers.",
        "Read our vulnerability disclosure policy.",
        "Not affected by CVE-2024-1234.",
        "This does not fix CVE-2024-1234.",
        "Fixed crash on startup. Improved performance.",
        PLAIN_GITHUB_NOTES,
        "",
    ]

    def test_positive_notes(self):
        for text, ids, phrase in self.FOUND:
            with self.subTest(text=text):
                self.assertEqual(badges._scan_notes(text), (ids, phrase))

    def test_negations_and_boilerplate_do_not_count(self):
        for text in self.NOTHING:
            with self.subTest(text=text):
                self.assertEqual(badges._scan_notes(text), ([], False))
                self.assertEqual(badges.compute(release(text)), [])

    def test_a_negation_stops_at_the_end_of_the_sentence(self):
        self.assertEqual(badges._scan_notes("There is no changelog. Security fixes inside."), ([], True))
        self.assertEqual(badges._scan_notes("Nothing breaks\nSecurity update included"), ([], True))

    def test_no_notes_no_update_or_a_broken_update(self):
        self.assertEqual(badges.compute(make_status()), [])
        self.assertEqual(badges.compute(release(None)), [])
        self.assertEqual(badges.compute(SimpleNamespace(update=SimpleNamespace(notes=5, version="2"))), [])


class SecurityBadgeTest(QuietCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(i18n.set_language, "en")

    def test_ids_are_listed_once_up_to_three(self):
        [badge] = badges.compute(release("Fixes CVE-2024-1111, CVE-2024-2222 and cve-2024-1111."))
        self.assertEqual((badge.kind, badge.text), ("security", "Security fix"))
        self.assertEqual(badge.values, {"ids": "CVE-2024-1111, CVE-2024-2222"})
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         "The release notes of this update mention CVE-2024-1111, CVE-2024-2222.")

    def test_more_than_three_ids_say_and_others(self):
        [badge] = badges.compute(make_status(version="20.0.0", offered="20.11.1", notes=NODE_SECURITY_RELEASE))
        self.assertEqual(badge.values, {"ids": "CVE-2024-21892, CVE-2024-22019, CVE-2023-46809"})
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         "The release notes of this update mention CVE-2024-21892, CVE-2024-22019, "
                         "CVE-2023-46809 and others.")

    def test_exactly_three_ids_have_no_and_others(self):
        [badge] = badges.compute(release("CVE-2024-1111 CVE-2024-2222 CVE-2024-3333"))
        self.assertNotIn("others", badge.tooltip)

    def test_a_phrase_without_ids(self):
        [badge] = badges.compute(release("This update contains a security patch."))
        self.assertEqual(badge.values, {})
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         "The release notes of this update mention a security fix or a vulnerability.")

    def test_persian_text_renders_with_the_ids(self):
        [badge] = badges.compute(release("CVE-2024-1111"))
        i18n.set_language("fa")
        self.assertEqual(i18n.render(badge.text, badge.values), badge_translations.FA["Security fix"])
        shown = i18n.render(badge.tooltip, badge.values)
        self.assertIn("CVE-2024-1111", shown)
        self.assertNotIn("{", shown)


class CycleTest(QuietCase):
    PYTHON = [cycle("3.13", "2029-10-31"), cycle("3.12", "2028-10-31"), cycle("3.9", "2025-10-31"),
              cycle("3.8", "2024-10-07")]

    def name(self, cycles, version):
        found = badges.find_cycle(cycles, version)
        return found["cycle"] if found else None

    def test_minor_lines(self):
        self.assertEqual(self.name(self.PYTHON, "3.12.4"), "3.12")
        self.assertEqual(self.name(self.PYTHON, "3.12"), "3.12")
        self.assertEqual(self.name(self.PYTHON, "3.12.4150.0"), "3.12")
        self.assertEqual(self.name(self.PYTHON, "3.8.10"), "3.8")

    def test_numbers_are_compared_not_text(self):
        cycles = [cycle("1.2", "2020-01-01"), cycle("1.20", "2030-01-01")]
        self.assertEqual(self.name(cycles, "1.20.3"), "1.20")
        self.assertEqual(self.name(cycles, "1.2.9"), "1.2")
        self.assertIsNone(self.name([cycle("1.2", "2020-01-01")], "1.20.3"))
        self.assertIsNone(self.name([cycle("26.10", "2030-01-01")], "26.1.0"))

    def test_major_only_lines(self):
        node = [cycle("22", "2027-04-30"), cycle("20", "2026-04-30"), cycle("0.12", "2016-12-31")]
        self.assertEqual(self.name(node, "20.11.1"), "20")
        self.assertEqual(self.name(node, "v20.11.1"), "20")
        self.assertEqual(self.name(node, "0.12.7"), "0.12")
        self.assertIsNone(self.name(node, "21.0.0"))

    def test_the_longest_matching_line_wins(self):
        dotnet = [cycle("3", "2030-01-01"), cycle("3.1", "2022-12-13"), cycle("8", "2026-11-10")]
        self.assertEqual(self.name(dotnet, "3.1.32.31915"), "3.1")
        self.assertEqual(self.name(dotnet, "3.0.3"), "3")
        self.assertEqual(self.name(dotnet, "8.0.4.33307"), "8")

    def test_a_version_shorter_than_the_line_does_not_match(self):
        self.assertIsNone(self.name(self.PYTHON, "3"))
        self.assertIsNone(self.name(self.PYTHON, ""))
        self.assertIsNone(self.name(self.PYTHON, "unknown"))
        self.assertIsNone(self.name(self.PYTHON, None))

    def test_junk_in_the_table_is_skipped(self):
        junk = ["x", None, {"eol": "2030-01-01"}, {"cycle": 3.12}, {"cycle": "lts"}, {"cycle": "v3.12"},
                cycle("3.12", "2028-10-31")]
        self.assertEqual(self.name(junk, "3.12.1"), "3.12")
        self.assertIsNone(badges.find_cycle(None, "3.12"))
        self.assertIsNone(badges.find_cycle({"cycle": "3.12"}, "3.12"))


class ProductTest(QuietCase):
    MAPPED = [
        ("Python 3.12.4 (64-bit)", None, "python"), ("Python 3.9.13 (32-bit)", None, "python"),
        ("Python 3.4.3", None, "python"), ("Python 3.12", "Python.Python.3.12", "python"),
        ("whatever", "Python.Python.3.11", "python"),
        ("Node.js", None, "nodejs"), ("Node.js", "OpenJS.NodeJS.LTS", "nodejs"), ("x", "OpenJS.NodeJS", "nodejs"),
        ("PHP 8.3.4", None, "php"), ("x", "PHP.PHP.8.3", "php"), ("x", "PHP.PHP.NTS.8.3", "php"),
        ("Microsoft .NET Runtime - 8.0.4 (x64)", None, "dotnet"), ("Microsoft .NET SDK 8.0.202 (x64)", None, "dotnet"),
        ("Microsoft Windows Desktop Runtime - 8.0.4 (x64)", None, "dotnet"),
        ("Microsoft ASP.NET Core 8.0.4 - Shared Framework", None, "dotnet"),
        ("Microsoft .NET Core Runtime - 3.1.32 (x64)", None, "dotnet"),
        ("Microsoft .NET Host - 8.0.4 (x64)", None, "dotnet"), ("x", "Microsoft.DotNet.SDK.8", "dotnet"),
        ("x", "Microsoft.DotNet.DesktopRuntime.8", "dotnet"), ("x", "Microsoft.DotNet.AspNetCore.8", "dotnet"),
        ("PowerShell 7-x64", None, "powershell"), ("PowerShell", "Microsoft.PowerShell", "powershell"),
        ("Eclipse Temurin JDK with Hotspot 17.0.9+9 (x64)", None, "eclipse-temurin"),
        ("Eclipse Temurin JRE with Hotspot 21.0.2+13 (x64)", None, "eclipse-temurin"),
        ("x", "EclipseAdoptium.Temurin.17.JDK", "eclipse-temurin"),
        ("Microsoft Build of OpenJDK with Hotspot 17.0.10+7 (x64)", None, "microsoft-build-of-openjdk"),
        ("x", "Microsoft.OpenJDK.21", "microsoft-build-of-openjdk"),
        ("Amazon Corretto 17.0.10.7.1", None, "amazon-corretto"),
        ("MySQL Server 8.0", None, "mysql"), ("MySQL Server 8.4", None, "mysql"),
        ("MariaDB 11.4 (x64)", None, "mariadb"), ("x", "MariaDB.Server", "mariadb"),
        ("PostgreSQL 16", None, "postgresql"), ("x", "PostgreSQL.PostgreSQL.16", "postgresql"),
        ("MongoDB 7.0.5 2008R2Plus SSL (64 bit)", None, "mongodb"), ("x", "MongoDB.Server", "mongodb"),
        ("Redis", None, "redis"), ("nginx", None, "nginx"),
        ("Mozilla Firefox (x64 en-US)", None, "firefox"), ("Mozilla Firefox ESR (x64 en-US)", None, "firefox"),
        ("Mozilla Firefox 122.0 (x64 en-US)", None, "firefox"), ("x", "Mozilla.Firefox", "firefox"),
        ("x", "Mozilla.Firefox.ESR", "firefox"), ("x", "Mozilla.Firefox.fa", "firefox"),
        ("x", "Mozilla.Firefox.en-US", "firefox"),
        ("Google Chrome", None, "chrome"), ("x", "Google.Chrome", "chrome"),
        ("Notepad++ (64-bit x64)", None, "notepad-plus-plus"), ("x", "Notepad++.Notepad++", "notepad-plus-plus"),
        ("LibreOffice 24.8.2.1", None, "libreoffice"), ("Oracle VM VirtualBox 7.0.14", None, "virtualbox"),
        ("Wireshark 4.2.3 x64", None, "wireshark"),
    ]
    UNMAPPED = [
        ("Python Launcher", None), ("Python 3.12.4 Documentation (64-bit)", None),
        ("Python 3.12.4 Core Interpreter (64-bit)", None), ("x", "Python.Launcher"),
        ("Node.js Tools for Visual Studio", None), ("nvm for Windows", None),
        ("PHP Storm", None), ("PhpStorm 2024.1", None),
        ("Microsoft .NET Framework 4.8", None), ("Microsoft .NET Native Toolchain", None),
        ("x", "Microsoft.DotNet.SDK.Preview"), ("x", "Microsoft.DotNet.Framework.DeveloperPack_4"),
        ("PowerShell 7-preview-x64", None), ("x", "Microsoft.PowerShell.Preview"), ("Windows PowerShell ISE", None),
        ("MySQL Workbench 8.0 CE", None), ("MySQL Connector/NET 8.0.36", None), ("MySQL Shell", None),
        ("MongoDB Compass", None), ("MongoDB Shell", None), ("x", "MongoDB.Compass.Full"),
        ("PostgreSQL ODBC Driver", None), ("pgAdmin 4", None),
        ("Mozilla Maintenance Service", None), ("Firefox Developer Edition", None), ("x", "Mozilla.Firefox.Nightly"),
        ("x", "Mozilla.Firefox.Beta"), ("x", "Mozilla.Firefox.DeveloperEdition"),
        ("Google Chrome Beta", None), ("Google Chrome Remote Desktop Host", None),
        ("Notepad++ plugin manager", None), ("LibreOffice Language Pack", None),
        ("Oracle VM VirtualBox Extension Pack", None), ("Eclipse Temurin JDK with OpenJ9 17", None),
        ("AdoptOpenJDK JRE with Hotspot 11.0.11.9 (x64)", None), ("Redis Insight", None), ("Demo App", None),
        ("", None),
    ]

    def test_names_and_winget_ids_map_to_one_product(self):
        for name, winget_id, slug in self.MAPPED:
            with self.subTest(name=name, winget_id=winget_id):
                found = badges.product_for(make_status(name, winget_id=winget_id))
                self.assertEqual(found.slug if found else None, slug)

    def test_look_alikes_are_left_out(self):
        for name, winget_id in self.UNMAPPED:
            with self.subTest(name=name, winget_id=winget_id):
                self.assertIsNone(badges.product_for(make_status(name, winget_id=winget_id)))

    def test_no_name_fits_two_products(self):
        for name, winget_id, _ in self.MAPPED:
            hits = {p.slug for p in badges.PRODUCTS
                    if any(r.fullmatch(name) for r in p.names) or any(r.fullmatch(winget_id or "") for r in p.winget)}
            self.assertEqual(len(hits), 1, (name, winget_id, hits))

    def test_slugs_are_unique_and_rolling_products_are_the_fast_movers(self):
        slugs = [p.slug for p in badges.PRODUCTS]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual({p.slug for p in badges.PRODUCTS if p.rolling}, {"firefox", "chrome", "notepad-plus-plus"})

    def test_the_default_address_is_the_public_one(self):
        self.assertEqual(badges.EOL_API, "https://endoflife.date/api")

    def test_objects_without_the_usual_attributes(self):
        self.assertIsNone(badges.product_for(object()))
        self.assertIsNone(badges.product_for(SimpleNamespace(app=None, winget_id=None)))


class EolBadgeTest(QuietCase):
    PYTHON = [cycle("3.13", "2029-10-31"), cycle("3.12", "2028-10-31"), cycle("3.9", "2025-10-31"),
              cycle("3.8", "2024-10-07")]

    def badge(self, name, version, cycles, slug="python", today=TODAY, **kw):
        cache = FakeCache(**{slug: cycles})
        found = badges.compute(make_status(name, version, **kw), cache, today=today)
        return [b for b in found if b.kind == "eol"]

    def python(self, version, cycles=None, today=TODAY):
        return self.badge(f"Python {version} (64-bit)", version, cycles or self.PYTHON, today=today)

    def test_past_end_of_life(self):
        [badge] = self.python("3.8.10")
        self.assertEqual((badge.kind, badge.text), ("eol", "End of life"))
        self.assertEqual(badge.values, {"product": "Python", "cycle": "3.8", "date": "2024-10-07"})
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         "Support for Python 3.8 ended on 2024-10-07. It no longer receives security fixes.")

    def test_supported_release_has_no_badge(self):
        self.assertEqual(self.python("3.12.4"), [])

    def test_upcoming_within_ninety_days(self):
        table = [cycle("3.9", iso(60))]
        [badge] = self.python("3.9.1", table)
        self.assertEqual((badge.text, badge.values["date"]), ("Support ends soon", iso(60)))
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         f"Support for Python 3.9 ends on {iso(60)}. Plan to move to a newer release before then.")

    def test_the_ninety_day_window_and_the_end_date_itself(self):
        for days, expected in ((91, None), (90, "Support ends soon"), (1, "Support ends soon"),
                               (0, "Support ends soon"), (-1, "End of life")):
            with self.subTest(days=days):
                found = self.python("3.9.1", [cycle("3.9", iso(days))])
                self.assertEqual(found[0].text if found else None, expected)

    def test_fake_clock_moves_a_release_through_the_stages(self):
        table = [cycle("3.9", "2026-12-31")]
        stages = [(date(2026, 8, 1), None), (date(2026, 10, 15), "Support ends soon"),
                  (date(2026, 12, 31), "Support ends soon"), (date(2027, 1, 1), "End of life")]
        for today, expected in stages:
            with self.subTest(today=today):
                found = self.python("3.9.1", table, today)
                self.assertEqual(found[0].text if found else None, expected)

    def test_end_of_life_without_a_date_or_not_announced(self):
        [badge] = self.python("3.9.1", [cycle("3.9", True)])
        self.assertEqual(badge.text, "End of life")
        self.assertEqual(badge.values, {"product": "Python", "cycle": "3.9"})
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         "Support for Python 3.9 has ended. It no longer receives security fixes.")
        self.assertEqual(self.python("3.9.1", [cycle("3.9", False)]), [])
        self.assertEqual(self.python("3.9.1", [{"cycle": "3.9"}]), [])

    def test_a_date_that_cannot_be_read_is_ignored(self):
        for junk in ("soon", "", "2026-13-45", "31/12/2026", 5, None):
            with self.subTest(junk=junk):
                self.assertEqual(self.python("3.9.1", [cycle("3.9", junk)]), [])

    def test_nodejs_major_lines(self):
        table = [cycle("22", "2027-04-30"), cycle("18", "2025-04-30")]
        [badge] = self.badge("Node.js", "18.19.0", table, "nodejs")
        self.assertEqual(badge.values["cycle"], "18")
        self.assertEqual(self.badge("Node.js", "22.1.0", table, "nodejs"), [])

    def test_unknown_release_line_gets_nothing(self):
        self.assertEqual(self.python("3.14.0", self.PYTHON), [])

    def test_missing_table_or_cache(self):
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10")
        self.assertEqual(badges.compute(status, FakeCache(), today=TODAY), [])
        self.assertEqual(badges.compute(status, FakeCache(python=[]), today=TODAY), [])
        self.assertEqual(badges.compute(status, None, today=TODAY), [])
        self.assertEqual(badges.compute(status, today=TODAY), [])

    def test_only_mapped_apps_get_the_badge(self):
        cache = FakeCache(python=self.PYTHON)
        for name in ("Python Launcher", "Demo App"):
            self.assertEqual(badges.compute(make_status(name, "3.8.10"), cache, today=TODAY), [])

    def test_winget_id_maps_a_store_row(self):
        [badge] = self.badge("Python (Store)", "3.8.10", self.PYTHON, winget_id="Python.Python.3.8")
        self.assertEqual(badge.values["cycle"], "3.8")

    def test_dotnet_and_jdk_versions_from_the_registry(self):
        dotnet = [cycle("8", "2026-11-10"), cycle("6", "2024-11-12"), cycle("3.1", "2022-12-13")]
        [soon] = self.badge("Microsoft .NET Runtime - 8.0.4 (x64)", "8.0.4.33307", dotnet, "dotnet")
        self.assertEqual((soon.text, soon.values["product"]), ("Support ends soon", ".NET"))
        [gone] = self.badge("Microsoft .NET Core Runtime - 3.1.32 (x64)", "3.1.32.31915", dotnet, "dotnet")
        self.assertEqual(gone.values["cycle"], "3.1")
        jdk = [cycle("21", "2029-12-31"), cycle("11", "2027-10-31"), cycle("8", "2030-12-31")]
        self.assertEqual(self.badge("Eclipse Temurin JDK with Hotspot 17.0.9+9 (x64)", "17.0.9.9", jdk,
                                    "eclipse-temurin"), [])
        self.assertEqual(self.badge("Eclipse Temurin JDK with Hotspot 21.0.2+13 (x64)", "21.0.2.13", jdk,
                                    "eclipse-temurin"), [])

    def test_postgresql_registry_version_with_a_build_suffix(self):
        pg = [cycle("16", "2028-11-09"), cycle("12", "2024-11-21")]
        self.assertEqual(self.badge("PostgreSQL 16", "16.2-1", pg, "postgresql"), [])
        [badge] = self.badge("PostgreSQL 12", "12.18-1", pg, "postgresql")
        self.assertEqual(badge.values["cycle"], "12")

    def test_the_badge_does_not_need_an_update(self):
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10")
        self.assertIsNone(status.update)
        self.assertEqual([b.kind for b in badges.compute(status, FakeCache(python=self.PYTHON), today=TODAY)],
                         ["eol"])


class RollingProductTest(QuietCase):
    """Browsers move on every few weeks, so the release before the current one is always "past end of life"."""
    FIREFOX = [cycle("159", iso(2)), cycle("158", iso(-12)), cycle("150", iso(-100)), cycle("140", iso(-40), True),
               cycle("153", iso(30), True), cycle("128", iso(-400), True)]

    def found(self, version, name="Mozilla Firefox (x64 en-US)"):
        return badges.compute(make_status(name, version), FakeCache(firefox=self.FIREFOX), today=TODAY)

    def test_the_current_and_the_previous_release_are_fine(self):
        self.assertEqual(self.found("159.0.1"), [])
        self.assertEqual(self.found("158.0.1"), [])

    def test_a_release_long_past_its_end_is_flagged(self):
        [badge] = self.found("150.0")
        self.assertEqual((badge.text, badge.values["cycle"]), ("End of life", "150"))

    def test_the_grace_period_edges(self):
        for days_ago, expected in ((59, []), (60, ["End of life"])):
            with self.subTest(days_ago=days_ago):
                table = [cycle("150", iso(-days_ago))]
                found = badges.compute(make_status("Mozilla Firefox (x64 en-US)", "150.0"),
                                       FakeCache(firefox=table), today=TODAY)
                self.assertEqual([b.text for b in found], expected)

    def test_lts_lines_have_no_grace_and_do_get_a_warning(self):
        self.assertEqual([b.text for b in self.found("140.2.0")], ["End of life"])
        self.assertEqual([b.text for b in self.found("128.1.0")], ["End of life"])
        self.assertEqual([b.text for b in self.found("153.0")], ["Support ends soon"])

    def test_a_rolling_release_never_warns_before_its_end(self):
        table = [cycle("159", iso(20)), cycle("160", iso(80))]
        for version in ("159.0", "160.0"):
            self.assertEqual(badges.compute(make_status("Mozilla Firefox (x64 en-US)", version),
                                            FakeCache(firefox=table), today=TODAY), [])

    def test_chrome_and_notepad_follow_the_same_rule(self):
        chrome = FakeCache(chrome=[cycle("157", iso(9)), cycle("156", iso(-5)), cycle("120", iso(-300))])
        self.assertEqual(badges.compute(make_status("Google Chrome", "156.0.1"), chrome, today=TODAY), [])
        [badge] = badges.compute(make_status("Google Chrome", "120.0.6099.71"), chrome, today=TODAY)
        self.assertEqual(badge.values["product"], "Chrome")
        npp = FakeCache(**{"notepad-plus-plus": [cycle("8.9", False), cycle("8.8", iso(-20)), cycle("8.4", iso(-900))]})
        self.assertEqual(badges.compute(make_status("Notepad++ (64-bit x64)", "8.8.3"), npp, today=TODAY), [])
        self.assertEqual([b.kind for b in badges.compute(make_status("Notepad++ (64-bit x64)", "8.4.9"), npp,
                                                         today=TODAY)], ["eol"])


class OutdatedTest(QuietCase):
    def kinds(self, installed, offered, name="Demo App", **kw):
        return [b.kind for b in badges.compute(make_status(name, installed, offered=offered, **kw), today=TODAY)]

    def test_two_or_more_majors_behind(self):
        [badge] = badges.compute(make_status(version="1.9.2", offered="3.0.1"), today=TODAY)
        self.assertEqual((badge.kind, badge.text), ("outdated", "Outdated"))
        self.assertEqual(badge.values, {"installed": "1.9.2", "latest": "3.0.1"})
        self.assertEqual(i18n.render(badge.tooltip, badge.values),
                         "The installed version (1.9.2) is at least two major versions behind 3.0.1.")
        self.assertEqual(self.kinds("4.1", "9.0"), ["outdated"])
        self.assertEqual(self.kinds("2.0", "4.0.0.1"), ["outdated"])

    def test_one_major_behind_or_the_same_major_is_not_outdated(self):
        self.assertEqual(self.kinds("2.9", "3.0"), [])
        self.assertEqual(self.kinds("3.0", "3.5"), [])
        self.assertEqual(self.kinds("3.5", "3.5"), [])

    def test_the_offered_version_must_really_be_newer(self):
        self.assertEqual(self.kinds("5.0", "2.0"), [])
        self.assertEqual(self.kinds("5.0", "1.0"), [])

    def test_prerelease_and_prefixed_versions(self):
        self.assertEqual(self.kinds("1.0", "3.0.0-beta.2"), [])
        self.assertEqual(self.kinds("1.0", "3.0.0-rc1"), [])
        self.assertEqual(self.kinds("v1.0", "v3.0"), ["outdated"])
        self.assertEqual(self.kinds("7-Zip 22.01", "24.08"), ["outdated"])

    def test_years_dates_and_build_numbers_have_no_majors(self):
        self.assertEqual(self.kinds("2021.3", "2024.1"), [])
        self.assertEqual(self.kinds("20210101", "20240314"), [])
        self.assertEqual(self.kinds("1204", "1506"), [])

    def test_pre_one_point_zero_versions_have_no_majors(self):
        self.assertEqual(self.kinds("0.20", "2.0"), [])

    def test_unreadable_versions(self):
        self.assertEqual(self.kinds("", "3.0"), [])
        self.assertEqual(self.kinds("1.0", "latest"), [])
        self.assertEqual(self.kinds("unknown", "unknown"), [])

    def test_no_update_means_no_badge(self):
        self.assertEqual(badges.compute(make_status(version="1.0"), today=TODAY), [])

    def test_browsers_count_releases_not_majors(self):
        self.assertEqual(self.kinds("120.0", "125.0", name="Mozilla Firefox (x64 en-US)"), [])
        self.assertEqual(self.kinds("100.0.1", "125.0", name="Google Chrome"), [])


class ComputeTest(QuietCase):
    def test_order_and_independence_of_the_three_kinds(self):
        cache = FakeCache(python=[cycle("3.8", "2024-10-07")])
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10", offered="5.0.0", notes="Fixes CVE-2025-0001.")
        self.assertEqual([b.kind for b in badges.compute(status, cache, today=TODAY)],
                         ["security", "eol", "outdated"])

    def test_disabled_returns_nothing(self):
        cache = FakeCache(python=[cycle("3.8", "2024-10-07")])
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10", offered="5.0.0", notes="Fixes CVE-2025-0001.")
        self.assertEqual(badges.compute(status, cache, enabled=False), [])
        self.assertEqual(badges.compute(status, cache, False), [])
        self.assertEqual(len(badges.compute(status, cache, True, TODAY)), 3)

    def test_today_defaults_to_the_real_date(self):
        cache = FakeCache(python=[cycle("3.8", "2024-10-07"), cycle("3.99", "2999-01-01")])
        self.assertEqual([b.kind for b in badges.compute(make_status("Python 3.8.1", "3.8.1"), cache)], ["eol"])
        self.assertEqual(badges.compute(make_status("Python 3.99.1", "3.99.1"), cache), [])

    def test_badges_are_plain_data(self):
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10", offered="5.0.0", notes="CVE-2025-0001")
        found = badges.compute(status, FakeCache(python=[cycle("3.8", "2024-10-07")]), today=TODAY)
        for badge in found:
            self.assertTrue(all(isinstance(v, str) for v in badge.values.values()))
            self.assertEqual(json.loads(json.dumps(badge.__dict__)), badge.__dict__)
            self.assertEqual(badges.Badge(**badge.__dict__), badge)
        self.assertEqual(len(set(found)), len(found))

    def test_a_default_badge_has_empty_values(self):
        badge = badges.Badge("pinned", "Held")
        self.assertEqual((badge.tooltip, badge.values), ("", {}))
        with self.assertRaises(AttributeError):
            badge.kind = "other"

    def test_never_raises(self):
        class Exploding:
            def entry(self, *args):
                raise RuntimeError("boom")

        broken = SimpleNamespace(app=SimpleNamespace(name=None, version=None), winget_id=5,
                                 update=SimpleNamespace(version=3, notes=["x"]))
        for status in (None, object(), 5, broken, SimpleNamespace(app=None, update=None)):
            with self.subTest(status=status):
                self.assertEqual(badges.compute(status, Exploding(), today=TODAY), [])
        python = make_status("Python 3.8.10 (64-bit)", "3.8.10", offered="5.0.0", notes="CVE-2025-0001")
        found = badges.compute(python, Exploding(), today=TODAY)
        self.assertEqual([b.kind for b in found], ["security", "outdated"])

    def test_real_scan_objects(self):
        from upstream.inventory import InstalledApp
        from upstream.scanner import AppStatus, Update
        app = InstalledApp(reg_key=r"HKLM64\Python", name="Python 3.8.10 (64-bit)", version="3.8.10",
                           publisher="Python Software Foundation", scope="machine", arch="x64")
        status = AppStatus(app, update=Update("github", "3.8.20", "python/cpython", notes="Security fix: CVE-2024-6232"))
        found = badges.compute(status, FakeCache(python=[cycle("3.8", "2024-10-07")]), today=TODAY)
        self.assertEqual([b.kind for b in found], ["security", "eol"])


class EolServer(http.server.ThreadingHTTPServer):
    """Serves routes[path] = (status, body bytes); anything else is 404. Remembers every path asked for."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), EolHandler)
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.requests: list[str] = []

    def table(self, slug, cycles, status=200):
        self.routes[f"/api/{slug}.json"] = (status, json.dumps(cycles).encode())


class EolHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        status, body = self.server.routes.get(self.path, (404, b'{"error": "unknown product"}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


# The shape endoflife.date really serves: more fields than we keep, `eol` a date or a boolean, `lts` a bool or a date.
PYTHON_API = [
    {"cycle": "3.13", "releaseDate": "2024-10-07", "eol": "2029-10-31", "latest": "3.13.18", "lts": False,
     "support": "2026-10-01", "pep": "PEP-0719"},
    {"cycle": "3.8", "releaseDate": "2019-10-14", "eol": "2024-10-07", "latest": "3.8.20", "lts": False,
     "support": False},
    {"cycle": "2.7", "eol": True, "latest": "2.7.18", "lts": False},
    {"cycle": "weird"},
    "not a cycle",
]
NODE_API = [{"cycle": "26", "eol": "2029-04-30", "lts": "2026-10-28"}, {"cycle": "18", "eol": "2025-04-30", "lts": True}]


class RefreshTest(QuietCase):
    @classmethod
    def setUpClass(cls):
        cls.server = EolServer()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        self.server.routes.clear()
        self.server.requests.clear()
        net.configure(FakeSettings(proxy={"mode": "none", "url": ""}))
        self.addCleanup(net.configure, None)
        patch = mock.patch.object(badges, "EOL_API", f"http://127.0.0.1:{self.server.server_port}/api")
        patch.start()
        self.addCleanup(patch.stop)
        self.cache = FakeCache()
        self.server.table("python", PYTHON_API)
        self.server.table("nodejs", NODE_API)

    def apps(self, *names):
        return [make_status(name, "1.0") for name in names]

    def asked(self):
        return sorted(self.server.requests)

    def test_fetches_the_products_among_the_statuses(self):
        badges.refresh_eol(self.cache, self.apps("Python 3.8.10 (64-bit)", "Node.js", "Demo App"))
        self.assertEqual(self.asked(), ["/api/nodejs.json", "/api/python.json"])
        self.assertEqual(self.cache.entry("eol", "python")["cycles"],
                         [{"cycle": "3.13", "eol": "2029-10-31", "lts": False},
                          {"cycle": "3.8", "eol": "2024-10-07", "lts": False},
                          {"cycle": "2.7", "eol": True, "lts": False}])
        self.assertEqual(self.cache.entry("eol", "nodejs")["cycles"],
                         [{"cycle": "26", "eol": "2029-04-30", "lts": True},
                          {"cycle": "18", "eol": "2025-04-30", "lts": True}])

    def test_a_product_is_asked_for_once_however_many_apps_use_it(self):
        badges.refresh_eol(self.cache, self.apps("Microsoft .NET Runtime - 8.0.4 (x64)",
                                                 "Microsoft .NET SDK 8.0.202 (x64)", "Microsoft ASP.NET Core 8.0.4"))
        self.assertEqual(self.asked(), ["/api/dotnet.json"])

    def test_nothing_to_look_up_means_no_request(self):
        badges.refresh_eol(self.cache, self.apps("Demo App", "Other"))
        badges.refresh_eol(self.cache, [])
        self.assertEqual(self.server.requests, [])

    def test_fresh_tables_are_not_fetched_again_and_old_ones_are(self):
        statuses = self.apps("Python 3.8.10 (64-bit)")
        now = time.time()
        badges.refresh_eol(self.cache, statuses, now)
        badges.refresh_eol(self.cache, statuses, now + badges.EOL_TTL - 60)
        self.assertEqual(self.asked(), ["/api/python.json"])
        badges.refresh_eol(self.cache, statuses, now + badges.EOL_TTL + 60)
        self.assertEqual(self.asked(), ["/api/python.json", "/api/python.json"])

    def test_a_stale_table_stays_usable_when_the_refresh_fails(self):
        old = [cycle("3.8", "2024-10-07")]
        self.cache.data["eol"]["python"] = {"cycles": old, "ts": time.time() - 30 * 86400}
        self.server.table("python", {"oops": 1})
        badges.refresh_eol(self.cache, self.apps("Python 3.8.10 (64-bit)"))
        self.assertEqual(self.asked(), ["/api/python.json"])
        self.assertEqual(self.cache.entry("eol", "python")["cycles"], old)

    def test_an_unknown_product_is_remembered_for_a_week(self):
        statuses = self.apps("Redis")
        now = time.time()
        badges.refresh_eol(self.cache, statuses, now)
        self.assertEqual(self.cache.entry("eol", "redis")["cycles"], [])
        badges.refresh_eol(self.cache, statuses, now + 3600)
        self.assertEqual(self.asked(), ["/api/redis.json"])
        self.assertEqual(badges.compute(make_status("Redis", "5.0.14"), self.cache, today=TODAY), [])

    def test_a_server_error_or_bad_data_skips_that_product_only(self):
        self.server.table("php", [], 503)
        self.server.routes["/api/mariadb.json"] = (200, b"<html>not json</html>")
        self.server.table("mongodb", {"cycle": "7.0"})
        self.server.table("nginx", [])
        self.server.table("postgresql", [{"cycle": "16", "eol": "2028-11-09"}])
        badges.refresh_eol(self.cache, self.apps("PHP 8.3.4", "MariaDB 11.4 (x64)", "MongoDB 7.0.5 x", "nginx",
                                                 "PostgreSQL 16", "Python 3.8.10 (64-bit)"))
        self.assertEqual(len(self.server.requests), 6)
        self.assertEqual(sorted(self.cache.data["eol"]), ["postgresql", "python"])

    def test_an_unreachable_network_stops_after_the_first_failure(self):
        with mock.patch.object(badges.net, "urlopen", side_effect=urllib.error.URLError("no route")) as opened:
            badges.refresh_eol(self.cache, self.apps("Python 3.8.10 (64-bit)", "Node.js", "PHP 8.3.4"))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.cache.data["eol"], {})
        with mock.patch.object(badges.net, "urlopen", side_effect=TimeoutError("slow")) as opened:
            badges.refresh_eol(self.cache, self.apps("Python 3.8.10 (64-bit)", "Node.js"))
        self.assertEqual(opened.call_count, 1)

    def test_at_most_ten_requests_per_call(self):
        names = ["Python 3.8.10 (64-bit)", "Node.js", "PHP 8.3.4", "Microsoft .NET Runtime - 8.0.4 (x64)",
                 "PowerShell 7-x64", "Eclipse Temurin JDK with Hotspot 17.0.9+9 (x64)", "MySQL Server 8.0",
                 "MariaDB 11.4 (x64)", "PostgreSQL 16", "MongoDB 7.0.5 x", "Redis", "nginx",
                 "Mozilla Firefox (x64 en-US)", "Google Chrome"]
        for slug in ("php", "dotnet", "powershell", "eclipse-temurin", "mysql", "mariadb", "postgresql",
                     "mongodb", "redis", "nginx", "firefox", "chrome"):
            self.server.table(slug, [{"cycle": "1", "eol": False}])
        statuses = self.apps(*names)
        badges.refresh_eol(self.cache, statuses)
        self.assertEqual(len(self.server.requests), badges.MAX_REQUESTS)
        badges.refresh_eol(self.cache, statuses)
        self.assertEqual(len(self.server.requests), len(names))
        self.assertEqual(len(set(self.server.requests)), len(names))

    def test_missing_tables_go_before_stale_ones(self):
        now = time.time()
        self.cache.data["eol"]["python"] = {"cycles": [cycle("3.8", "2024-10-07")], "ts": now - 20 * 86400}
        with mock.patch.object(badges, "MAX_REQUESTS", 1):
            badges.refresh_eol(self.cache, self.apps("Python 3.8.10 (64-bit)", "Node.js"), now)
        self.assertEqual(self.server.requests, ["/api/nodejs.json"])
        with mock.patch.object(badges, "MAX_REQUESTS", 1):
            badges.refresh_eol(self.cache, self.apps("Python 3.8.10 (64-bit)", "Node.js"), now)
        self.assertEqual(self.server.requests, ["/api/nodejs.json", "/api/python.json"])

    def test_the_whole_chain_from_server_to_badge(self):
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10")
        badges.refresh_eol(self.cache, [status])
        [badge] = badges.compute(status, self.cache, today=TODAY)
        self.assertEqual((badge.kind, badge.values["cycle"], badge.values["date"]), ("eol", "3.8", "2024-10-07"))

    def test_never_raises(self):
        class Exploding:
            def entry(self, *args):
                raise RuntimeError("boom")

            def put(self, *args):
                raise RuntimeError("boom")

        badges.refresh_eol(Exploding(), self.apps("Python 3.8.10 (64-bit)"))
        badges.refresh_eol(self.cache, None)
        badges.refresh_eol(self.cache, [object(), None, 5])
        badges.refresh_eol(None, self.apps("Python 3.8.10 (64-bit)"))
        self.assertEqual(self.server.requests, [])

    def test_a_cache_that_cannot_store_does_not_break_the_scan(self):
        class NoStore(FakeCache):
            def put(self, *args):
                raise OSError("disk full")

        badges.refresh_eol(NoStore(), self.apps("Python 3.8.10 (64-bit)", "Node.js"))
        self.assertEqual(len(self.server.requests), 1)


class TranslationTest(QuietCase):
    def test_every_persian_entry_has_the_placeholders_of_its_template(self):
        for english, persian in badge_translations.FA.items():
            with self.subTest(english=english):
                self.assertEqual(self.fields(english), self.fields(persian))

    def test_every_template_a_badge_can_carry_has_a_persian_text(self):
        status = make_status("Python 3.8.10 (64-bit)", "3.8.10", offered="5.0.0", notes="CVE-2025-0001")
        cache = FakeCache(python=[cycle("3.8", "2024-10-07")])
        samples = badges.compute(status, cache, today=TODAY)
        samples += badges.compute(release("A security patch."), today=TODAY)
        samples += badges.compute(release("CVE-2024-1001 CVE-2024-1002 CVE-2024-1003 CVE-2024-1004"), today=TODAY)
        samples += badges.compute(make_status("Python 3.9.1", "3.9.1"), FakeCache(python=[cycle("3.9", iso(30))]),
                                  today=TODAY)
        samples += badges.compute(make_status("Python 3.9.1", "3.9.1"), FakeCache(python=[cycle("3.9", True)]),
                                  today=TODAY)
        templates = {t for b in samples for t in (b.text, b.tooltip)}
        self.assertEqual(len(templates), 11)
        self.assertEqual(templates, set(badge_translations.FA))

    def test_persian_rendering_fills_the_values(self):
        self.addCleanup(i18n.set_language, "en")
        [badge] = badges.compute(make_status("Python 3.8.10 (64-bit)", "3.8.10"),
                                 FakeCache(python=[cycle("3.8", "2024-10-07")]), today=TODAY)
        i18n.set_language("fa")
        shown = i18n.render(badge.tooltip, badge.values)
        self.assertIn("2024-10-07", shown)
        self.assertIn("Python", shown)
        self.assertNotIn("{", shown)
        self.assertEqual(i18n.render(badge.text, badge.values), badge_translations.FA["End of life"])

    @staticmethod
    def fields(template):
        return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


if __name__ == "__main__":
    unittest.main()
