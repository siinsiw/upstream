"""Discover: parsing `winget search` tables, ranking, the synthetic status that the update queue installs, and the
Discover page offscreen with a fake search. No real winget call and nothing is installed."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import threading
import time
import unicodedata
import unittest
from unittest import mock

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from upstream import discover, i18n, running, verify, winget
from upstream.context import CancelToken
from upstream.discover import NO_MATCH, NO_WINGET, Package, SearchError, search, status_for
from upstream.errors import Cancelled
from upstream.ui import discover as ui
from upstream.ui.locale import apply_language
from upstream.ui.theme import apply_theme

app = QApplication.instance() or QApplication([])

# The start of `winget search firefox` as winget prints it into a pipe: a spinner redraws the line, the Version
# and Match columns are one space apart, and long cells of the later rows are cut with an ellipsis.
FIREFOX = (b"\r   - \r   \\ \r   | \r   / \r                                                    \r"
           b"Name                          Id                          Version Match\r\n"
           b"----------------------------------------------------------------------------------\r\n"
           b"Mozilla Firefox (en-US)       Mozilla.Firefox             159.0   Moniker: firefox\r\n"
           b"Mozilla Firefox (MSIX)        Mozilla.Firefox.MSIX        159.0.1 Moniker: firefox\r\n"
           b"Mozilla Firefox (ach)         Mozilla.Firefox.ach         159.0   Moniker: firefox\r\n"
           b"Ablaze Floorp                 Ablaze.Floorp               12.20.\xe2\x80\xa6 Tag: firefox-fo\xe2\x80\xa6\r\n"
           b"r3dfox                        EclipseCommunity.r3dfox     152.0.\xe2\x80\xa6 Tag: firefox-fo\xe2\x80\xa6\r\n"
           b"Mozilla Flowstate Pinebuild   Mozilla.Flowstate           106.0a1 Tag: mozillafir\xe2\x80\xa6\r\n"
           b"<additional entries truncated due to result limit>\r\n")

# `winget search "visual studio code"`: four rows, three columns, the cells as wide as they need to be.
VSCODE = [
    "Name                                      Id                                      Version",
    "-----------------------------------------------------------------------------------------",
    "Microsoft Visual Studio Code              Microsoft.VisualStudioCode              1.143.1",
    "Microsoft Visual Studio Code CLI          Microsoft.VisualStudioCode.CLI          1.126.0",
    "Microsoft Visual Studio Code Insiders     Microsoft.VisualStudioCode.Insiders     1.127.0",
    "Microsoft Visual Studio Code Insiders CLI Microsoft.VisualStudioCode.Insiders.CLI 1.127.0",
]


def cells(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def render_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Lines laid out as winget does: every column as wide as its widest cell (a Chinese character takes two
    cells), one space between columns."""
    widths = [max(cells(row[i]) for row in [header, *rows]) for i in range(len(header))]

    def line(row):
        return " ".join(cell + " " * (w - cells(cell)) for cell, w in zip(row, widths)).rstrip()
    return [line(header), "-" * (sum(widths) + len(widths) - 1), *map(line, rows)]


PERSIAN_NAME = "".join(map(chr, (0x628, 0x631, 0x646, 0x627, 0x645, 0x647)))    # "program"; no Persian text in test files


def plain(text: str) -> str:
    """Without the bidi isolates that tr() wraps around values in a right-to-left UI."""
    return text.replace("⁨", "").replace("⁩", "")


def pkg(n: int, name: str = "", version: str = "1.0") -> Package:
    return Package(f"Pub{n}.App{n}", name or f"App {n}", version, f"Pub{n}")


