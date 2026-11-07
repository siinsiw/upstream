"""The network layer against a local server on 127.0.0.1: proxy choice, resumable downloads
(Range, If-Range, 206/200/416, retries, cancel), the https-only rules for unverified downloads
and the metered / battery policy. No Internet."""
from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import io
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

from upstream import config, net
from upstream.context import CancelToken, UpdateContext
from upstream.errors import Cancelled, InstallError

DATA = random.Random(7).randbytes(900_000)
SHA = hashlib.sha256(DATA).hexdigest()
OTHER = random.Random(8).randbytes(700_000)


class Origin(http.server.ThreadingHTTPServer):
    """/file.bin and /named serve DATA with ETag, Last-Modified and Range support; /redirect goes to /named
    and /to-ftp to an ftp:// address (a redirect the opener must refuse).
    `faults` holds one entry per request: {"status": 503} or {"cut": n} (close the connection after n bytes)."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), OriginHandler)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.data = DATA
        self.etag = '"v1"'
        self.last_modified = "Mon, 02 Nov 2026 10:00:00 GMT"
        self.honour_range = True
        self.faults: list[dict] = []
        self.requests: list[tuple] = []     # (path, Range, If-Range)


class OriginHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        path = urllib.parse.urlsplit(self.path).path
        with server.lock:
            server.requests.append((path, self.headers.get("Range"), self.headers.get("If-Range")))
            fault = server.faults.pop(0) if server.faults and path not in ("/redirect", "/to-ftp") else {}
        if path in ("/redirect", "/to-ftp"):
            self.send_response(302)
            self.send_header("Location", "/named?x=1" if path == "/redirect" else "ftp://127.0.0.1/file.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path not in ("/file.bin", "/named"):
            self.send_error(404)
            return
        if "status" in fault:
            self.send_error(fault["status"])
            return
        data, start, status = server.data, 0, 200
        wanted = self.headers.get("Range")
        if wanted and server.honour_range and self.headers.get("If-Range") in (None, server.etag, server.last_modified):
            start = int(re.fullmatch(r"bytes=(\d+)-", wanted).group(1))
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        body = data[start:]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", server.etag)
        self.send_header("Last-Modified", server.last_modified)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        if path == "/named":
            self.send_header("Content-Disposition", 'attachment; filename="tool-1.2.exe"')
        self.end_headers()
        body = body[:fault["cut"]] if "cut" in fault else body
        try:
            for i in range(0, len(body), 65536):
                self.wfile.write(body[i:i + 65536])
        except OSError:
            pass


class FakeProxy(http.server.ThreadingHTTPServer):
    """Answers every absolute-URI request with b"via-proxy" and remembers what it was asked."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeProxyHandler)
        self.seen: list[tuple] = []


class FakeProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Proxy-Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"via-proxy")


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


ORIGIN: Origin


def setUpModule():
    global ORIGIN
    ORIGIN = Origin()
    threading.Thread(target=ORIGIN.serve_forever, daemon=True).start()


def tearDownModule():
    ORIGIN.shutdown()
    ORIGIN.server_close()


class DownloadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for patch in (mock.patch.object(net, "DOWNLOAD_DIR", self.dir),
                      mock.patch.object(net, "RETRY_DELAYS", (0.01, 0.01, 0.01))):
            patch.start()
            self.addCleanup(patch.stop)
        ORIGIN.reset()
        self.progress: list[tuple[int, int]] = []
        self.addCleanup(net.log.setLevel, net.log.level)
        net.log.setLevel(logging.ERROR)         # the retries are expected here

    def url(self, path="/file.bin"):
        return f"http://127.0.0.1:{ORIGIN.server_port}{path}"

    def record(self, done, total):
        self.progress.append((done, total))

    def get(self, path="/file.bin", name="out.bin", sha=None, cancel=None):
        return net.download(self.url(path), name, self.record, sha, cancel)

    def cancel_after(self, nbytes):
        token = CancelToken()

        def on_progress(done, total):
            self.record(done, total)
            if done >= nbytes:
                token.cancel()
        return token, on_progress

    def interrupt(self, path="/file.bin", name="out.bin", nbytes=100_000):
        """Start a download and cancel it part-way; returns the size of the partial file kept."""
        token, on_progress = self.cancel_after(nbytes)
        with self.assertRaises(Cancelled):
            net.download(self.url(path), name, on_progress, None, token)
        part, meta = net._part_paths(self.url(path))
        self.assertTrue(part.exists() and meta.exists())
        self.assertTrue(0 < part.stat().st_size < len(ORIGIN.data))
        ORIGIN.requests.clear()
        self.progress.clear()
        return part.stat().st_size

    def seed(self, content, **meta):
        part, meta_path = net._part_paths(self.url())
        part.write_bytes(content)
        meta_path.write_text(json.dumps({"url": self.url(), "name": "out.bin", "etag": '"v1"',
                                         "last_modified": "", "size": len(DATA), **meta}), "utf-8")

    def test_fresh_download(self):
        dest = self.get(sha=SHA)
        self.assertEqual(dest, self.dir / "out.bin")
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(self.progress[-1], (len(DATA), len(DATA)))
        self.assertEqual(ORIGIN.requests, [("/file.bin", None, None)])
        self.assertEqual([p.name for p in self.dir.iterdir()], ["out.bin"])

    def test_file_name_rules(self):
        self.assertEqual(self.get("/named", name=None).name, "tool-1.2.exe")
        self.assertEqual(self.get("/file.bin", name=None).name, "file.bin")
        self.assertEqual(self.get(name='a:b?.exe').name, "a_b_.exe")
        self.assertEqual(self.get("/redirect", name=None).name, "tool-1.2.exe")

    def test_wrong_hash_is_refused_and_nothing_is_kept(self):
        with self.assertRaises(InstallError) as caught:
            self.get(sha="0" * 64)
        self.assertTrue(caught.exception.template.startswith("The download's SHA-256 doesn't match"))
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual(len(ORIGIN.requests), 1)

    def test_cancel_keeps_the_partial_file_and_the_next_try_resumes(self):
        kept = self.interrupt()
        self.assertFalse((self.dir / "out.bin").exists())
        dest = self.get(sha=SHA)            # the hash covers the kept prefix as well
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(ORIGIN.requests, [("/file.bin", f"bytes={kept}-", '"v1"')])
        self.assertEqual(self.progress[0], (kept, len(DATA)))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["out.bin"])

    def test_resume_works_across_a_redirect_without_a_file_name(self):
        kept = self.interrupt("/redirect", name=None)
        dest = self.get("/redirect", name=None, sha=SHA)
        self.assertEqual((dest.name, dest.read_bytes()), ("tool-1.2.exe", DATA))
        self.assertEqual(ORIGIN.requests, [("/redirect", f"bytes={kept}-", '"v1"'),
                                          ("/named", f"bytes={kept}-", '"v1"')])

    def test_dropped_connection_is_retried_from_where_it_stopped(self):
        ORIGIN.faults = [{"cut": 400_000}]
        dest = self.get(sha=SHA)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(ORIGIN.requests, [("/file.bin", None, None), ("/file.bin", "bytes=400000-", '"v1"')])
        done = [d for d, _ in self.progress]
        self.assertEqual(done, sorted(done))
        self.assertEqual({t for _, t in self.progress}, {len(DATA)})

    def test_server_errors_are_retried(self):
        ORIGIN.faults = [{"status": 503}, {"status": 500}]
        self.assertEqual(self.get().read_bytes(), DATA)
        self.assertEqual(len(ORIGIN.requests), 3)

    def test_gives_up_after_three_retries(self):
        ORIGIN.faults = [{"status": 503}] * 10
        with self.assertRaises(InstallError) as caught:
            self.get()
        self.assertEqual(caught.exception.template, "The server is not answering properly (HTTP {code}). Try again later.")
        self.assertEqual(caught.exception.values, {"code": 503})
        self.assertEqual(len(ORIGIN.requests), 4)

    def test_a_connection_that_keeps_dropping_stops_with_a_reason(self):
        ORIGIN.faults = [{"cut": 10_000}] * 10
        with self.assertRaises(InstallError) as caught:
            self.get()
        self.assertEqual(caught.exception.template, "The connection keeps failing, so the download stopped: {error}")
        self.assertEqual(caught.exception.values, {"error": "The connection closed before the whole file arrived"})
        self.assertEqual(len(ORIGIN.requests), 4)

    def test_progress_resets_the_retry_count(self):
        ORIGIN.faults = [{"cut": 150_000}] * 5          # five drops, each one after real progress
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual(len(ORIGIN.requests), 6)

    def test_client_errors_are_not_retried(self):
        with self.assertRaises(InstallError) as caught:
            self.get("/missing")
        self.assertEqual(caught.exception.template, "The server refused the download (HTTP {code}).")
        self.assertEqual(caught.exception.values, {"code": 404})
        self.assertEqual(len(ORIGIN.requests), 1)

    def test_cancel_during_the_pause_between_retries(self):
        ORIGIN.faults = [{"status": 503}] * 5
        token = CancelToken()
        threading.Timer(0.3, token.cancel).start()
        started = time.monotonic()
        with mock.patch.object(net, "RETRY_DELAYS", (30.0, 30.0, 30.0)), self.assertRaises(Cancelled):
            self.get(cancel=token)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(ORIGIN.requests), 1)

    def test_a_cancelled_token_does_not_touch_the_network(self):
        token = CancelToken()
        token.cancel()
        with self.assertRaises(Cancelled):
            self.get(cancel=token)
        self.assertEqual(ORIGIN.requests, [])

    def test_server_that_ignores_range_restarts_from_zero(self):
        self.interrupt()
        ORIGIN.honour_range = False
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual(self.progress[0], (0, len(DATA)))

    def test_changed_file_is_downloaded_again_in_full(self):
        self.interrupt()
        ORIGIN.data, ORIGIN.etag = OTHER, '"v2"'
        dest = self.get(sha=hashlib.sha256(OTHER).hexdigest())
        self.assertEqual(dest.read_bytes(), OTHER)
        self.assertEqual(self.progress[-1], (len(OTHER), len(OTHER)))

    def test_a_weak_etag_gives_way_to_last_modified_for_if_range(self):
        ORIGIN.etag = 'W/"weak"'
        kept = self.interrupt()
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual(ORIGIN.requests, [("/file.bin", f"bytes={kept}-", ORIGIN.last_modified)])

    def test_without_a_validator_the_download_starts_over(self):
        ORIGIN.etag, ORIGIN.last_modified = 'W/"weak"', ""
        self.interrupt()
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual(ORIGIN.requests, [("/file.bin", None, None)])

    def test_416_on_a_complete_partial_file_finishes_it(self):
        self.seed(DATA)
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual(ORIGIN.requests, [("/file.bin", f"bytes={len(DATA)}-", '"v1"')])

    def test_416_on_an_oversized_partial_file_starts_over(self):
        self.seed(DATA + b"extra")
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual([r[1] for r in ORIGIN.requests], [f"bytes={len(DATA) + 5}-", None])

    def test_corrupt_prefix_fails_the_hash_and_is_downloaded_again_once(self):
        self.seed(b"\0" * 300_000)
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual([r[1] for r in ORIGIN.requests], ["bytes=300000-", None])

    def test_wrong_hash_after_a_resume_retries_once_then_refuses(self):
        self.interrupt()
        with self.assertRaises(InstallError) as caught:
            self.get(sha="0" * 64)
        self.assertTrue(caught.exception.template.startswith("The download's SHA-256 doesn't match"))
        self.assertEqual(len(ORIGIN.requests), 2)
        self.assertEqual(ORIGIN.requests[1][1], None)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_sidecar_for_another_url_is_ignored(self):
        part, meta_path = net._part_paths(self.url())
        part.write_bytes(b"\0" * 1000)
        meta_path.write_text(json.dumps({"url": "http://elsewhere.test/x", "etag": '"v1"'}), "utf-8")
        self.assertEqual(self.get(sha=SHA).read_bytes(), DATA)
        self.assertEqual(ORIGIN.requests, [("/file.bin", None, None)])

    def test_stale_partial_files_are_removed_when_a_download_starts(self):
        week = time.time() - 8 * 24 * 3600
        for name in ("old.exe.part", "old.exe.part.json", "keep.exe", "recent.exe.part"):
            (self.dir / name).write_bytes(b"x")
        for name in ("old.exe.part", "old.exe.part.json", "keep.exe"):
            os.utime(self.dir / name, (week, week))
        self.get()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["keep.exe", "out.bin", "recent.exe.part"])

    def test_cannot_write_the_result(self):
        (self.dir / "out.bin").mkdir()          # a folder in the way: os.replace cannot overwrite it
        with self.assertRaises(InstallError) as caught:
            self.get()
        self.assertEqual(caught.exception.template, "Couldn't save the download: {error}")

    def test_downloads_stay_out_of_the_real_folder(self):
        self.assertEqual(net.DOWNLOAD_DIR, self.dir)
        self.assertNotEqual(net.DOWNLOAD_DIR, config.DOWNLOAD_DIR)

    def test_a_redirect_to_another_protocol_is_refused_and_not_retried(self):
        with self.assertRaises(InstallError) as caught:
            self.get("/to-ftp")
        self.assertEqual(caught.exception.template,
                         "Refused: the server redirected the download to a less secure address.")
        self.assertEqual(len(ORIGIN.requests), 1)
        self.assertEqual(list(self.dir.iterdir()), [])


