"""Export of the installed-app list: what each format holds, what it leaves out, and that a failed write never
damages an existing file. Files only go into temp folders."""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from upstream import export, i18n
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus

# Written as escapes: check_i18n.py keeps Persian letters out of code. Persian sample text for the encoding checks.
PERSIAN_NAME = "\u0648\u06cc\u0631\u0627\u06cc\u0634\u06af\u0631 \u0645\u062a\u0646 \xab\u0646\u0645\u0648\u0646\u0647\xbb"


def make_status(name, *, version="1.0", publisher="Acme", scope="machine", arch="x64", winget_id=None, repo=None,
                reg_key=None, system=False) -> AppStatus:
    app = InstalledApp(reg_key=reg_key or rf"HKLM64\{name}", name=name, version=version, publisher=publisher,
                       scope=scope, arch=arch, system_component=system)
    return AppStatus(app, repo=repo, winget_id=winget_id)


def sample() -> list[AppStatus]:
    return [
        make_status("Zeta Tool", version="3.1", winget_id="Zeta.Tool", repo="zeta/tool"),
        make_status("alpha editor", version="2.0.1", publisher="Alpha, Inc.", scope="user", arch="x86",
                    winget_id="Alpha.Editor"),
        make_status(PERSIAN_NAME, version="0.9", publisher="\u0646\u0627\u0634\u0631", reg_key=r"HKCU\persian", scope="user"),
        make_status("Hidden Runtime", system=True, winget_id="Contoso.Runtime"),
    ]


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.addCleanup(i18n.set_language, "en")

    def path(self, name="out"):
        return self.dir / name

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())


class JsonTests(ExportTestCase):
    def test_content(self):
        target = self.path("apps.json")
        self.assertEqual(export.export_apps(sample(), target, "json"), 3)
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(PERSIAN_NAME.encode("utf-8"), raw)          # written as UTF-8, not \u escapes
        doc = json.loads(raw.decode("utf-8"))
        self.assertEqual(doc["format"], "upstream-apps")
        self.assertEqual(doc["version"], 1)
        self.assertRegex(doc["exported"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertEqual([a["name"] for a in doc["apps"]], ["alpha editor", "Zeta Tool", PERSIAN_NAME])   # by name, case-blind
        self.assertEqual(doc["apps"][0], {"name": "alpha editor", "version": "2.0.1", "publisher": "Alpha, Inc.",
                                          "scope": "user", "arch": "x86", "winget_id": "Alpha.Editor",
                                          "github_repo": None})
        self.assertEqual(doc["apps"][1], {"name": "Zeta Tool", "version": "3.1", "publisher": "Acme", "scope": "machine",
                                          "arch": "x64", "winget_id": "Zeta.Tool", "github_repo": "zeta/tool"})
        self.assertIsNone(doc["apps"][2]["winget_id"])

    def test_no_system_components_and_no_paths(self):
        target = self.path("apps.json")
        app = make_status("Located", winget_id="Loc.App")
        app.app.install_location = r"C:\Users\someone\secret folder"
        app.app.uninstall_string = r"C:\x\unins000.exe"
        export.export_apps(sample() + [app], target, "json")
        text = target.read_text("utf-8")
        self.assertNotIn("Hidden Runtime", text)
        self.assertNotIn("secret folder", text)
        self.assertNotIn("unins000", text)

    def test_duplicate_entry_listed_once_but_two_installs_both(self):
        one = make_status("Twice", reg_key=r"HKLM64\Twice")
        again = make_status("Twice", reg_key=r"HKLM64\Twice")
        other_scope = make_status("Twice", scope="user", reg_key=r"HKCU\Twice")
        self.assertEqual(export.export_apps([one, again, other_scope], self.path("a.json"), "json"), 2)

    def test_empty_list(self):
        target = self.path("empty.json")
        self.assertEqual(export.export_apps([], target, "json"), 0)
        self.assertEqual(json.loads(target.read_text("utf-8"))["apps"], [])

    def test_not_installed_discover_entry_skipped(self):
        wish = make_status("Wanted", reg_key="WINGET\\NEW\\Pub.Wanted", scope="user", winget_id="Pub.Wanted")
        self.assertEqual(export.export_apps([wish], self.path("a.json"), "json"), 0)


class CsvTests(ExportTestCase):
    def read(self, target):
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "Excel needs the BOM to read UTF-8")
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
        return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))

    def test_header_rows_and_persian(self):
        target = self.path("apps.csv")
        self.assertEqual(export.export_apps(sample(), target, "csv"), 3)
        rows = self.read(target)
        self.assertEqual(rows[0], list(export.CSV_COLUMNS))
        self.assertEqual(rows[1], ["alpha editor", "2.0.1", "Alpha, Inc.", "user", "x86", "Alpha.Editor", ""])
        self.assertEqual(rows[2], ["Zeta Tool", "3.1", "Acme", "machine", "x64", "Zeta.Tool", "zeta/tool"])
        self.assertEqual(rows[3][0], PERSIAN_NAME)
        self.assertEqual(rows[3][2], "\u0646\u0627\u0634\u0631")
        self.assertEqual(len(rows), 4)

    def test_awkward_text_survives(self):
        odd = make_status('Say "hi", then\nleave', publisher="A;B")
        rows = self.read_after([odd])
        self.assertEqual(rows[1][0], 'Say "hi", then\nleave')
        self.assertEqual(rows[1][2], "A;B")

    def test_spreadsheet_formulas_are_defused(self):
        evil = make_status("=HYPERLINK(\"http://x\",\"y\")", publisher="@SUM(1)")
        rows = self.read_after([evil, make_status("-dash"), make_status("Plain 1.0")])
        names = {r[0] for r in rows[1:]}
        self.assertEqual(names, {"'=HYPERLINK(\"http://x\",\"y\")", "'-dash", "Plain 1.0"})
        self.assertIn("'@SUM(1)", [r[2] for r in rows[1:]])

    def read_after(self, statuses):
        target = self.path("t.csv")
        export.export_apps(statuses, target, "csv")
        return self.read(target)

    def test_empty_list_still_has_header(self):
        self.assertEqual(self.read_after([]), [list(export.CSV_COLUMNS)])