class ParseTests(unittest.TestCase):
    def test_pipe_output_with_spinner_and_match_column(self):
        rows = discover._table(winget._lines(FIREFOX))
        self.assertEqual(rows[0], ("Mozilla Firefox (en-US)", "Mozilla.Firefox", "159.0"))
        self.assertEqual(rows[1], ("Mozilla Firefox (MSIX)", "Mozilla.Firefox.MSIX", "159.0.1"))
        self.assertEqual(rows[3], ("Ablaze Floorp", "Ablaze.Floorp", "12.20.…"))      # the cut version, as printed
        self.assertEqual(rows[5], ("Mozilla Flowstate Pinebuild", "Mozilla.Flowstate", "106.0a1"))
        self.assertEqual(len(rows), 6)      # the "<additional entries truncated ...>" note is not a package

    def test_three_columns_without_match(self):
        rows = discover._table(VSCODE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[3], ("Microsoft Visual Studio Code Insiders CLI",
                                   "Microsoft.VisualStudioCode.Insiders.CLI", "1.127.0"))

    def test_unicode_names_keep_their_columns(self):
        lines = render_table(["Name", "Id", "Version", "Match"], [
            ["115浏览器", "115.115Chrome", "36.0.1", "Tag: chrome"],
            ["360 极速浏览器X", "360.360Chrome.X", "23.1.1312.64", "Tag: chrome"],
            ["Café Müller 工具", "Cafe.Mueller", "2.0", "Tag: chrome"],
            ["Google Chrome", "Google.Chrome", "157.0.8103.39", "Moniker: chrome"],
        ])
        self.assertEqual(discover._table(lines), [
            ("115浏览器", "115.115Chrome", "36.0.1"), ("360 极速浏览器X", "360.360Chrome.X", "23.1.1312.64"),
            ("Café Müller 工具", "Cafe.Mueller", "2.0"), ("Google Chrome", "Google.Chrome", "157.0.8103.39")])

    def test_header_in_another_language(self):
        lines = render_table(["Имя", "Идентификатор", "Версия", "Совпадение"],
                             [["7-Zip", "7zip.7zip", "26.06", "Moniker: 7zip"]])
        self.assertEqual(discover._table(lines), [("7-Zip", "7zip.7zip", "26.06")])
        lines = render_table(["名称", "ID", "版本", "匹配"], [["微信", "Tencent.WeChat", "4.0.1", ""]])
        self.assertEqual(discover._table(lines), [("微信", "Tencent.WeChat", "4.0.1")])

    def test_not_a_table(self):
        self.assertEqual(discover._table(["No package found matching input criteria.", ""]), [])
        self.assertEqual(discover._table([]), [])
        self.assertEqual(discover._table(["Name", "-" * 40]), [])                 # one column: no Id, no Version

    def test_table_ends_at_the_first_blank_line(self):
        lines = [*VSCODE, "", "Some footer  Not.AnId  1.0"]
        self.assertEqual(len(discover._table(lines)), 4)

    def test_rows_that_are_not_packages_are_skipped(self):
        lines = render_table(["Name", "Id", "Version"], [
            ["Good", "Pub.Good", "1.0"], ["Spaced", "Not an id", "1.0"], ["NoDot", "Nodot", "1.0"], ["", "Pub.Anon", "1.0"]])
        self.assertEqual([r[1] for r in discover._table(lines)], ["Pub.Good"])


class RankTests(unittest.TestCase):
    ROWS = [
        ("Tag only", "Zed.Thing", "1"),
        ("Notepad Next", "dail8859.NotepadNext", "0.14"),
        ("Notepad++ Plugin", "Some.Plugin", "2"),
        ("Notepad++", "Notepad++.Notepad++", "8.9.9"),
        ("Notepad++", "notepad++.notepad++", "8.9.9"),            # the same id in another case: one result
        ("Super Editor", "Pub.Notepad", "3"),
    ]

    def test_exact_first_then_prefix_then_contains(self):
        ranked = discover._rank(self.ROWS, "notepad++")
        self.assertEqual(ranked[0][1], "Notepad++.Notepad++")
        self.assertEqual(ranked[1][1], "Some.Plugin")
        # the rest does not contain "notepad++" (it matched by a tag, or by "notepad" alone): winget's order stays
        self.assertEqual([r[1] for r in ranked[2:]], ["Zed.Thing", "dail8859.NotepadNext", "Pub.Notepad"])
        self.assertEqual(len(ranked), len(self.ROWS) - 1)

    def test_spaces_and_punctuation_do_not_hide_an_exact_match(self):
        rows = [("Other", "A.Other", "1"), ("Microsoft Visual Studio Code", "Microsoft.VisualStudioCode", "1.1")]
        self.assertEqual(discover._rank(rows, "visual studio code")[0][1], "Microsoft.VisualStudioCode")
        self.assertEqual(discover._rank(rows, "VisualStudioCode")[0][1], "Microsoft.VisualStudioCode")

    def test_per_language_packages_go_after_the_rest(self):
        rows = [("Mozilla Firefox (en-US)", "Mozilla.Firefox", "1")]
        rows += [(f"Mozilla Firefox ({code})", f"Mozilla.Firefox.{code}", "1") for code in ("ach", "af", "de-AT")]
        rows += [("Firefox Nightly", "Mozilla.Firefox.Nightly", "2"), ("Firefox ESR", "Mozilla.Firefox.ESR", "3")]
        ids = [r[1] for r in discover._rank(rows, "firefox")]
        self.assertEqual(ids[0], "Mozilla.Firefox")
        self.assertEqual(set(ids[1:3]), {"Mozilla.Firefox.Nightly", "Mozilla.Firefox.ESR"})
        self.assertEqual(ids[3:], ["Mozilla.Firefox.ach", "Mozilla.Firefox.af", "Mozilla.Firefox.de-AT"])


