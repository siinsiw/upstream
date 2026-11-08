"""How many MB an offered update downloads: the size lookup (HEAD, then a one-byte ranged GET) against a local server,
the sizes the scanner puts on GitHub, winget and site updates, what is cached and when it is asked again, the time
budget, and the scan snapshot. Offline: only 127.0.0.1."""
from __future__ import annotations

import http.server
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from upstream import i18n, net, scanner, site, winget
from upstream.scanner import AppStatus, Scanner, Update, load_snapshot, save_snapshot

from . import test_snapshot
from .test_pins import FakeCache, FakeSettings, api_release, github_with, make_app

MB = 1024 * 1024
PAGE = b"x" * 20_000


class Origin(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), OriginHandler)
        self.requests: list[tuple[str, str, str | None]] = []   # (method, path, Range)

    def handle_error(self, request, client_address):
        pass    # the client closes the connection once it has the headers


class OriginHandler(http.server.BaseHTTPRequestHandler):
    """/head answers HEAD with a length; /nohead refuses HEAD but honours Range; /norange refuses HEAD and ignores Range;
    /nolength answers HEAD without a length; /page is an HTML page; /tiny is 300 bytes; /redirect goes to /head;
    anything else is a 404."""

    def log_message(self, *args):
        pass

    def _note(self):
        self.server.requests.append((self.command, self.path, self.headers.get("Range")))

    def _headers(self, status, length=None, ctype="application/octet-stream", **extra):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in extra.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()

    def do_HEAD(self):
        self._note()
        path = self.path
        if path == "/head":
            self._headers(200, 5 * MB)
        elif path == "/redirect":
            self._headers(302, 0, Location="/head")
        elif path == "/nolength":
            self._headers(200)
        elif path == "/page":
            self._headers(200, 50_000, "text/html; charset=utf-8")
        elif path == "/tiny":
            self._headers(200, 300)
        elif path in ("/nohead", "/norange"):
            self._headers(405, 0)
        else:
            self._headers(404, 0)

    def do_GET(self):
        self._note()
        wanted = self.headers.get("Range")
        if self.path in ("/nohead", "/nolength") and wanted == "bytes=0-0":
            self._headers(206, 1, Content_Range=f"bytes 0-0/{7 * MB}")
            self.wfile.write(b"M")
        elif self.path == "/norange":
            self._headers(200, len(PAGE))
            self.wfile.write(PAGE)
        elif self.path == "/head":     # where /redirect leads: a GET here means the redirect turned a HEAD into a download
            self._headers(200, 5 * MB)
            self.wfile.write(b"MZ")
        else:
            self._headers(404, 0)


ORIGIN = Origin()
threading.Thread(target=ORIGIN.serve_forever, daemon=True).start()


def tearDownModule():
    ORIGIN.shutdown()
    ORIGIN.server_close()


def url(path):
    return f"http://127.0.0.1:{ORIGIN.server_port}{path}"


class Direct:
    """Settings that send requests straight out: a system proxy must not get between the test and 127.0.0.1."""

    def get(self, key, default=None):
        return {"mode": "none", "url": ""} if key == "proxy" else default