class WingetTests(ExportTestCase):
    def load(self, statuses):
        target = self.path("w.json")
        count = export.export_apps(statuses, target, "winget")
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        return count, json.loads(raw.decode("utf-8"))

    def check_schema(self, doc):
        """The required parts of packages.schema.2.0.json that `winget import` insists on."""
        self.assertEqual(doc["$schema"], "https://aka.ms/winget-packages.schema.2.0.json")
        self.assertRegex(doc["CreationDate"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")
        self.assertEqual(len(doc["Sources"]), 1)
        source = doc["Sources"][0]
        self.assertEqual(sorted(source["SourceDetails"]), ["Argument", "Identifier", "Name", "Type"])
        self.assertEqual(source["SourceDetails"]["Name"], "winget")
        for package in source["Packages"]:
            self.assertEqual(list(package), ["PackageIdentifier"])      # no Version: the new PC gets the latest

    def test_only_apps_with_a_winget_id(self):
        count, doc = self.load(sample())
        self.assertEqual(count, 2)
        self.check_schema(doc)
        self.assertEqual([p["PackageIdentifier"] for p in doc["Sources"][0]["Packages"]],
                         ["Alpha.Editor", "Zeta.Tool"])

    def test_ids_winget_cannot_import_are_left_out(self):
        statuses = [make_status("Arp", winget_id=r"ARP\Machine\X64\Thing"), make_status("Msix", winget_id=r"MSIX\A_1.0_x64__x"),
                    make_status("Store", winget_id="9NBLGGH4NNS1"), make_status("Cut", winget_id="Some.Long.Na…"),
                    make_status("Blank", winget_id="  "), make_status("Good", winget_id="Good.App")]
        count, doc = self.load(statuses)
        self.assertEqual(count, 1)
        self.assertEqual([p["PackageIdentifier"] for p in doc["Sources"][0]["Packages"]], ["Good.App"])
        self.assertEqual(export.export_report(statuses, "winget").left_out, ("Arp", "Blank", "Cut", "Msix", "Store"))

    def test_same_id_twice_is_one_package(self):
        statuses = [make_status("Runtime x64", winget_id="Vendor.Runtime"),
                    make_status("Runtime x86", winget_id="vendor.runtime", reg_key=r"HKLM32\Runtime")]
        count, doc = self.load(statuses)
        self.assertEqual(count, 1)
        self.assertEqual(len(doc["Sources"][0]["Packages"]), 1)

    def test_nothing_to_import_writes_no_file(self):
        # winget rejects "Packages": [] ("Array should contain no fewer than 1 elements"), so no such file is written.
        for statuses in ([make_status("Plain")], []):
            target = self.path("w.json")
            with self.subTest(apps=len(statuses)):
                self.assertFalse(export.export_report(statuses, "winget").can_write)
                with self.assertRaises(ValueError):
                    export.export_apps(statuses, target, "winget")
                self.assertEqual(self.leftovers(), [])

    def test_system_components_are_not_reported_as_left_out(self):
        report = export.export_report([make_status("Hidden", system=True), make_status("Plain")], "winget")
        self.assertEqual((report.written, report.left_out), (0, ("Plain",)))


class ReportTests(ExportTestCase):
    def test_report_matches_what_is_written(self):
        for fmt, written, left_out in (("json", 3, ()), ("csv", 3, ()), ("winget", 2, (PERSIAN_NAME,))):
            with self.subTest(fmt=fmt):
                report = export.export_report(sample(), fmt)
                self.assertEqual((report.fmt, report.written, report.left_out), (fmt, written, left_out))
                self.assertEqual(export.export_apps(sample(), self.path(f"r.{fmt}"), fmt), report.written)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_report_writes_nothing(self):
        export.export_report(sample(), "json")
        self.assertEqual(self.leftovers(), [])

    def test_summary_english(self):
        self.assertEqual(export.export_report(sample(), "json").summary(), "Saved a list of 3 apps.")
        one = export.ExportReport("winget", 1, ("A",))
        self.assertEqual(one.summary(), "Saved a list of 1 app. 1 app has no winget ID, so it was left out.")
        many = export.ExportReport("winget", 2, ("A", "B"))
        self.assertEqual(many.summary(), "Saved a list of 2 apps. 2 apps have no winget ID, so they were left out.")

    def test_summary_when_there_is_nothing_to_save(self):
        none = export.export_report([make_status("Plain")], "winget")
        self.assertFalse(none.can_write)
        self.assertEqual(none.summary(), "No app has a winget ID, so no file was saved.")
        for fmt in ("json", "csv"):
            self.assertTrue(export.export_report([], fmt).can_write)      # an empty list is still a valid file
        self.assertTrue(export.export_report(sample(), "winget").can_write)

    def test_summary_persian(self):
        i18n.set_language("fa")
        text = export.ExportReport("winget", 2, ("A", "B")).summary()
        self.assertIn("\u06f2", text)
        self.assertNotIn("Saved", text)
        self.assertNotIn("winget ID", text)


class NamingTests(ExportTestCase):
    def test_default_names(self):
        today = date.today().isoformat()
        self.assertEqual(export.default_name("json"), f"upstream-apps-{today}.json")
        self.assertEqual(export.default_name("csv"), f"upstream-apps-{today}.csv")
        self.assertEqual(export.default_name("winget"), f"upstream-winget-{today}.json")
        self.assertEqual(len({export.default_name(f) for f in export.FORMATS}), 3)

    def test_file_filters(self):
        self.assertEqual(export.file_filter("csv"), "Spreadsheet (CSV) (*.csv)")
        self.assertTrue(export.file_filter("winget").endswith("(*.json)"))
        i18n.set_language("fa")
        self.assertNotIn("Spreadsheet", export.file_filter("csv"))

    def test_labels_exist_for_every_format(self):
        self.assertEqual(set(export.FORMAT_LABELS), set(export.FORMATS))

    def test_unknown_format(self):
        target = self.path("x.txt")
        with self.assertRaises(ValueError):
            export.export_apps(sample(), target, "xml")
        with self.assertRaises(ValueError):
            export.default_name("xml")
        with self.assertRaises(ValueError):
            export.export_report(sample(), "xml")
        self.assertEqual(self.leftovers(), [])


class AtomicWriteTests(ExportTestCase):
    def test_replace_failure_keeps_the_old_file(self):
        target = self.path("keep.json")
        target.write_text("old list", "utf-8")
        with mock.patch.object(export.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                export.export_apps(sample(), target, "json")
        self.assertEqual(target.read_text("utf-8"), "old list")
        self.assertEqual(self.leftovers(), ["keep.json"])

    def test_write_failure_leaves_nothing_behind(self):
        target = self.path("new.csv")
        with mock.patch.object(export.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                export.export_apps(sample(), target, "csv")
        self.assertEqual(self.leftovers(), [])

    def test_missing_folder(self):
        with self.assertRaises(OSError):
            export.export_apps(sample(), self.dir / "no" / "such" / "dir.json", "json")

    def test_target_is_a_folder(self):
        target = self.path("folder")
        target.mkdir()
        with self.assertRaises(OSError):
            export.export_apps(sample(), target, "json")
        self.assertEqual(self.leftovers(), ["folder"])

    def test_overwrites_an_existing_file_and_accepts_str_paths(self):
        target = self.path("again.json")
        target.write_text("old", "utf-8")
        export.export_apps(sample(), str(target), "json")
        self.assertEqual(json.loads(target.read_text("utf-8"))["format"], "upstream-apps")
        self.assertEqual(self.leftovers(), ["again.json"])

    def test_temp_file_is_in_the_target_folder(self):
        seen = []
        real_replace = os.replace
        with mock.patch.object(export.os, "replace", side_effect=lambda a, b: (seen.append((a, b)), real_replace(a, b))):
            export.export_apps(sample(), self.path("one.json"), "json")
        (tmp, final), = seen
        self.assertEqual(Path(tmp).parent, self.dir)
        self.assertNotEqual(Path(tmp), Path(final))


if __name__ == "__main__":
    unittest.main()