class FakeResponse(io.BytesIO):
    """What net.urlopen hands back for a 200: the body, the final URL and the few attributes download reads."""

    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.url, self.status = url, 200
        self.headers = {"Content-Length": str(len(body))}

    def geturl(self):
        return self.url


class FakeHttps(urllib.request.BaseHandler):
    """Answers https:// requests from {url: (status, Location header or body)} instead of the network."""
    handler_order = 400         # ahead of the real HTTPSHandler

    def __init__(self, pages):
        self.pages = pages

    def https_open(self, req):
        status, payload = self.pages[req.full_url]
        headers = http.client.HTTPMessage()
        if status == 200:
            body = payload
        else:
            body = b""
            headers["Location"] = payload
        resp = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, status)
        resp.msg = "OK" if status == 200 else "Found"
        return resp


class RedirectPolicyTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(net.log.setLevel, net.log.level)
        net.log.setLevel(logging.ERROR)

    def follow(self, source, target):
        """What _SafeRedirect decides for a 302 from `source` to `target`: (new request, the response stream)."""
        fp = io.BytesIO()
        new = net._SafeRedirect().redirect_request(urllib.request.Request(source), fp, 302, "Found",
                                                   http.client.HTTPMessage(), target)
        return new, fp

    def test_https_to_http_is_refused_even_to_the_same_host_or_loopback(self):
        for target in ("http://example.test/a", "http://127.0.0.1:8000/a", "HTTP://example.test/a"):
            fp = io.BytesIO()
            with self.assertRaises(net.RedirectRefused, msg=target):
                net._SafeRedirect().redirect_request(urllib.request.Request("https://example.test/x"), fp, 302,
                                                     "Found", http.client.HTTPMessage(), target)
            self.assertTrue(fp.closed)

    def test_any_scheme_but_http_and_https_is_refused(self):
        for source in ("http://example.test/x", "https://example.test/x"):
            for target in ("ftp://example.test/a", "file:///C:/Windows/a.exe", "gopher://example.test/a"):
                with self.assertRaises(net.RedirectRefused, msg=f"{source} -> {target}"):
                    self.follow(source, target)

    def test_hops_that_do_not_downgrade_are_followed(self):
        for source, target in (("http://a.test/x", "http://b.test/y"), ("http://a.test/x", "https://b.test/y"),
                               ("https://a.test/x", "https://b.test/y")):
            new, _ = self.follow(source, target)
            self.assertEqual(new.full_url, target)

    def test_every_opener_is_built_with_it(self):
        for mode in ("system", "none", "custom"):
            opener = net._build_opener(mode, "127.0.0.1:3128")
            redirectors = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
            self.assertEqual([type(h) for h in redirectors], [net._SafeRedirect], mode)

    def test_the_opener_stops_at_the_refused_hop_and_follows_the_safe_one(self):
        opener = net._build_opener("none", "")
        opener.add_handler(FakeHttps({
            "https://a.test/down": (302, "http://a.test/plain"),
            "https://a.test/ftp": (302, "ftp://a.test/file"),
            "https://a.test/ok": (302, "https://b.test/file"),
            "https://b.test/file": (200, b"payload"),
        }))
        for page in ("down", "ftp"):
            with self.assertRaises(net.RedirectRefused, msg=page):
                opener.open(f"https://a.test/{page}")
        with opener.open("https://a.test/ok") as resp:
            self.assertEqual((resp.read(), resp.geturl()), (b"payload", "https://b.test/file"))

    def test_loopback_hosts(self):
        for host in ("localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "::1"):
            self.assertTrue(net._is_loopback(host), host)
        for host in ("example.test", "10.0.0.1", "0.0.0.0", "localhost.example.test", "127.0.0.1.example.test",
                     "", None):
            self.assertFalse(net._is_loopback(host), host)


class SecureDownloadTest(unittest.TestCase):
    """Without a published SHA-256 only a download that stayed on https (or on this PC) is trusted."""
    BODY = random.Random(9).randbytes(5000)
    SHA = hashlib.sha256(BODY).hexdigest()
    PLAIN = "Refused: this download isn't served over HTTPS and has no published checksum."

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.opened: list[str] = []
        self.ends_at: str | None = None

        def fake_urlopen(req, timeout=20):
            self.opened.append(req.full_url)
            return FakeResponse(self.BODY, self.ends_at or req.full_url)
        for patch in (mock.patch.object(net, "DOWNLOAD_DIR", self.dir), mock.patch.object(net, "urlopen", fake_urlopen)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(net.log.setLevel, net.log.level)
        net.log.setLevel(logging.ERROR)

    def get(self, url, sha=None):
        return net.download(url, "app.exe", lambda done, total: None, sha)

    def assertRefused(self, url, sha=None, template=PLAIN):
        with self.assertRaises(InstallError) as caught:
            self.get(url, sha)
        self.assertEqual(caught.exception.template, template)
        self.assertFalse((self.dir / "app.exe").exists())

    def test_plain_http_without_a_hash_is_refused_before_any_request(self):
        for url in ("http://downloads.example.test/app.exe", "ftp://downloads.example.test/app.exe"):
            self.assertRefused(url)
        self.assertEqual(self.opened, [])
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_plain_http_with_a_published_hash_is_allowed(self):
        self.assertEqual(self.get("http://downloads.example.test/app.exe", self.SHA).read_bytes(), self.BODY)

    def test_the_hash_must_still_match_on_plain_http(self):
        with self.assertRaises(InstallError) as caught:
            self.get("http://downloads.example.test/app.exe", "0" * 64)
        self.assertTrue(caught.exception.template.startswith("The download's SHA-256 doesn't match"))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_https_needs_no_hash(self):
        self.assertEqual(self.get("https://downloads.example.test/app.exe").read_bytes(), self.BODY)

    def test_an_https_request_that_ends_on_plain_http_is_refused_without_a_hash(self):
        self.ends_at = "http://mirror.example.test/app.exe"
        self.assertRefused("https://downloads.example.test/app.exe")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.get("https://downloads.example.test/app.exe", self.SHA).read_bytes(), self.BODY)

    def test_loopback_urls_are_exempt(self):
        for url in ("http://127.0.0.1:8080/app.exe", "http://localhost/app.exe", "http://[::1]:8080/app.exe"):
            self.assertEqual(self.get(url).read_bytes(), self.BODY, url)

    def test_a_loopback_request_that_ends_elsewhere_on_plain_http_is_refused(self):
        self.ends_at = "http://203.0.113.9/app.exe"
        self.assertRefused("http://127.0.0.1:8080/app.exe")

    def test_a_416_that_finishes_the_kept_file_checks_the_address_too(self):
        url = "https://downloads.example.test/app.exe"
        part, meta_path = net._part_paths(url)
        part.write_bytes(self.BODY)
        meta_path.write_text(json.dumps({"url": url, "name": "app.exe", "etag": '"v1"', "last_modified": "",
                                         "size": len(self.BODY)}), "utf-8")
        headers = http.client.HTTPMessage()
        headers["Content-Range"] = f"bytes */{len(self.BODY)}"

        def answer(req, timeout=20):
            raise urllib.error.HTTPError("http://mirror.example.test/app.exe", 416, "Range Not Satisfiable",
                                         headers, io.BytesIO())
        with mock.patch.object(net, "urlopen", answer):
            self.assertRefused(url)
            self.assertTrue(part.exists())      # kept, nothing was moved into place
            self.assertEqual(self.get(url, self.SHA).read_bytes(), self.BODY)


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(net.configure, None)
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for key in ("NO_PROXY", "no_proxy"):
            os.environ.pop(key, None)
        ORIGIN.reset()

    def settings(self, mode, url=""):
        settings = FakeSettings(proxy={"mode": mode, "url": url})
        net.configure(settings)
        return settings

    def origin_url(self):
        return f"http://127.0.0.1:{ORIGIN.server_port}/file.bin"

    def test_no_settings_means_system(self):
        net.configure(None)
        self.assertEqual(net._proxy_setting(), ("system", ""))
        net.configure(FakeSettings())
        self.assertEqual(net._proxy_setting(), ("system", ""))

    def test_none_ignores_the_proxy_of_the_system(self):
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        self.settings("system")
        with self.assertRaises(urllib.error.URLError):
            net.urlopen(self.origin_url(), timeout=5)
        self.settings("none")
        with net.urlopen(self.origin_url(), timeout=5) as resp:
            self.assertEqual(resp.read(), DATA)

    def test_custom_proxy_with_and_without_credentials(self):
        proxy = FakeProxy()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        port = proxy.server_port
        with mock.patch.object(urllib.request, "proxy_bypass", lambda host: False):
            self.settings("custom", f"127.0.0.1:{port}")
            with net.urlopen("http://upstream.test/a", timeout=5) as resp:
                self.assertEqual(resp.read(), b"via-proxy")
            self.settings("custom", f"http://user:p%40ss@127.0.0.1:{port}")
            with net.urlopen("http://upstream.test/b", timeout=5) as resp:
                self.assertEqual(resp.read(), b"via-proxy")
        token = base64.b64encode(b"user:p@ss").decode()
        self.assertEqual(proxy.seen, [("http://upstream.test/a", None),
                                      ("http://upstream.test/b", f"Basic {token}")])

    def test_empty_custom_address_falls_back_to_system(self):
        self.settings("custom", "   ")
        self.assertEqual(net._proxy_setting(), ("system", ""))

    def test_invalid_custom_address_is_an_error(self):
        for bad in ("http://", "host:notaport", "ftp://host:21", "socks5://host:1080"):
            self.settings("custom", bad)
            with self.assertRaises(InstallError, msg=bad) as caught:
                net.urlopen(self.origin_url())
            self.assertEqual(caught.exception.template, "The proxy address in Settings isn't valid.")

    def test_the_opener_is_cached_by_the_setting_value(self):
        settings = self.settings("none")
        first = net._opener()
        self.assertIs(net._opener(), first)
        settings.values["proxy"] = {"mode": "system", "url": ""}
        second = net._opener()
        self.assertIsNot(second, first)
        self.assertIs(net._opener(), second)
        settings.values["proxy"] = {"mode": "custom", "url": "127.0.0.1:3128"}
        self.assertIsNot(net._opener(), second)


class PreflightTest(unittest.TestCase):
    def setUp(self):
        net._answers.clear()
        self.addCleanup(net._answers.clear)
        self.metered = False
        self.battery = None
        self.saver = False
        self.answer = True
        self.questions = []
        self.clock = [0.0]
        for patch in (mock.patch.object(net, "is_metered", lambda: self.metered),
                      mock.patch.object(net, "battery_status", lambda: self.battery),
                      mock.patch.object(net, "battery_saver", lambda: self.saver),
                      mock.patch.object(net, "_now", lambda: self.clock[0])):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, bulk=False, **settings):
        def ask(question):
            self.questions.append(question)
            return self.answer
        return UpdateContext(FakeSettings(**settings), CancelToken(), ask, bulk)

    def test_nothing_to_report_means_no_question(self):
        net.preflight(self.ctx())
        self.assertEqual(self.questions, [])

    def test_metered_ask_yes_continues(self):
        self.metered = True
        net.preflight(self.ctx())
        question, = self.questions
        self.assertEqual(question.template, "This PC is on a metered connection. Download updates anyway?")
        self.assertEqual((question.yes, question.no), ("Download anyway", "Not now"))
        self.assertTrue(question.detail)

    def test_metered_ask_no_cancels(self):
        self.metered, self.answer = True, False
        with self.assertRaises(Cancelled) as caught:
            net.preflight(self.ctx())
        self.assertEqual(caught.exception.template, "Not updated: this PC is on a metered connection.")

    def test_metered_skip_cancels_without_asking(self):
        self.metered = True
        with self.assertRaises(Cancelled):
            net.preflight(self.ctx(metered_policy="skip"))
        self.assertEqual(self.questions, [])

    def test_metered_ignore_does_not_even_look(self):
        with mock.patch.object(net, "is_metered", side_effect=AssertionError("looked")):
            net.preflight(self.ctx(metered_policy="ignore"))

    def test_unknown_policy_value_asks(self):
        self.metered = True
        net.preflight(self.ctx(metered_policy="whatever"))
        self.assertEqual(len(self.questions), 1)

    def test_unknown_connection_cost_fails_open(self):
        self.metered = None
        net.preflight(self.ctx())
        self.assertEqual(self.questions, [])

    def test_low_battery_asks_with_the_percentage(self):
        self.battery = (True, 20)
        net.preflight(self.ctx())
        question, = self.questions
        self.assertEqual(question.template, "The battery is at {percent}% and the PC is not plugged in. Update anyway?")
        self.assertEqual(question.values, {"percent": 20})
        self.assertEqual(question.yes, "Update on battery")

    def test_battery_threshold_is_inclusive(self):
        self.battery = (True, 30)
        net.preflight(self.ctx())
        self.assertEqual(len(self.questions), 1)
        self.battery = (True, 31)
        net.preflight(self.ctx())
        self.assertEqual(len(self.questions), 1)

    def test_battery_that_is_fine_or_charging_or_absent_is_not_a_problem(self):
        for self.battery in ((True, 80), (False, 5), None):
            net.preflight(self.ctx())
        self.assertEqual(self.questions, [])

    def test_unknown_percentage_alone_is_not_a_problem_but_battery_saver_is(self):
        self.battery = (True, None)
        net.preflight(self.ctx())
        self.assertEqual(self.questions, [])
        self.saver = True
        net.preflight(self.ctx())
        question, = self.questions
        self.assertEqual(question.template, "Battery saver is on and the PC is not plugged in. Update anyway?")

    def test_battery_saver_on_mains_power_is_ignored(self):
        self.battery, self.saver = (False, 90), True
        net.preflight(self.ctx())
        self.assertEqual(self.questions, [])

    def test_battery_skip_and_ignore(self):
        self.battery = (True, 10)
        with self.assertRaises(Cancelled) as caught:
            net.preflight(self.ctx(battery_policy="skip"))
        self.assertEqual(caught.exception.values, {"percent": 10})
        self.assertEqual(self.questions, [])
        net.preflight(self.ctx(battery_policy="ignore"))
        self.assertEqual(self.questions, [])

    def test_declining_on_battery_cancels(self):
        self.battery, self.answer = (True, 10), False
        with self.assertRaises(Cancelled) as caught:
            net.preflight(self.ctx())
        self.assertEqual(caught.exception.template, "Not updated: the battery is at {percent}% and the PC is not plugged in.")

    def test_both_conditions_are_asked_separately(self):
        self.metered, self.battery = True, (True, 10)
        net.preflight(self.ctx())
        self.assertEqual(len(self.questions), 2)

    def test_single_updates_ask_every_time(self):
        self.metered = True
        net.preflight(self.ctx())
        net.preflight(self.ctx())
        self.assertEqual(len(self.questions), 2)

    def test_update_all_asks_once(self):
        self.metered, self.battery = True, (True, 10)
        for _ in range(3):
            net.preflight(self.ctx(bulk=True))
        self.assertEqual(len(self.questions), 2)        # one per kind of problem, not per app

    def test_update_all_remembers_a_yes_while_the_batch_goes_on(self):
        self.metered = True
        net.preflight(self.ctx(bulk=True))
        for self.clock[0] in (200.0, 400.0, 600.0):     # each use stretches the memory
            net.preflight(self.ctx(bulk=True))
        self.assertEqual(len(self.questions), 1)
        self.clock[0] = 1000.0                          # more than five minutes after the last app
        net.preflight(self.ctx(bulk=True))
        self.assertEqual(len(self.questions), 2)

    def test_update_all_remembers_a_no_just_long_enough_to_cancel_the_rest(self):
        self.metered, self.answer = True, False
        for _ in range(3):
            with self.assertRaises(Cancelled):
                net.preflight(self.ctx(bulk=True))
        self.assertEqual(len(self.questions), 1)
        self.clock[0] = 40.0
        with self.assertRaises(Cancelled):
            net.preflight(self.ctx(bulk=True))
        self.assertEqual(len(self.questions), 2)