class RemoteSizeTest(unittest.TestCase):
    def setUp(self):
        net.configure(Direct())
        self.addCleanup(net.configure, None)
        ORIGIN.requests.clear()

    def methods(self):
        return [m for m, _, _ in ORIGIN.requests]

    def test_a_head_that_gives_a_length_is_enough(self):
        self.assertEqual(winget.remote_size(url("/head")), 5 * MB)
        self.assertEqual(self.methods(), ["HEAD"])

    def test_a_server_that_refuses_head_is_asked_for_one_byte(self):
        self.assertEqual(winget.remote_size(url("/nohead")), 7 * MB)
        self.assertEqual(ORIGIN.requests, [("HEAD", "/nohead", None), ("GET", "/nohead", "bytes=0-0")])

    def test_a_head_without_a_length_falls_back_to_the_ranged_get(self):
        self.assertEqual(winget.remote_size(url("/nolength")), 7 * MB)
        self.assertEqual(self.methods(), ["HEAD", "GET"])

    def test_a_server_that_ignores_range_still_gives_the_whole_length(self):
        self.assertEqual(winget.remote_size(url("/norange")), len(PAGE))

    def test_redirects_are_followed(self):
        self.assertEqual(winget.remote_size(url("/redirect")), 5 * MB)

    def test_a_redirect_keeps_the_head_so_a_size_lookup_never_starts_a_download(self):
        winget.remote_size(url("/redirect"))
        self.assertEqual(ORIGIN.requests, [("HEAD", "/redirect", None), ("HEAD", "/head", None)])

    def test_a_plain_http_address_is_not_asked_but_https_and_loopback_are(self):
        asked = []

        def refuse(req, timeout=0):
            asked.append(req.full_url)
            raise OSError("no network in tests")

        with mock.patch.object(winget.net, "urlopen", refuse):
            self.assertEqual(winget.remote_size("http://download.example.test/x.exe"), 0)
            self.assertEqual(winget.remote_size("ftp://download.example.test/x.exe"), 0)
            self.assertEqual(asked, [])
            with self.assertRaises(OSError):
                winget.remote_size("https://download.example.test/x.exe")
            with self.assertRaises(OSError):
                winget.remote_size("http://127.0.0.1:9/x.exe")
            with self.assertRaises(OSError):
                winget.remote_size("http://localhost:9/x.exe")
        self.assertEqual(asked, ["https://download.example.test/x.exe", "http://127.0.0.1:9/x.exe",
                                 "http://localhost:9/x.exe"])

    def test_a_web_page_is_not_an_installer(self):
        self.assertEqual(winget.remote_size(url("/page")), 0)

    def test_an_error_page_sized_answer_is_unknown(self):
        self.assertEqual(winget.remote_size(url("/tiny")), 0)

    def test_missing_files_have_no_size(self):
        self.assertEqual(winget.remote_size(url("/missing")), 0)

    def test_a_malformed_address_has_no_size(self):
        self.assertEqual(winget.remote_size("not a url"), 0)

    def test_no_server_raises_so_the_caller_can_tell_it_from_no_size(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(OSError):
            winget.remote_size(f"http://127.0.0.1:{port}/x.exe", timeout=1)


def github_asset_release(tag, size):
    release = api_release(tag)
    release["assets"][0]["size"] = size
    return release


def winget_meta(version="2.0", url="https://vendor.example/Demo-2.0.exe", **extra):
    return {"v": winget.META_VERSION, "version": version, "installer_url": url, "urls": [], "homepage": None, **extra}


class ScannerSizeTest(unittest.TestCase):
    def setUp(self):
        self.asked: list[str] = []
        self.answers: dict[str, object] = {}

        def remote_size(url, timeout=winget.SIZE_TIMEOUT):
            self.asked.append(url)
            answer = self.answers.get(url, 12 * MB)
            if isinstance(answer, Exception):
                raise answer
            return answer

        patch = mock.patch.object(winget, "remote_size", remote_size)
        patch.start()
        self.addCleanup(patch.stop)

    def scanner(self, releases=None, overrides=None, meta=None, **settings):
        sc = Scanner(FakeSettings(settings, overrides=overrides or {}), FakeCache())
        if releases is not None:
            sc._gh = github_with({"o/demo": releases})
        else:
            sc._gh = github_with({})
            sc._gh.discover = lambda *a, **k: None
        for pkg_id, value in (meta or {}).items():
            sc.cache.put("winget_meta", pkg_id, value)
        return sc

    def resolve(self, sc, version="1.0", available="", winget_id=False):
        status = AppStatus(make_app(version=version), winget_id="Demo.App" if available or winget_id else None,
                           winget_available=available)
        sc.resolve(status, False)
        return status

    def test_a_github_update_carries_the_size_of_its_asset(self):
        sc = self.scanner([github_asset_release("v2.0.0", 45_000_000)], overrides={"demo": {"github": "o/demo"}})
        status = self.resolve(sc)
        self.assertEqual((status.update.source, status.update.size), ("github", 45_000_000))
        self.assertEqual(self.asked, [], "a size GitHub already reported is not asked for again")

    def test_the_newest_release_of_a_pinned_line_carries_its_own_size(self):
        sc = self.scanner([github_asset_release("v3.0.0", 90_000_000), github_asset_release("v2.1.0", 31_000_000),
                           github_asset_release("v2.0.0", 30_000_000)],
                          overrides={"demo": {"github": "o/demo"}},
                          pins={"demo": {"mode": "major", "version": "2.0.0"}})
        status = self.resolve(sc, version="2.0.0")
        self.assertEqual((status.update.version, status.update.size), ("2.1.0", 31_000_000))

    def test_a_winget_update_is_measured_from_the_manifest_installer(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0")})
        status = self.resolve(sc, available="2.0")
        self.assertEqual((status.update.source, status.update.size), ("winget", 12 * MB))
        self.assertEqual(self.asked, ["https://vendor.example/Demo-2.0.exe"])
        cached = sc.cache.entry("winget_meta", "Demo.App")
        self.assertEqual((cached["size"], cached["size_v"]), (12 * MB, "2.0"))

    def test_the_cached_size_is_not_asked_for_again(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0")})
        self.resolve(sc, available="2.0")
        status = self.resolve(sc, available="2.0")
        self.assertEqual(status.update.size, 12 * MB)
        self.assertEqual(len(self.asked), 1)

    def test_a_new_version_is_measured_again(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.1", "https://vendor.example/Demo-2.1.exe",
                                                        size=3 * MB, size_v="2.0", size_ts=time.time())})
        status = self.resolve(sc, available="2.1")
        self.assertEqual(self.asked, ["https://vendor.example/Demo-2.1.exe"])
        self.assertEqual(status.update.size, 12 * MB)

    def test_a_size_in_the_cache_for_another_version_is_never_shown(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0", size=3 * MB, size_v="1.0", size_ts=time.time())})
        status = self.resolve(sc, available="2.0")
        self.assertEqual(status.update.size, 12 * MB)

    def test_the_installer_of_another_version_than_the_offered_one_gives_no_size(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0")})
        status = self.resolve(sc, available="2.1")
        self.assertEqual((status.update.version, status.update.size), ("2.1", 0))
        self.assertEqual(self.asked, [])

    def test_trailing_zeros_do_not_count_as_another_version(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0.0")})
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 12 * MB)

    def test_nothing_is_asked_when_there_is_no_update(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0")})
        status = self.resolve(sc, version="2.0", winget_id=True)
        self.assertIsNone(status.update)
        self.assertEqual(self.asked, [])

    def test_a_package_without_an_installer_url_stays_unknown(self):
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0", url=None)})
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 0)
        self.assertEqual(self.asked, [])

    def test_a_winget_fallback_is_not_measured_but_uses_a_size_already_known(self):
        releases = [github_asset_release("v2.0.0", 45_000_000)]
        overrides = {"demo": {"github": "o/demo"}}
        sc = self.scanner(releases, overrides, meta={"Demo.App": winget_meta("2.0")})
        status = self.resolve(sc, available="2.0")
        self.assertEqual((status.update.source, status.fallbacks[0].source, status.fallbacks[0].size), ("github", "winget", 0))
        self.assertEqual(self.asked, [])
        sc = self.scanner(releases, overrides, meta={"Demo.App": winget_meta("2.0", size=8 * MB, size_v="2.0")})
        self.assertEqual(self.resolve(sc, available="2.0").fallbacks[0].size, 8 * MB)

    def test_a_server_that_gives_no_size_is_not_asked_again_for_a_day(self):
        self.answers["https://vendor.example/Demo-2.0.exe"] = 0
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0")})
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 0)
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 0)
        self.assertEqual(len(self.asked), 1)
        cached = sc.cache.entry("winget_meta", "Demo.App")
        cached["size_ts"] = time.time() - scanner.SIZE_RETRY - 5
        sc.cache.put("winget_meta", "Demo.App", cached)
        self.answers["https://vendor.example/Demo-2.0.exe"] = 9 * MB
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 9 * MB)

    def test_a_failed_request_is_not_remembered(self):
        self.answers["https://vendor.example/Demo-2.0.exe"] = TimeoutError("timed out")
        sc = self.scanner(meta={"Demo.App": winget_meta("2.0")})
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 0)
        self.assertNotIn("size", sc.cache.entry("winget_meta", "Demo.App"))
        self.answers.clear()
        self.assertEqual(self.resolve(sc, available="2.0").update.size, 12 * MB)

    def test_a_site_rule_update_is_measured_from_its_download_url(self):
        sc = self.scanner(overrides={"demo": {"site": {"url": "https://vendor.example/dl", "version_regex": "v(.*)"}}})
        with mock.patch.object(site, "check", return_value=("2.0", "https://vendor.example/Demo-2.0.exe")):
            status = self.resolve(sc)
        self.assertEqual((status.update.source, status.update.size), ("site", 12 * MB))
        with mock.patch.object(site, "check", return_value=("2.0", None)):
            self.assertEqual(self.resolve(sc).update.size, 0)

    def test_a_scan_that_finds_no_network_stops_asking(self):
        for n in range(10):
            self.answers[f"https://vendor.example/{n}.exe"] = OSError("no route")
        sc = self.scanner()
        for n in range(10):
            sc.cache.put("winget_meta", f"P.{n}", winget_meta("2.0", f"https://vendor.example/{n}.exe"))
            status = AppStatus(make_app(f"App{n}"), winget_id=f"P.{n}", winget_available="2.0")
            sc.resolve(status, False)
        self.assertEqual(len(self.asked), scanner.SIZE_OFFLINE_AFTER)

    def test_an_answer_resets_the_count_of_failures(self):
        budget = scanner._SizeBudget()
        self.answers["https://a.example/bad"] = OSError("no route")
        for _ in range(scanner.SIZE_OFFLINE_AFTER - 1):
            self.assertIsNone(budget.measure("https://a.example/bad"))
        self.assertEqual(budget.measure("https://a.example/ok"), 12 * MB)
        for _ in range(scanner.SIZE_OFFLINE_AFTER - 1):
            self.assertIsNone(budget.measure("https://a.example/bad"))
        self.assertEqual(budget.measure("https://a.example/ok"), 12 * MB)

    def test_a_lookup_that_hangs_is_given_up_on(self):
        def hang(url, timeout=winget.SIZE_TIMEOUT):
            time.sleep(0.5)
            return 12 * MB

        budget = scanner._SizeBudget()
        started = time.monotonic()
        with mock.patch.object(scanner, "SIZE_DEADLINE", 0.05), mock.patch.object(winget, "remote_size", hang):
            self.assertIsNone(budget.measure("https://a.example/x"))
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(budget.failed, 1, "no answer in time counts as no network")

    def test_an_unexpected_error_is_neither_raised_nor_counted_as_a_missing_network(self):
        self.answers["https://a.example/x"] = RuntimeError("boom")
        budget = scanner._SizeBudget()
        with self.assertLogs("upstream.scan", "WARNING"):
            self.assertIsNone(budget.measure("https://a.example/x"))
        self.assertEqual(budget.failed, 0)

    def test_the_budget_runs_out(self):
        budget = scanner._SizeBudget(seconds=0)
        self.assertIsNone(budget.measure("https://a.example/ok"))
        self.assertEqual(self.asked, [])

        def slow(url, timeout=winget.SIZE_TIMEOUT):
            time.sleep(0.05)
            return 12 * MB

        budget = scanner._SizeBudget(seconds=0.04)
        with mock.patch.object(winget, "remote_size", slow):
            self.assertEqual(budget.measure("https://a.example/1"), 12 * MB)
            self.assertIsNone(budget.measure("https://a.example/2"))

    def test_a_scan_sizes_its_updates_in_parallel_within_a_few_seconds(self):
        def slow(url, timeout=winget.SIZE_TIMEOUT):
            time.sleep(0.2)
            return 12 * MB

        sc = self.scanner()
        statuses = []
        for n in range(24):
            sc.cache.put("winget_meta", f"P.{n}", winget_meta("2.0", f"https://vendor.example/{n}.exe"))
            statuses.append(AppStatus(make_app(f"App{n}"), winget_id=f"P.{n}", winget_available="2.0"))
        started = time.monotonic()
        with mock.patch.object(winget, "remote_size", slow):
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda s: sc.resolve(s, False), statuses))
        self.assertTrue(all(s.update.size == 12 * MB for s in statuses))
        self.assertLess(time.monotonic() - started, 4)


