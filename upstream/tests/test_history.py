"""History: the JSON Lines store (order, corrupt lines, rotation, threads, switched off) and the History page
(rows, filter, search, clearing, paging, language, RTL, theme, keyboard). Every test runs against a temp folder."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from upstream import history, i18n
from upstream.ui import history as page_module, theme
from upstream.ui.history import HistoryPage, HistoryRow, KindGlyph, full_time, when_text
from upstream.ui.locale import apply_language
from upstream.ui.widgets import ElidedLabel

app = QApplication.instance() or QApplication([])

PERSIAN_NAME = "\u062a\u0644\u06af\u0631\u0627\u0645"   # a name that is not Latin, written as escapes (no Persian text outside i18n)


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for target, name in ((history, "DATA_DIR"), (theme, "DATA_DIR")):   # theme glyphs are cached in DATA_DIR too
            patch = mock.patch.object(target, name, self.dir)
            patch.start()
            self.addCleanup(patch.stop)
        history.configure(None)
        self.addCleanup(history.configure, None)
        self.addCleanup(self._tmp.cleanup)

    @property
    def file(self) -> Path:
        return self.dir / "history.jsonl"

    @property
    def old_file(self) -> Path:
        return self.dir / "history.1.jsonl"

    def write_lines(self, *lines: bytes | str, path: Path | None = None):
        path = path or self.file
        with open(path, "ab") as f:
            for line in lines:
                f.write(line if isinstance(line, bytes) else line.encode("utf-8"))
                f.write(b"\n")

    @staticmethod
    def line(ts: float, name: str = "App", kind: str = "update", **more) -> str:
        return json.dumps({"id": f"id{ts}", "ts": ts, "kind": kind, "name": name, **more})


class StoreTests(Base):
    def test_round_trip_keeps_every_field(self):
        history.record("update", PERSIAN_NAME, "1.0", "2.0", ok=False, template="Exited with code {code}",
                       values={"code": 1603, "path": Path("C:/x")}, source="github", ts=1_700_000_000)
        (entry,) = history.load()
        self.assertEqual((entry.kind, entry.name, entry.version_from, entry.version_to), ("update", PERSIAN_NAME, "1.0", "2.0"))
        self.assertEqual((entry.ok, entry.template, entry.source, entry.ts), (False, "Exited with code {code}", "github", 1_700_000_000))
        self.assertEqual(entry.values, {"code": 1603, "path": str(Path("C:/x"))})
        self.assertTrue(entry.id)

    def test_file_is_one_json_object_per_line(self):
        for i in range(3):
            history.record("install", f"App {i}", version_to="1.0")
        lines = self.file.read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(len(lines) - 1, 3)
        self.assertTrue(all(json.loads(line)["kind"] == "install" for line in lines[:-1]))

    def test_newest_first_and_limit(self):
        for ts, name in ((100, "old"), (300, "new"), (200, "mid")):
            history.record("update", name, ts=ts)
        self.assertEqual([e.name for e in history.load()], ["new", "mid", "old"])
        self.assertEqual([e.name for e in history.load(2)], ["new", "mid"])
        self.assertEqual(history.load(0), [])

    def test_same_time_keeps_write_order(self):
        for name in ("first", "second", "third"):
            history.record("update", name, ts=500)
        self.assertEqual([e.name for e in history.load()], ["third", "second", "first"])

    def test_entries_written_without_an_id_still_get_distinct_ones(self):
        self.write_lines('{"ts": 1, "kind": "update", "name": "a"}', '{"ts": 2, "kind": "update", "name": "a"}')
        ids = [e.id for e in history.load()]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(ids))

    def test_old_file_is_read_before_the_current_one(self):
        self.write_lines(self.line(1, "a"), self.line(2, "b"), path=self.old_file)
        self.write_lines(self.line(3, "c"))
        self.assertEqual([e.name for e in history.load()], ["c", "b", "a"])

    def test_corrupt_lines_are_skipped_and_the_rest_survives(self):
        self.write_lines(
            self.line(10, "good one"),
            "this is not json",
            '{"ts": "yesterday", "kind": "update", "name": "bad time"}',
            "[1, 2, 3]",
            '{"ts": 11, "kind": "update"}',                                    # no name
            '{"ts": true, "kind": "update", "name": "bool time"}',
            '{"ts": 12, "kind": "", "name": "no kind"}',
            b"\xff\xfe\xfd garbage",
            "",
            '{"ts": 13, "kind": "update", "name": "cut off',
            self.line(14, "good two", values=["not", "a", "dict"], ok="yes"),
        )
        self.write_lines(b"\x00\x00\x00" + self.line(15, "after NULs").encode())
        entries = history.load()
        self.assertEqual([e.name for e in entries], ["after NULs", "good two", "good one"])
        self.assertEqual(entries[1].values, {})     # a wrong type is dropped, the entry is kept
        self.assertTrue(entries[1].ok)

    def test_a_torn_last_line_does_not_swallow_the_next_record(self):
        self.write_lines(self.line(1, "before"))
        with open(self.file, "ab") as f:
            f.write(b'{"id": "x", "ts": 2, "kind": "upd')        # the process died in the middle of a write
        history._clean_file = None      # a new process: it has not written to this file yet
        history.record("update", "after", ts=3)
        self.assertEqual([e.name for e in history.load()], ["after", "before"])

    def test_nothing_is_recorded_while_history_is_off(self):
        settings = FakeSettings(history_enabled=False)
        history.configure(settings)
        history.record("update", "ignored")
        self.assertFalse(self.file.exists())
        self.assertEqual(history.load(), [])
        self.assertFalse(history.is_enabled())
        settings.values["history_enabled"] = True          # read live: no restart needed
        history.record("update", "kept")
        self.assertEqual([e.name for e in history.load()], ["kept"])

    def test_old_entries_stay_readable_while_off(self):
        history.record("update", "before")
        history.configure(FakeSettings(history_enabled=False))
        self.assertEqual([e.name for e in history.load()], ["before"])

    def test_a_settings_object_that_fails_counts_as_on(self):
        class Broken:
            def get(self, *_):
                raise RuntimeError("settings are gone")
        history.configure(Broken())
        history.record("update", "kept")
        self.assertEqual(len(history.load()), 1)

    def test_record_never_raises(self):
        blocker = self.dir / "a-file"
        blocker.write_text("x")
        with mock.patch.object(history, "DATA_DIR", blocker / "inside"), \
                self.assertLogs("upstream.history", "WARNING"):      # the folder cannot be created
            self.assertIsNone(history.record("update", "x"))
            self.assertEqual(history.load(), [])
            history.clear()
        history.record("update", None, None, None, True, None, {"a": object(), "n": float("nan"), 3: 1}, None, ts=float("inf"))
        (entry,) = history.load()
        self.assertEqual((entry.name, entry.template), ("", ""))
        self.assertEqual(set(entry.values), {"a", "n", "3"})
        self.assertTrue(entry.values["a"].startswith("<object"))
        self.assertLess(abs(entry.ts - time.time()), 60)     # a time that is not a time becomes "now"

    def test_text_that_is_not_valid_unicode_is_still_recorded(self):
        stray = chr(0xDCFF)     # what a file name with an undecodable byte becomes in Python
        history.record("update", f"Odd {stray} name", template="{p}", values={"p": "C:" + stray})
        (entry,) = history.load()
        self.assertTrue(entry.name.startswith("Odd "))
        self.assertTrue(entry.name.endswith(" name"))

    def test_long_texts_are_cut(self):
        history.record("update", "x" * 5000, template="y" * 5000, values={"error": "z" * 5000})
        (entry,) = history.load()
        self.assertEqual((len(entry.name), len(entry.template), len(entry.values["error"])), (history.MAX_TEXT,) * 3)

    def test_threads_never_interleave_lines(self):
        threads, per_thread = 8, 40
        start = threading.Barrier(threads)

        def work(t: int):
            start.wait()
            for i in range(per_thread):
                history.record("update", f"t{t}-{i}", "1", "2", template="padding " * 20, values={"t": t})

        workers = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        lines = [line for line in self.file.read_bytes().split(b"\n") if line]
        self.assertEqual(len(lines), threads * per_thread)
        for line in lines:
            json.loads(line)
        entries = history.load(10_000)
        self.assertEqual(len({e.name for e in entries}), threads * per_thread)
        self.assertEqual(len({e.id for e in entries}), threads * per_thread)

    def test_the_file_rotates_and_one_old_file_is_kept(self):
        with mock.patch.object(history, "MAX_BYTES", 1500):
            for i in range(80):
                history.record("update", f"App {i:03}", "1", "2", ts=1000 + i)
            self.assertTrue(self.old_file.exists())
            self.assertLess(self.file.stat().st_size, 1500 + 400)
            self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["history.1.jsonl", "history.jsonl"])
            names = [e.name for e in history.load(1000)]
        self.assertEqual(names[0], "App 079")
        self.assertEqual(names, sorted(names, reverse=True))     # newest first across both files
        self.assertEqual(names[:8], [f"App {i:03}" for i in range(79, 71, -1)])
        self.assertLess(len(names), 80)                          # the oldest entries were dropped with the old file

    def test_clear_removes_both_files_and_can_be_repeated(self):
        self.write_lines(self.line(1), path=self.old_file)
        history.record("update", "x")
        history.clear()
        self.assertEqual((history.load(), self.file.exists(), self.old_file.exists()), ([], False, False))
        history.clear()
        history.record("update", "again")
        self.assertEqual([e.name for e in history.load()], ["again"])


def rows_of(page: HistoryPage) -> list[HistoryRow]:
    return [page.list.itemWidget(page.list.item(i)) for i in range(page.list.count())]


def labels(row: HistoryRow, name: str) -> list[str]:
    return [w.full_text for w in row.findChildren(ElidedLabel) if w.objectName() == name]


class PageTests(Base):
    def setUp(self):
        super().setUp()
        apply_language(app, "en")
        theme.apply_theme(app, "paper")
        self.addCleanup(apply_language, app, "en")
        self.addCleanup(theme.apply_theme, app, "paper")
        patch = mock.patch.object(page_module, "SEARCH_DELAY_MS", 5)     # typing is answered at once in a test
        patch.start()
        self.addCleanup(patch.stop)

    def page(self) -> HistoryPage:
        page = HistoryPage()
        page.resize(1000, 600)
        self.addCleanup(self._dispose, page)
        return page

    @staticmethod
    def _dispose(page: HistoryPage):
        """Deleted now, not when the interpreter exits: Qt widgets that outlive the QApplication crash it."""
        page.close()
        page.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)

    def search_for(self, page: HistoryPage, text: str):
        page.search.setText(text)
        for _ in range(400):            # until the page's debounce timer has fired, however busy the machine is
            if not page._debounce.isActive():
                break
            QTest.qWait(5)

    def fill(self):
        now = time.time()
        history.record("update", "Visual Studio Code", "1.94", "1.95", source="github", ts=now - 100)
        history.record("install", "Notepad++", "", "8.8.3", source="winget", ts=now - 200)
        history.record("uninstall", "Old Printer Tools", "3.1.0", "", template="{name} was removed",
                       values={"name": "Old Printer Tools"}, ts=now - 300)
        history.record("update", "Git", "2.46", "2.51.0.windows.1", ok=False, source="github",
                       template="Installer exited with code {code}", values={"code": 1603}, ts=now - 400)
        history.record("cleanup", "Old Printer Tools", template="{n} items removed", values={"n": 14}, ts=now - 500)
        history.record("restore", "Old Printer Tools", template="{n} items restored", values={"n": 14}, ts=now - 600)
        history.record("rollback", "Firefox", "132.0", "131.0.3", source="winget", ts=now - 700)

    # -- states ----------------------------------------------------------
    def test_empty_state(self):
        page = self.page()
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty_title.text(), "No history yet")
        self.assertFalse(page.clear_btn.isEnabled())
        self.assertTrue(page.notice.isHidden())

    def test_history_off_says_so(self):
        history.configure(FakeSettings(history_enabled=False))
        page = self.page()
        self.assertEqual(page.empty_title.text(), "History is turned off")
        history.configure(None)
        history.record("update", "x")
        history.configure(FakeSettings(history_enabled=False))
        page.refresh()
        self.assertIs(page.stack.currentWidget(), page.list)
        self.assertFalse(page.notice.isHidden())     # the old entries are shown, with a note that nothing new is kept

    # -- rows ------------------------------------------------------------
    def test_rows_come_newest_first_with_their_details(self):
        self.fill()
        page = self.page()
        rows = rows_of(page)
        self.assertEqual([r.entry.name for r in rows], ["Visual Studio Code", "Notepad++", "Old Printer Tools", "Git",
                                                        "Old Printer Tools", "Old Printer Tools", "Firefox"])
        update, install, uninstall, failed, cleanup = rows[:5]
        self.assertEqual((labels(update, "oldVer"), labels(update, "newVer")), (["1.94"], ["1.95"]))
        self.assertEqual((labels(install, "oldVer"), labels(install, "newVer")), ([], ["8.8.3"]))
        self.assertEqual((labels(uninstall, "oldVer"), labels(uninstall, "newVer")), (["3.1.0"], []))
        self.assertEqual((labels(cleanup, "oldVer"), labels(cleanup, "newVer")), ([], []))
        self.assertEqual(update.result.text(), "Succeeded")
        self.assertEqual(update.kind.text(), "Update")
        self.assertEqual(install.kind.text(), "New install")
        self.assertEqual(uninstall.detail.full_text, "Old Printer Tools was removed")
        self.assertEqual(cleanup.detail.full_text, "14 items removed")
        self.assertEqual((failed.result.text(), failed.detail.full_text), ("Failed", "Installer exited with code 1603"))
        self.assertEqual((failed.result.property("tone"), update.result.property("tone")), ("danger", "muted"))
        self.assertEqual(labels(failed, "newVer"), [])     # a version it did not reach has no accent
        self.assertEqual(labels(failed, "oldVer"), ["2.46", "2.51.0.windows.1"])
        self.assertTrue(update.detail.isHidden())
        self.assertFalse(rows[0].when.full_text == "")

    def test_source_badge_only_for_known_sources(self):
        history.record("update", "A", source="github")
        history.record("update", "B", source="something else")
        history.record("update", "C")
        rows = {r.entry.name: r for r in rows_of(self.page())}
        self.assertEqual(rows["A"].findChild(type(rows["A"].kind), "badge").text(), "GitHub")
        self.assertIsNone(rows["B"].findChild(type(rows["B"].kind), "badge"))
        self.assertIsNone(rows["C"].findChild(type(rows["C"].kind), "badge"))

    def test_odd_entries_do_not_break_the_page(self):
        self.write_lines(self.line(time.time(), "from the future", kind="teleport", template="{", values={}),
                         self.line(time.time() - 5, "missing value", template="Needs {thing}"))
        rows = rows_of(self.page())
        self.assertEqual([r.kind.text() for r in rows], ["teleport", "Update"])
        self.assertEqual(rows[0].detail.full_text, "{")                     # a template that cannot be filled shows as it is
        self.assertEqual(rows[1].detail.full_text, "Needs {thing}")

    def test_message_is_rendered_when_it_is_shown(self):
        template = "Installer exited with code {code}"
        self.assertIn(template, i18n.FA)
        history.record("update", "Git", "2.46", "2.51", ok=False, template=template, values={"code": 1603})
        page = self.page()
        self.assertEqual(rows_of(page)[0].detail.full_text, "Installer exited with code 1603")
        apply_language(app, "fa")
        page.retranslate()
        row = rows_of(page)[0]
        self.assertEqual(row.detail.full_text, i18n.render(template, {"code": 1603}))
        self.assertNotEqual(row.detail.full_text, "Installer exited with code 1603")
        self.assertEqual(row.result.text(), i18n.FA["Failed"])
        self.assertEqual(row.kind.text(), i18n.FA["Update"])
        self.assertEqual(page.search.placeholderText(), i18n.FA["Search history"])
        apply_language(app, "en")
        page.retranslate()
        self.assertEqual(rows_of(page)[0].detail.full_text, "Installer exited with code 1603")

    def test_every_text_of_the_page_has_a_persian_entry(self):
        for text in (*page_module.KIND_LABELS.values(), *(label for _, label in page_module.FILTERS)):
            self.assertIn(text, i18n.FA, text)
        apply_language(app, "fa")
        page = self.page()
        self.assertEqual([page.kind_filter.itemText(i) for i in range(page.kind_filter.count())],
                         [i18n.FA[label] for _, label in page_module.FILTERS])
        self.assertEqual(page.clear_btn.text(), i18n.FA["Clear history…"])
        self.assertEqual(page.empty_title.text(), i18n.FA["No history yet"])

    def test_every_kind_has_a_label_a_filter_and_a_drawing(self):
        fallback = page_module._glyph_path("something else").boundingRect()
        for kind in history.KINDS:
            self.assertIn(kind, page_module.KIND_LABELS)
            self.assertIn(kind, [key for key, _ in page_module.FILTERS])
            self.assertNotEqual(page_module._glyph_path(kind).boundingRect(), fallback, kind)
        self.assertEqual(len(history.KINDS), len(page_module.KIND_LABELS))

    def test_time_and_tooltips(self):
        ts = datetime(2026, 11, 3, 14, 32, 5).timestamp()
        history.record("update", "App", "1", "2", template="Done in {n} s", values={"n": 3}, ts=ts)
        (row,) = rows_of(self.page())
        self.assertEqual(row.when.toolTip(), "2026-11-03 14:32:05")
        self.assertIn("2026-11-03 14:32:05", row.toolTip())
        self.assertIn("Done in 3 s", row.toolTip())
        for part in ("Update", "App", "1 → 2", "Succeeded", "Done in 3 s"):
            self.assertIn(part, row.accessibleName())

    def test_when_text_in_both_languages(self):
        now = datetime(2026, 11, 8, 12, 0, 0).timestamp()
        ago = lambda **delta: when_text(now - (delta.get("s", 0) + 60 * delta.get("m", 0) + 3600 * delta.get("h", 0)
                                              + 86400 * delta.get("d", 0)), now)
        cases = [(ago(s=30), "Just now"), (ago(s=-90), "Just now"), (ago(m=1), "1 minute ago"), (ago(m=59, s=59), "59 minutes ago"),
                 (ago(h=1), "1 hour ago"), (ago(h=11, m=59), "11 hours ago"), (ago(h=30), "Yesterday"),
                 (ago(d=2, h=1), "2 days ago"), (ago(d=6), "6 days ago"), (ago(d=8), "2026-10-31"), (ago(d=400), "2025-10-04")]
        for got, want in cases:
            self.assertEqual(got, want)
        apply_language(app, "fa")
        self.assertEqual(ago(m=5), i18n.tr("{n} minutes ago", n=i18n.num(5)))
        self.assertIn("\u06f5", ago(m=5))                    # Persian digits
        self.assertEqual(ago(h=30), i18n.FA["Yesterday"])
        self.assertTrue(full_time(now).replace("\u2068", "").replace("\u2069", "").startswith("\u06f2\u06f0\u06f2\u06f6"))

    # -- filter and search -----------------------------------------------
    def test_kind_filter(self):
        self.fill()
        page = self.page()
        expected = {"": 7, "install": 1, "update": 2, "rollback": 1, "uninstall": 1, "cleanup": 1, "restore": 1}
        for key, count in expected.items():
            page.kind_filter.setCurrentIndex(page.kind_filter.findData(key))
            self.assertEqual(page.list.count(), count, key)
        self.assertEqual(page.kind_filter.count(), len(expected))
        page.kind_filter.setCurrentIndex(page.kind_filter.findData("update"))
        self.assertEqual({r.entry.kind for r in rows_of(page)}, {"update"})

    def test_search_matches_name_message_version_kind_and_source(self):
        self.fill()
        page = self.page()
        for text, names in (("firefox", ["Firefox"]), ("PRINTER", ["Old Printer Tools"] * 3), ("code 1603", ["Git"]), ("131.0", ["Firefox"]),
                            ("windows.1", ["Git"]), ("leftover", ["Old Printer Tools"]), ("undone", ["Old Printer Tools"]),
                            ("  notepad  ", ["Notepad++"]), ("git", ["Visual Studio Code", "Git"])):    # "git" is also the source GitHub
            self.search_for(page, text)
            self.assertEqual([r.entry.name for r in rows_of(page)], names, text)
        self.search_for(page, "no such thing")
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty_title.text(), "Nothing matches")
        self.search_for(page, "")
        self.assertEqual(page.list.count(), 7)

    def test_search_and_kind_filter_combine(self):
        self.fill()
        page = self.page()
        page.kind_filter.setCurrentIndex(page.kind_filter.findData("update"))
        self.search_for(page, "2.51")
        self.assertEqual([r.entry.name for r in rows_of(page)], ["Git"])
        self.search_for(page, "printer")
        self.assertIs(page.stack.currentWidget(), page.empty)

    # -- clearing --------------------------------------------------------
    def test_clear_history_asks_first(self):
        self.fill()
        page = self.page()
        cleared = []
        page.cleared.connect(lambda: cleared.append(True))
        with mock.patch.object(page, "_confirm_clear", return_value=False):
            page.clear_btn.click()
        self.assertEqual((page.list.count(), len(history.load()), cleared), (7, 7, []))
        with mock.patch.object(page, "_confirm_clear", return_value=True):
            page.clear_btn.click()
        self.assertEqual((page.list.count(), history.load(), cleared), (0, [], [True]))
        self.assertEqual(page.empty_title.text(), "No history yet")
        self.assertFalse(page.clear_btn.isEnabled())

    def test_confirmation_defaults_to_keeping_the_history(self):
        page = self.page()
        with mock.patch.object(page_module.QMessageBox, "exec") as run, \
                mock.patch.object(page_module.QMessageBox, "clickedButton", return_value=None):
            self.assertFalse(page._confirm_clear())
            run.assert_called_once()

    # -- paging and refresh ----------------------------------------------
    def test_older_entries_come_in_pages(self):
        base = time.time() - 10_000
        self.write_lines(*(self.line(base + i, f"App {i:03}") for i in range(250)))
        page = self.page()
        page_size = page_module.PAGE
        self.assertEqual(page.list.count(), page_size)
        self.assertFalse(page.more_btn.isHidden())
        self.assertIn(str(250 - page_size), page.more_btn.text())
        self.assertEqual(rows_of(page)[0].entry.name, "App 249")
        while not page.more_btn.isHidden():
            before = rows_of(page)
            page.more_btn.click()
            self.assertEqual(rows_of(page)[:len(before)], before)       # the rows already there are kept, not rebuilt
            self.assertEqual(page.list.count(), min(250, len(before) + page_size))
        self.assertEqual([r.entry.name for r in rows_of(page)], [f"App {i:03}" for i in range(249, -1, -1)])
        self.search_for(page, "App 00")      # a new search starts from the first page again
        self.assertEqual(page.list.count(), 10)

    def test_refresh_rebuilds_only_when_something_changed(self):
        history.record("update", "first")
        page = self.page()
        rows_of(page)[0].marker = True
        page.refresh()
        self.assertTrue(getattr(rows_of(page)[0], "marker", False))
        history.record("update", "second")
        page.refresh()
        self.assertEqual([r.entry.name for r in rows_of(page)], ["second", "first"])
        self.assertFalse(hasattr(rows_of(page)[1], "marker"))

    def test_open_page_picks_up_new_entries_by_itself(self):
        page = self.page()
        page.show()
        history.record("update", "arrived later")
        page._tick.timeout.emit()      # the page's minute timer
        self.assertEqual([r.entry.name for r in rows_of(page)], ["arrived later"])

    def test_showing_the_page_reloads_it(self):
        page = self.page()
        history.record("update", "written while hidden")
        page.show()
        self.assertEqual([r.entry.name for r in rows_of(page)], ["written while hidden"])

    # -- layout, theme, keyboard -----------------------------------------
    def test_rtl_mirrors_the_row(self):
        history.record("update", "App", "1", "2", source="github")
        xs = {}
        for lang in ("en", "fa"):
            apply_language(app, lang)
            page = self.page()
            page.retranslate()
            page.show()
            app.processEvents()
            row = rows_of(page)[0]
            self.assertEqual(page.layoutDirection(), Qt.RightToLeft if lang == "fa" else Qt.LeftToRight)
            xs[lang] = (row.findChild(KindGlyph).x(), row.name.x(), row.when.x(), row.width())
            self.assertGreaterEqual(row.when.x() + row.when.width(), 0)
            self.assertLessEqual(row.when.x() + row.when.width(), row.width())     # nothing hangs out of the row
            arrow = row.findChild(type(row.kind), "arrow")
            self.assertEqual(arrow.text(), "\u2190" if lang == "fa" else "\u2192")
        glyph, name, when, width = xs["en"]
        self.assertTrue(glyph < name < when)
        glyph, name, when, width = xs["fa"]
        self.assertTrue(when < name < glyph)

    def test_narrow_window_keeps_every_column_inside_the_row(self):
        history.record("update", "A rather long application name", "2024.03.1", "N-118522-g7a4cdf1b2f-20241020", ok=False,
                       template="A long explanation of why the installer did not finish this time", source="winget")
        page = self.page()
        page.resize(700, 400)
        page.show()
        app.processEvents()
        row = rows_of(page)[0]
        for child in (row.name, row.versions, row.result, row.when):
            self.assertGreaterEqual(child.x(), 0)
            self.assertLessEqual(child.x() + child.width(), row.width(), child.objectName() or type(child).__name__)

    def test_glyph_colours_follow_the_theme(self):
        seen = set()
        for key in ("paper", "graphite", "contrast"):
            theme.apply_theme(app, key)
            t = theme.THEMES[key]
            for kind, stroke in (("update", t["accent"]), ("uninstall", t["danger"]), ("rollback", t["text"])):
                glyph = KindGlyph(kind)
                self.assertEqual(glyph.colours()[1], QColor(stroke), (key, kind))
                seen.add(glyph.grab().toImage().pixelColor(18, 18).name())
        self.assertGreater(len(seen), 3)

    def test_theme_switch_repaints_a_visible_row(self):
        history.record("update", "App", "1", "2")
        page = self.page()
        page.show()
        app.processEvents()
        glyph = rows_of(page)[0].findChild(KindGlyph)
        before = glyph.grab().toImage()
        theme.apply_theme(app, "graphite")
        app.processEvents()
        self.assertNotEqual(glyph.grab().toImage(), before)

    def test_keyboard_moves_through_the_rows(self):
        self.fill()
        page = self.page()
        page.show()
        page.activateWindow()
        app.processEvents()
        page.list.setFocus()
        app.processEvents()
        QTest.keyClick(page.list, Qt.Key_Down)
        QTest.keyClick(page.list, Qt.Key_Down)
        rows = rows_of(page)
        current = page.list.currentRow()
        self.assertGreaterEqual(current, 1)
        self.assertEqual([i for i, r in enumerate(rows) if r._marked], [current])
        QTest.keyClick(page.list, Qt.Key_End)
        self.assertEqual(page.list.currentRow(), 6)
        QTest.keyClick(page.list, Qt.Key_Home)
        self.assertEqual(page.list.currentRow(), 0)
        page.search.setFocus()
        app.processEvents()
        self.assertFalse(any(r._marked for r in rows_of(page)))      # the mark goes with the focus

    def test_search_shortcuts(self):
        self.fill()
        page = self.page()
        page.show()
        page.activateWindow()
        page.list.setFocus()
        app.processEvents()
        QTest.keySequence(page.list, QKeySequence(QKeySequence.Find))
        self.assertTrue(page.search.hasFocus())
        page.search.setText("abc")
        QTest.keyClick(page.search, Qt.Key_Escape)
        self.assertEqual(page.search.text(), "")

    def test_tab_order_visits_every_control(self):
        history.record("update", "x")
        page = self.page()
        page.show()
        mine = (page.search, page.kind_filter, page.clear_btn, page.list, page.more_btn)
        chain, widget = [], page.search
        for _ in range(40):         # the focus chain of the page's window, in order, starting at the search box
            if widget in mine and widget not in chain:
                chain.append(widget)
            widget = widget.nextInFocusChain()
        self.assertEqual(chain, list(mine))
        for widget in chain:
            self.assertTrue(widget.focusPolicy() & Qt.TabFocus)


if __name__ == "__main__":
    unittest.main()