class SearchTests(unittest.TestCase):
    def run_search(self, lines, code=0, query="vs", limit=30):
        with mock.patch.object(discover, "_run", return_value=(code, lines)) as run:
            return search(query, limit), run

    def test_results_are_packages(self):
        found, run = self.run_search(VSCODE, query="visual studio code")
        self.assertEqual(found[0], Package("Microsoft.VisualStudioCode", "Microsoft Visual Studio Code", "1.143.1",
                                           "Microsoft", "winget"))
        self.assertEqual(len(found), 4)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["search", "visual studio code"])
        self.assertEqual(args[args.index("--count") + 1], str(discover.MAX_ROWS))

    def test_limit_and_duplicates(self):
        lines = render_table(["Name", "Id", "Version"],
                             [[f"App {i}", f"Pub.App{i % 7}", "1.0"] for i in range(20)])
        found, _ = self.run_search(lines, query="app", limit=5)
        self.assertEqual(len(found), 5)
        self.assertEqual(len({p.id for p in found}), 5)
        self.assertEqual(self.run_search(lines, query="app", limit=0)[0], [])

    def test_cells_winget_cut_short(self):
        lines = render_table(["Name", "Id", "Version", "Match"], [
            ["Whole", "Pub.Whole", "1.0", "Tag: x"], ["Long Name Cut Sh…", "Pub.Cut", "1.2.3.4…", "Tag: x"],
            ["Id Cut", "Pub.Id.That.Is.Very.Lo…", "2.0", "Tag: x"]])
        found, _ = self.run_search(lines, query="x")
        self.assertEqual([(p.name, p.id, p.version) for p in found],
                         [("Whole", "Pub.Whole", "1.0"), ("Long Name Cut Sh", "Pub.Cut", "")])   # the unusable id is left out

    def test_nothing_found_is_not_an_error(self):
        self.assertEqual(self.run_search(["No package found matching input criteria.", ""], code=NO_MATCH)[0], [])
        self.assertEqual(self.run_search([], code=0)[0], [])

    def test_failure_is_a_runtime_error_with_an_english_template(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_search(["", "Failed when opening source(s); try the 'source reset' command.", ""],
                            code=0x8A15000F)
        e = caught.exception
        self.assertIsInstance(e, SearchError)
        self.assertEqual(e.template, "winget couldn't search: {error}")
        self.assertIn("source reset", e.values["error"])
        self.assertEqual(str(e), "winget couldn't search: Failed when opening source(s); try the 'source reset' command.")
        with self.assertRaises(SearchError) as silent:
            self.run_search([], code=0x8A150068)
        self.assertEqual(silent.exception.values["error"], "0X8A150068")      # no output at all: the code is shown

    def test_query_is_cleaned_and_blank_queries_do_not_start_winget(self):
        self.assertEqual(discover._clean("  --accept-source-agreements  "), "accept-source-agreements")
        self.assertEqual(discover._clean("-x\r\n\t y\x00z"), "x y z")
        self.assertEqual(len(discover._clean("a" * 500)), 80)
        with mock.patch.object(discover, "_run") as run:
            self.assertEqual(search("   "), [])
            self.assertEqual(search("---"), [])
            run.assert_not_called()

    def test_missing_winget(self):
        with mock.patch.object(winget, "available", return_value=False), \
                mock.patch.object(discover.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError) as caught:
                search("vlc")
        popen.assert_not_called()
        self.assertEqual(caught.exception.template, NO_WINGET)
        self.assertEqual(str(caught.exception), "winget isn't installed on this PC.")


class FakeProc:
    """A winget process that answers after `after` seconds, or never."""

    def __init__(self, output: bytes = b"", code: int = 0, after: float = 0.0):
        self.output, self.returncode, self.killed = output, None, False
        self._code, self._ready = code, time.monotonic() + after

    def communicate(self, timeout=None):
        if self.killed:
            self.returncode = 1
            return b"", None
        if time.monotonic() < self._ready:
            time.sleep(min(timeout or 0, 0.01))
            raise subprocess.TimeoutExpired("winget", timeout)
        self.returncode = self._code
        return self.output, None

    def kill(self):
        self.killed = True


class RunTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(winget, "available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_command_line_only_searches(self):
        proc = FakeProc(FIREFOX)
        with mock.patch.object(discover.subprocess, "Popen", return_value=proc) as popen:
            found = search("firefox", 10)
        args = popen.call_args.args[0]
        self.assertEqual(args[:3], ["winget", "search", "firefox"])
        self.assertTrue({"--source", "winget", "--accept-source-agreements", "--disable-interactivity"} <= set(args))
        self.assertFalse({"install", "upgrade", "uninstall", "import"} & set(args))
        # the exact name and the other Firefox first, the per-language package after the tag matches
        self.assertEqual([p.id for p in found], ["Mozilla.Firefox", "Mozilla.Firefox.MSIX", "Ablaze.Floorp",
                                                 "EclipseCommunity.r3dfox", "Mozilla.Flowstate", "Mozilla.Firefox.ach"])
        self.assertEqual(found[2].version, "")      # "12.20.…" was cut by winget

    def test_exit_code_is_read_unsigned(self):
        proc = FakeProc(b"No package found matching input criteria.\r\n", code=-1978335212)   # 0x8A150014 as signed
        with mock.patch.object(discover.subprocess, "Popen", return_value=proc):
            self.assertEqual(search("zzzz"), [])

    def test_cancel_ends_the_process(self):
        proc, token = FakeProc(after=60), CancelToken()
        threading.Timer(0.2, token.cancel).start()
        with mock.patch.object(discover.subprocess, "Popen", return_value=proc):
            with self.assertRaises(Cancelled):
                search("firefox", cancel=token)
        self.assertTrue(proc.killed)

    def test_a_search_that_never_ends_times_out(self):
        proc = FakeProc(after=60)
        with mock.patch.object(discover.subprocess, "Popen", return_value=proc), mock.patch.object(discover, "TIMEOUT", 0.3):
            with self.assertRaises(SearchError) as caught:
                search("firefox")
        self.assertTrue(proc.killed)
        self.assertEqual(caught.exception.template, "winget took too long to answer.")

    def test_winget_that_cannot_start(self):
        with mock.patch.object(discover.subprocess, "Popen", side_effect=OSError("access denied")):
            with self.assertRaises(SearchError) as caught:
                search("firefox")
        self.assertEqual(caught.exception.values, {"error": "access denied"})


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.pkg = Package("Mozilla.Firefox", "Mozilla Firefox (en-US)", "159.0", "Mozilla")
        self.status = status_for(self.pkg)

    def test_synthetic_app(self):
        app = self.status.app
        self.assertEqual(app.reg_key, "WINGET\\NEW\\Mozilla.Firefox")
        self.assertEqual(self.status.uid, app.reg_key)
        self.assertEqual((app.name, app.version, app.publisher, app.scope), ("Mozilla Firefox (en-US)", "", "Mozilla", "user"))
        self.assertEqual(self.status.winget_id, "Mozilla.Firefox")

    def test_update_is_a_winget_install(self):
        upd = self.status.update
        self.assertEqual((upd.source, upd.version, upd.origin, upd.winget_mode), ("winget", "159.0", "Mozilla.Firefox", "install"))
        self.assertIsNone(upd.download_url)
        self.assertEqual(self.status.fallbacks, [])

    def test_two_packages_never_share_a_uid(self):
        self.assertNotEqual(status_for(pkg(1)).uid, status_for(pkg(2)).uid)

    def test_the_pipeline_copes_with_an_app_that_has_no_registry_entry(self):
        # read-only: the registry is read, nothing is run
        self.assertEqual(running.find_running(self.status.app), [])
        snap = verify.snapshot(self.status.app)
        self.assertEqual(snap.entries, [])
        verdict = verify.check(self.status.app, snap, mock.Mock(restart=False, present=False),
                               self.status.update.version)
        self.assertEqual((verdict.kind, verdict.version), ("unknown", "159.0"))


def wait_for(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class FakeSearch:
    """Stands in for discover.search: records every call; `results`, `error` or `gate` (an Event the call waits for,
    ignoring the cancel token like a slow winget would) decide what it does."""

    def __init__(self, results=None, error=None, gate: threading.Event | None = None):
        self.results, self.error, self.gate = results if results is not None else [], error, gate
        self.calls: list[tuple[str, int, CancelToken]] = []

    def __call__(self, query, limit, cancel=None):
        self.calls.append((query, limit, cancel))
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.results(query) if callable(self.results) else list(self.results)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apply_theme(app, "paper")

    def setUp(self):
        apply_language(app, "en")
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            page.close()
            page.deleteLater()
        app.processEvents()
        apply_language(app, "en")

    def make(self, search_fn=None, available=True, show=False) -> ui.DiscoverPage:
        page = ui.DiscoverPage(search_fn=search_fn or FakeSearch(), available_fn=lambda: available)
        page._debounce.setInterval(15)
        page.resize(960, 520)
        if show:
            page.show()
        self.pages.append(page)
        return page

    def type_and_search(self, page, text):
        page.search_box.setText(text)
        page._submit()

    def test_starts_with_a_hint_and_searches_nothing(self):
        fake = FakeSearch()
        page = self.make(fake)
        self.assertEqual(page._view, "hint")
        self.assertEqual(page.message_title.text(), "Find an app to install")
        self.assertIs(page.stack.currentWidget(), page.message)
        self.assertEqual(page.search_box.placeholderText(), "Search winget for an app to install")
        self.assertFalse(page.message_button.isVisibleTo(page))
        self.assertEqual(fake.calls, [])

    def test_enter_searches_and_lists_the_results(self):
        fake = FakeSearch([pkg(1, "Alpha"), pkg(2, "Beta", ""), pkg(3, "Gamma")])
        page = self.make(fake)
        self.type_and_search(page, "  al  pha ")
        self.assertTrue(page.busy.isVisibleTo(page) or page._searching)
        self.assertTrue(wait_for(lambda: page._view == "results"))
        self.assertEqual(fake.calls[0][:2], ("al pha", ui.LIMIT))
        self.assertIsInstance(fake.calls[0][2], CancelToken)
        self.assertIs(page.stack.currentWidget(), page.list)
        self.assertEqual(page.list.count(), 3)
        self.assertFalse(page.busy.isVisibleTo(page))
        self.assertEqual(page.summary.text(), "3 results")
        first = page.list.itemWidget(page.list.item(0))
        self.assertEqual((first.button.text(), first.version.text()), ("Install", "1.0"))
        self.assertFalse(page.list.itemWidget(page.list.item(1)).version.isVisibleTo(page))     # no version known

    def test_typing_waits_for_a_pause_and_short_queries_are_not_sent(self):
        fake = FakeSearch([pkg(1)])
        page = self.make(fake)
        page.search_box.setText("v")
        time.sleep(0.06)
        app.processEvents()
        self.assertEqual(fake.calls, [])
        page.search_box.setText("vl")
        page.search_box.setText("vlc")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        self.assertEqual([c[0] for c in fake.calls], ["vlc"])          # one search for the burst of keystrokes

    def test_clearing_the_box_drops_the_results_and_cancels_the_search(self):
        gate = threading.Event()
        fake = FakeSearch([pkg(1)], gate=gate)
        page = self.make(fake)
        self.type_and_search(page, "vlc")
        self.assertTrue(wait_for(lambda: fake.calls))
        token = fake.calls[0][2]
        page.search_box.clear()
        self.assertTrue(token.is_set)
        self.assertEqual(page._view, "hint")
        gate.set()
        time.sleep(0.05)
        app.processEvents()
        self.assertEqual(page._view, "hint")           # the late answer is thrown away
        self.assertEqual(page.list.count(), 0)

    def test_a_newer_search_replaces_a_stale_one(self):
        gate = threading.Event()
        order = []

        def answer(query):
            order.append(query)
            return [pkg(1, "Old")] if query == "first" else [pkg(2, "New")]

        fake = FakeSearch(answer, gate=gate)
        page = self.make(fake)
        self.type_and_search(page, "first")
        self.assertTrue(wait_for(lambda: len(fake.calls) == 1))
        self.type_and_search(page, "second")
        self.assertTrue(wait_for(lambda: len(fake.calls) == 2))
        self.assertTrue(fake.calls[0][2].is_set)
        self.assertFalse(fake.calls[1][2].is_set)
        gate.set()
        self.assertTrue(wait_for(lambda: page._view == "results"))
        time.sleep(0.05)
        app.processEvents()
        self.assertEqual([page.list.itemWidget(page.list.item(i)).pkg.name for i in range(page.list.count())], ["New"])

    def test_previous_results_stay_while_the_next_search_runs(self):
        gate = threading.Event()
        gate.set()
        fake = FakeSearch([pkg(1), pkg(2)], gate=gate)
        page = self.make(fake)
        self.type_and_search(page, "first")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        gate.clear()
        self.type_and_search(page, "second")
        self.assertEqual(page._view, "results")
        self.assertEqual(page.list.count(), 2)
        self.assertTrue(page.busy.isVisibleTo(page))
        gate.set()
        self.assertTrue(wait_for(lambda: not page._searching))

    def test_nothing_found(self):
        page = self.make(FakeSearch([]))
        self.type_and_search(page, "zzzz")
        self.assertTrue(wait_for(lambda: page._view == "empty"))
        self.assertEqual(page.message_title.text(), "No match in winget")
        self.assertIn("zzzz", page.message_text.text())
        self.assertIs(page.stack.currentWidget(), page.message)

    def test_a_failed_search_shows_the_reason_and_can_be_retried(self):
        fake = FakeSearch(error=SearchError("winget couldn't search: {error}", error="no network"))
        page = self.make(fake)
        with self.assertLogs("upstream.discover", "WARNING"):
            self.type_and_search(page, "vlc")
            self.assertTrue(wait_for(lambda: page._view == "error"))
        self.assertEqual(page.message_title.text(), "The search didn't work")
        self.assertEqual(page.message_text.text(), "winget couldn't search: no network")
        self.assertTrue(page.message_button.isVisibleTo(page))
        fake.error, fake.results = None, [pkg(1)]
        page.message_button.click()
        self.assertTrue(wait_for(lambda: page._view == "results"))
        self.assertEqual(len(fake.calls), 2)

    def test_an_unexpected_error_is_shown_too(self):
        page = self.make(FakeSearch(error=ValueError("boom")))
        with self.assertLogs("upstream.discover", "ERROR"):
            self.type_and_search(page, "vlc")
            self.assertTrue(wait_for(lambda: page._view == "error"))
        self.assertEqual(page.message_text.text(), "Something went wrong: boom")

    def test_winget_missing_from_the_start(self):
        fake = FakeSearch()
        page = self.make(fake, available=False)
        self.assertEqual(page._view, "nowinget")
        self.assertEqual(page.message_title.text(), "winget isn't available")
        self.assertFalse(page.search_box.isEnabled())
        self.assertTrue(page.message_button.isVisibleTo(page))
        page.message_button.click()
        self.assertEqual((page._view, fake.calls), ("nowinget", []))

    def test_winget_installed_meanwhile(self):
        state = {"ok": False}
        page = ui.DiscoverPage(search_fn=FakeSearch([pkg(1)]), available_fn=lambda: state["ok"])
        self.pages.append(page)
        self.assertEqual(page._view, "nowinget")
        state["ok"] = True
        page.message_button.click()
        self.assertEqual(page._view, "hint")
        self.assertTrue(page.search_box.isEnabled())

    def test_the_search_reports_winget_missing(self):
        page = self.make(FakeSearch(error=SearchError(NO_WINGET)))
        with self.assertLogs("upstream.discover", "WARNING"):
            self.type_and_search(page, "vlc")
            self.assertTrue(wait_for(lambda: page._view == "nowinget"))
        self.assertFalse(page.search_box.isEnabled())

    def test_a_full_page_says_so(self):
        page = self.make(FakeSearch([pkg(i) for i in range(ui.LIMIT)]))
        self.type_and_search(page, "app")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        self.assertEqual(page.summary.text(),
                         f"Showing the first {ui.LIMIT} results. Add more of the name to narrow them down.")

    def test_install_click_queues_the_row_and_asks_for_the_package(self):
        page = self.make(FakeSearch([pkg(1), pkg(2)]))
        asked = []
        page.install_requested.connect(asked.append)
        self.type_and_search(page, "app")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        row = page.list.itemWidget(page.list.item(1))
        row.button.click()
        self.assertEqual(asked, [pkg(2)])
        self.assertEqual(row.current, ("queued",))
        self.assertEqual(row.phase.full_text, "Queued")
        self.assertEqual(page.list.itemWidget(page.list.item(0)).current, ("idle",))
        row.button.click()                                  # the page is on the progress page now
        self.assertEqual(len(asked), 1)

    def test_state_api_and_cancel(self):
        page = self.make(FakeSearch([pkg(1), pkg(2)]))
        cancelled = []
        page.cancel_requested.connect(cancelled.append)
        self.type_and_search(page, "app")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        row = page.list.itemWidget(page.list.item(0))
        page.set_state("pub1.app1", "Downloading (winget)", 40, "cancel")       # the id's case does not matter
        self.assertEqual((row.phase.full_text, row.pct.text(), row.bar.value()), ("Downloading (winget)", "40%", 40))
        self.assertEqual(row.cancel_btn.text(), "Cancel")
        page.set_state("Pub1.App1", "Installing", -1, "wait")
        self.assertEqual((row.cancel_btn.text(), row.bar.maximum()), ("Stop waiting", 0))
        row._page_since = float("-inf")
        row.cancel_btn.click()
        self.assertEqual(cancelled, ["Pub1.App1"])
        page.set_cancelling("Pub1.App1")
        self.assertEqual(row.phase.full_text, "Cancelling…")
        self.assertFalse(row.cancel_btn.isVisibleTo(row))
        page.set_cancelled("Pub1.App1", "Cancelled by you.")
        self.assertEqual((row.button.text(), row.note.full_text, row.note.toolTip()), ("Install", "Cancelled", "Cancelled by you."))
        page.set_result("Pub1.App1", False, "winget exited with 5\nsee the log")
        self.assertEqual((row.button.text(), row.note.full_text), ("Retry", "winget exited with 5 see the log"))
        page.set_result("Pub1.App1", True, "Installed 1.0")
        self.assertEqual(row.state.text(), "Installed 1.0")
        self.assertEqual(row.actions.currentIndex(), 2)
        self.assertEqual(page.list.itemWidget(page.list.item(1)).current, ("idle",))     # the other row is untouched

    def test_installed_apps_show_installed(self):
        page = self.make(FakeSearch([pkg(1), pkg(2), pkg(3)]))
        page.set_installed_ids(["PUB1.app1"])
        self.type_and_search(page, "app")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        row1, row2 = (page.list.itemWidget(page.list.item(i)) for i in (0, 1))
        self.assertEqual((row1.state.text(), row1.actions.currentIndex()), ("Installed", 2))
        self.assertEqual(row2.button.text(), "Install")
        page.mark_installed("Pub2.App2")
        self.assertEqual(row2.state.text(), "Installed")
        page.set_installed_ids([])                              # the next scan no longer lists it
        self.assertEqual(row1.button.text(), "Install")
        self.assertEqual(row2.state.text(), "Installed")        # but this session installed it

    def test_an_install_in_progress_survives_a_new_search_and_a_language_switch(self):
        page = self.make(FakeSearch([pkg(1), pkg(2)]))
        self.type_and_search(page, "app")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        page.set_state("Pub1.App1", "Installing", 55)
        page.set_installed_ids(["Pub1.App1"])                   # the installed list says nothing about a running job
        self.assertEqual(page.list.itemWidget(page.list.item(0)).current[0], "progress")
        self.type_and_search(page, "app two")
        self.assertTrue(wait_for(lambda: len(page._rows) == 2 and not page._searching))
        row = page.list.itemWidget(page.list.item(0))
        self.assertEqual((row.phase.full_text, row.pct.text()), ("Installing", "55%"))
        apply_language(app, "fa")
        page.retranslate()
        row = page.list.itemWidget(page.list.item(0))
        self.assertEqual(row.phase.full_text, "Installing")            # the window gives the text again, translated
        self.assertTrue(plain(row.pct.text()).startswith(i18n.num(55)))
        self.assertNotIn("55", row.pct.text())

    def test_persian(self):
        apply_language(app, "fa")
        fa = i18n.FA
        page = self.make(FakeSearch([pkg(1, PERSIAN_NAME), pkg(2, "Telegram Desktop", "5.0.1")]), show=True)
        self.assertEqual(page.search_box.placeholderText(), fa["Search winget for an app to install"])
        self.assertEqual(page.message_title.text(), fa["Find an app to install"])
        self.type_and_search(page, PERSIAN_NAME)
        self.assertTrue(wait_for(lambda: page._view == "results"))
        self.assertEqual(plain(page.summary.text()), fa["{n} results"].format(n=i18n.num(2)))
        self.assertNotIn("2", page.summary.text())
        row = page.list.itemWidget(page.list.item(1))
        self.assertEqual(row.button.text(), fa["Install"])
        self.assertEqual(plain(row.button.accessibleName()), fa["Install {name}"].format(name="Telegram Desktop"))
        page.set_installed_ids(["Pub2.App2"])
        self.assertEqual(row.state.text(), fa["Installed"])
        # mirrored: the avatar is at the right edge, the action area at the left
        self.assertGreater(row.icon.mapTo(row, QPoint(0, 0)).x(), row.actions.mapTo(row, QPoint(0, 0)).x())
        apply_language(app, "en")
        page.retranslate()
        self.assertEqual(page.search_box.placeholderText(), "Search winget for an app to install")
        self.assertEqual(page.list.itemWidget(page.list.item(1)).state.text(), "Installed")
        self.assertEqual(page.summary.text(), "2 results")

    def test_every_page_text_has_a_persian_translation(self):
        page = self.make(FakeSearch(error=SearchError("winget couldn't search: {error}", error="x")))
        seen = []
        for view in ("hint", "searching", "empty", "error", "nowinget"):
            apply_language(app, "en")
            page._error = "x"
            page._set_view(view)
            english = (page.message_title.text(), page.message_text.text(), page.message_button.text())
            apply_language(app, "fa")
            page.retranslate()
            persian = (page.message_title.text(), page.message_text.text(), page.message_button.text())
            for en, fa in zip(english, persian):
                if en and en != "x":
                    self.assertNotEqual(en, fa, view)
                    seen.append(fa)
        self.assertEqual(len(seen), len(set(seen)) + 1)          # only "Try again" repeats (error and nowinget)

    def test_long_and_odd_names_fit_the_row(self):
        odd = [pkg(1, "A very long application name " * 8), pkg(2, "115浏览器 " * 6), pkg(3, "")]
        page = self.make(FakeSearch(odd), show=True)
        self.type_and_search(page, "name")
        self.assertTrue(wait_for(lambda: page._view == "results"))
        for i in range(3):
            row = page.list.itemWidget(page.list.item(i))
            self.assertLessEqual(row.width(), page.list.viewport().width())
            self.assertLessEqual(row.button.mapTo(row, QPoint(row.button.width(), 0)).x(), row.width())


if __name__ == "__main__":
    unittest.main()