class ManifestRefreshTest(unittest.TestCase):
    """The winget manifest is read again when it is older than the update on offer; the size measured for an installer
    survives a refresh that finds the same installer."""

    def setUp(self):
        self.shown: list[str] = []
        self.fresh: dict[str, dict] = {}

        def show(pkg_id):
            self.shown.append(pkg_id)
            return dict(self.fresh[pkg_id])

        patch = mock.patch.object(winget, "show", show)
        patch.start()
        self.addCleanup(patch.stop)
        self.sc = Scanner(FakeSettings(), FakeCache())

    def refresh(self, version, available, age, **extra):
        self.sc.cache.put("winget_meta", "Demo.App", winget_meta(version, **extra))
        stored = self.sc.cache.store[("winget_meta", "Demo.App")]
        stored["ts"] = time.time() - age
        status = AppStatus(make_app(), winget_id="Demo.App", winget_available=available)
        self.sc._refresh_meta([status], lambda *a: None)

    def test_a_manifest_behind_the_offered_version_is_read_again(self):
        self.fresh["Demo.App"] = winget_meta("2.1", "https://vendor.example/Demo-2.1.exe")
        self.refresh("2.0", "2.1", age=scanner.META_RECHECK + 60)
        self.assertEqual(self.shown, ["Demo.App"])
        self.assertEqual(self.sc.cache.entry("winget_meta", "Demo.App")["version"], "2.1")

    def test_a_fresh_manifest_is_left_alone_even_when_the_versions_differ(self):
        self.refresh("2.0", "2.1", age=60)
        self.assertEqual(self.shown, [])

    def test_the_same_version_written_differently_is_not_a_difference(self):
        self.refresh("2.0.0", "2.0", age=scanner.META_RECHECK + 60)
        self.assertEqual(self.shown, [])

    def test_no_update_on_offer_means_no_new_reading(self):
        self.refresh("2.0", "", age=scanner.META_RECHECK + 60)
        self.assertEqual(self.shown, [])

    def test_a_week_old_manifest_is_read_again_and_keeps_the_size_of_an_unchanged_installer(self):
        self.fresh["Demo.App"] = winget_meta("2.0")
        self.refresh("2.0", "2.0", age=scanner.META_TTL + 60, size=8 * MB, size_v="2.0", size_ts=1.0)
        self.assertEqual(self.shown, ["Demo.App"])
        cached = self.sc.cache.entry("winget_meta", "Demo.App")
        self.assertEqual((cached["size"], cached["size_v"]), (8 * MB, "2.0"))

    def test_a_new_installer_url_drops_the_old_size(self):
        self.fresh["Demo.App"] = winget_meta("2.0", "https://other.example/Demo-2.0.exe")
        self.refresh("2.0", "2.0", age=scanner.META_TTL + 60, size=8 * MB, size_v="2.0", size_ts=1.0)
        self.assertNotIn("size", self.sc.cache.entry("winget_meta", "Demo.App"))