class SystemStateTest(unittest.TestCase):
    def test_cost_table(self):
        for cost, expected in ((0, None), (1, False), (2, True), (4, True), (0x10001, True),
                               (0x40001, True), (0x80002, True), (0x20001, False), (0x80001, False)):
            self.assertIs(net._cost_is_metered(cost), expected, hex(cost))

    def test_real_calls_do_not_raise(self):
        self.assertIn(net.is_metered(), (True, False, None))
        status = net.battery_status()
        self.assertTrue(status is None or (isinstance(status[0], bool) and (status[1] is None or 0 <= status[1] <= 100)))
        self.assertIsInstance(net.battery_saver(), bool)

    def test_battery_status_from_the_power_structure(self):
        def status(**fields):
            return mock.patch.object(net, "_power_status", lambda: net._PowerStatus(**fields))
        with status(ACLineStatus=0, BatteryFlag=1, BatteryLifePercent=20, SystemStatusFlag=1):
            self.assertEqual(net.battery_status(), (True, 20))
            self.assertTrue(net.battery_saver())
        with status(ACLineStatus=1, BatteryFlag=9, BatteryLifePercent=60, SystemStatusFlag=0):
            self.assertEqual(net.battery_status(), (False, 60))
            self.assertFalse(net.battery_saver())
        with status(ACLineStatus=0, BatteryFlag=1, BatteryLifePercent=255, SystemStatusFlag=0):
            self.assertEqual(net.battery_status(), (True, None))
        with status(ACLineStatus=1, BatteryFlag=128, BatteryLifePercent=255, SystemStatusFlag=0):
            self.assertIsNone(net.battery_status())         # a desktop PC
        with status(ACLineStatus=255, BatteryFlag=255, BatteryLifePercent=255, SystemStatusFlag=0):
            self.assertIsNone(net.battery_status())
        with mock.patch.object(net, "_power_status", lambda: None):
            self.assertIsNone(net.battery_status())
            self.assertFalse(net.battery_saver())

    def test_a_call_that_hangs_or_fails_gives_up_quietly(self):
        started = time.monotonic()
        self.assertIsNone(net._call_with_timeout(lambda: time.sleep(3), 0.1))
        self.assertLess(time.monotonic() - started, 2)

        def broken():
            raise OSError("no")
        self.assertIsNone(net._call_with_timeout(broken, 1))


if __name__ == "__main__":
    unittest.main()