class SnapshotSizeTest(test_snapshot.TempSnapshot):
    def statuses(self):
        github = Update("github", "4.0.0", "o/demo", download_url="https://github.com/o/demo/releases/download/v4/D.exe",
                        size=47_395_635)
        winget_fallback = Update("winget", "4.0.0", "Demo.App", size=45 * MB)
        return [AppStatus(make_app("Demo", "3.0"), update=github, fallbacks=[winget_fallback]),
                AppStatus(make_app("Other", "1.0"), update=Update("winget", "1.1", "Other.App"))]

    def test_sizes_survive_the_snapshot(self):
        self.assertTrue(save_snapshot(self.statuses()))
        loaded, _ = load_snapshot()
        self.assertEqual([loaded[0].update.size, loaded[0].fallbacks[0].size, loaded[1].update.size],
                         [47_395_635, 45 * MB, 0])
        self.assertEqual(self.doc()["schema"], scanner.SNAPSHOT_SCHEMA)

    def test_a_snapshot_written_before_sizes_existed_still_loads(self):
        save_snapshot(self.statuses())
        doc = self.doc()
        for entry in doc["statuses"]:
            for update in [entry["update"], *entry["fallbacks"]]:
                update.pop("size")
        self.write(doc)
        loaded, _ = load_snapshot()
        self.assertEqual([s.update.size for s in loaded], [0, 0])
        self.assertEqual(loaded[0].update.version, "4.0.0")

    def test_a_size_of_the_wrong_type_drops_only_that_entry(self):
        save_snapshot(self.statuses())
        doc = self.doc()
        doc["statuses"][0]["update"]["size"] = "big"
        self.write(doc)
        loaded, _ = load_snapshot()
        self.assertEqual([s.app.name for s in loaded], ["Other"])



class SizeTextTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_unknown_shows_nothing(self):
        self.assertEqual(Update("winget", "1", "X").size_text(), "")

    def test_sizes_read_in_the_unit_that_fits(self):
        self.assertEqual(Update("winget", "1", "X", size=47_395_635).size_text(), "45.2 MB")
        self.assertEqual(Update("winget", "1", "X", size=3 * 1024).size_text(), "3.0 KB")
        self.assertEqual(Update("winget", "1", "X", size=int(1.5 * 1024 ** 3)).size_text(), "1.5 GB")

    def test_persian_uses_persian_digits(self):
        i18n.set_language("fa")
        self.assertEqual(Update("winget", "1", "X", size=47_395_635).size_text(), "\u06f4\u06f5.\u06f2 MB")

    def test_the_summary_totals_what_is_offered_and_says_when_it_is_partial(self):
        def status(name, size, offered=True):
            return AppStatus(make_app(name), update=Update("winget", "2", "X", size=size) if offered else None)

        known = [status("A", 3 * MB), status("B", 5 * MB), status("C", 0, offered=False)]
        self.assertEqual(scanner.download_summary(known), "8.0 MB to download")
        self.assertEqual(scanner.download_summary([*known, status("D", 0)]), "At least 8.0 MB to download")
        self.assertEqual(scanner.download_summary([status("D", 0), status("C", 0, offered=False)]), "")
        self.assertEqual(scanner.download_summary([]), "")
        i18n.set_language("fa")
        self.assertIn("\u06f8", scanner.download_summary(known))
        self.assertNotIn("to download", scanner.download_summary(known))


if __name__ == "__main__":
    unittest.main()
