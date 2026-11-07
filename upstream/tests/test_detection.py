"""Detection accuracy: version order, repository discovery, release asset choice and winget row matching.
Everything is offline: GitHub and winget answers are canned."""
from __future__ import annotations

import copy
import string
import time
import unittest
from unittest import mock

from upstream import scanner, winget
from upstream.github import Asset, GitHub, RateLimited, _asset_kind, pick_asset, rank_repos, repo_from_urls
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Scanner
from upstream.translations import detection
from upstream.versioning import clean, compare, is_newer, is_prerelease, parse_version, same_version
from upstream.winget import WingetRow, match_rows


def make_app(name="Demo", version="1.0", publisher="Demo Inc", scope="machine", arch="x64", reg_key=None,
             install_kind="", is_msi=False, **extra):
    return InstalledApp(reg_key=reg_key or rf"HKLM64\{name}", name=name, version=version, publisher=publisher,
                        scope=scope, arch=arch, install_kind=install_kind, is_msi=is_msi, **extra)


TELEGRAM_FA = "".join(map(chr, (0x62A, 0x644, 0x6AF, 0x631, 0x627, 0x645)))   # Telegram in Persian: no Latin letters at all


def assets(*names):
    return [Asset(n, f"https://example.invalid/{n}", 1000, None) for n in names]


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


def api_release(tag, files=(), prerelease=False, title=None):
    return {"tag_name": tag, "name": title or tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}",
            "prerelease": prerelease, "draft": False, "body": "",
            "assets": [{"name": f, "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{f}",
                        "size": 1000, "digest": None} for f in files]}


def github_with(releases_by_repo, **kwargs):
    """A GitHub client whose HTTP layer answers from releases_by_repo; .calls lists the repos asked for."""
    gh = GitHub(FakeCache(), None, **kwargs)
    gh.calls = []

    def request(path, etag):
        repo = path.split("/repos/", 1)[1].split("/releases", 1)[0]
        gh.calls.append(repo)
        return 200, releases_by_repo[repo], '"etag"'

    gh._request = request
    return gh


class VersionOrderTest(unittest.TestCase):
    ORDER = [
        # (a, b, compare(a, b))
        ("1.2.3-beta.1", "1.2.3", -1),
        ("1.2.3", "1.2.3-beta.1", 1),
        ("1.2.3.0", "1.2.3", 0),
        ("1.2.3.0", "1.2.3.1", -1),
        ("1.2.3-alpha", "1.2.3-beta", -1),
        ("1.2.3-beta", "1.2.3-beta.1", -1),
        ("1.2.3-beta.2", "1.2.3-beta.10", -1),
        ("1.2.3-beta.5", "1.2.3-rc.1", -1),
        ("1.2.3-rc.1", "1.2.3", -1),
        ("1.2.3", "1.2.4-alpha", -1),
        # prefixes and suffixes that are not part of the version
        ("v8.8.3", "8.8.3", 0),
        ("Notepad++ v8.8.3", "8.8.2", 1),
        ("release-1.4.0", "windows-v1.4.0", 0),
        ("spotube-v5.1.2", "5.1.1", 1),
        ("1.2.3-windows", "1.2.3", 0),
        ("1.2.3_x64", "1.2.3", 0),
        ("1.2.3-windows-x64", "1.2.4", -1),
        ("1.2.3 (Build 456)", "1.2.3", 0),
        # trailing letters: a and b are alpha and beta, any other letter after three numbers is OpenSSL's patch level
        ("1.2.3a", "1.2.3", -1),
        ("1.2.3b", "1.2.3", -1),
        ("1.2.3b", "1.2.3a", 1),
        ("1.2.4", "1.2.3b", 1),
        ("1.2.3-b", "1.2.3", -1),
        ("1.2.3_a", "1.2.3-b", -1),
        ("1.2.3-b2", "1.2.3-b10", -1),
        ("1.1.1k", "1.1.1", 1),
        ("1.1.1w", "1.1.1k", 1),
        ("1.1.2", "1.1.1w", 1),
        ("1.1.1-k", "1.1.1", 0),
        ("2.0k", "2.0", 0),
        # Windows resource files write file versions with commas
        ("5.1.2", "5, 1, 2, 0", 0),
        ("1.2.3", "1,2,3", 0),
        ("1.2.4", "1,2,3", 1),
        ("5.1.2.1", "5, 1, 2, 0", 1),
        ("5, 1, 2, 0", "5, 1, 3, 0", -1),
        ("2024, 3, 1", "2024.3.1", 0),
        ("3.13.0a1", "3.13.0b1", -1),
        ("3.13.0b2", "3.13.0rc1", -1),
        ("3.13.0rc1", "3.13.0", -1),
        ("3.12.4", "3.12.3", 1),
        # build metadata and a build part
        ("3.9.0+38", "3.9.0", 0),
        ("1.0.0+build.5", "1.0.0+build.9", 0),
        ("17.11-1", "17.11", 1),
        # Git for Windows: windows.1 is the plain release, windows.N installs as .N
        ("2.51.0.windows.1", "2.51.0", 0),
        ("2.51.0.windows.2", "2.51.0.2", 0),
        ("2.51.0.windows.2", "2.51.0.windows.1", 1),
        ("2.51.0.windows.1", "2.50.1.windows.2", 1),
        # numbers compare as numbers
        ("0.10.0", "0.9.1", 1),
        ("25.01", "24.09", 1),
        ("25.01", "25.1", 0),
        ("124.0.6367.119", "124.0.6367.60", 1),
        ("14.42.34433.0", "14.40.33810.0", 1),
        # date versions
        ("2024.03.1", "2024.3.1", 0),
        ("2024.04", "2024.03.1", 1),
        ("2024-03-14", "2024-04-02", -1),
        ("2024-03-14-12-55", "2024-03-14", 1),
        ("20240314", "2024-03-14", 0),
        ("autobuild-2024-04-02-12-55", "2024-03-14", 1),
    ]
    NO_ORDER = [
        ("2026.11.02", "8.0.1"),
        ("8.0.1", "2026.11.02"),
        ("2024-03-14", "7.1.1"),
        ("build-1234", "2.5.1"),
        ("latest", "1.0"),
        ("nightly", "1.0"),
        ("1.0", ""),
        ("", "1.0"),
        (None, "1.0"),
        ("1.0", None),
    ]

    def test_order_table(self):
        for a, b, expected in self.ORDER:
            with self.subTest(a=a, b=b):
                self.assertEqual(compare(a, b), expected)
                self.assertEqual(compare(b, a), -expected)

    def test_incomparable_pairs_have_no_order_and_never_offer_an_update(self):
        for a, b in self.NO_ORDER:
            with self.subTest(a=a, b=b):
                self.assertIsNone(compare(a, b))
                self.assertFalse(is_newer(a, b))
                self.assertFalse(same_version(a, b))

    def test_is_newer_and_same_version(self):
        self.assertTrue(is_newer("1.2.3", "1.2.3-beta.1"))
        self.assertFalse(is_newer("1.2.3-beta.1", "1.2.3"))
        self.assertFalse(is_newer("1.2.3.0", "1.2.3"))
        self.assertTrue(is_newer("v8.8.3", "8.8.2"))
        self.assertTrue(same_version("1.2.3.0", "1.2.3"))
        self.assertFalse(same_version("1.2.3-beta", "1.2.3"))

    def test_parse_version_keeps_the_numeric_core(self):
        for text, core in [("Git 2.51.0.windows.1", (2, 51)), ("3.9.0+38", (3, 9)), ("v1.2.3.0", (1, 2, 3)),
                           ("Notepad++ v8.8.3", (8, 8, 3)), ("2024-03-14", (2024, 3, 14)), ("7", (7,)),
                           ("17.11-1", (17, 11, 1)), ("5, 1, 2, 0", (5, 1, 2)), ("1,2,3", (1, 2, 3)),
                           ("1,5", (1,)), ("latest", None), ("", None), (None, None)]:
            with self.subTest(text=text):
                self.assertEqual(parse_version(text), core)

    def test_an_installed_version_with_commas_is_never_an_update_over_itself(self):
        for candidate, installed in [("5.1.2", "5, 1, 2, 0"), ("1.2.3", "1,2,3"), ("1.2.3.4", "1, 2, 3, 4")]:
            with self.subTest(installed=installed):
                self.assertFalse(is_newer(candidate, installed))
                self.assertTrue(same_version(candidate, installed))
        self.assertTrue(is_newer("5.1.3", "5, 1, 2, 0"))

    def test_a_lone_comma_is_left_alone_and_dots_win_over_commas(self):
        self.assertEqual(parse_version("1,5"), (1,))
        self.assertEqual(parse_version("1.5, 2"), (1, 5))
        self.assertEqual(clean("v5, 1, 2, 0"), "5.1.2.0")
        self.assertEqual(clean("1,5"), "1,5")

    def test_clean_cuts_the_prefix_only(self):
        for text, shown in [("v8.8.3", "8.8.3"), ("spotube-v5.1.2", "5.1.2"), ("release-2024-03-14", "2024-03-14"),
                            ("7zip-25.01", "25.01"), ("2.51.0.windows.1", "2.51.0.windows.1"), ("nightly", "nightly")]:
            with self.subTest(text=text):
                self.assertEqual(clean(text), shown)

    def test_prerelease_detection(self):
        for text in ("1.2.3-beta.1", "3.13.0rc1", "3.13.0a1", "2.0-preview", "1.0.0-dev", "1.2.3b", "v1.2.3a",
                     "1.2.3-b", "1.2.3b2"):
            self.assertTrue(is_prerelease(text), text)
        for text in ("1.2.3", "1.1.1k", "1.0.2u", "2.51.0.windows.1", "v8.8.3", "1.2.3-windows", "1.2.3-x64", ""):
            self.assertFalse(is_prerelease(text), text)


class RepoRankingTest(unittest.TestCase):
    def best(self, name, publisher, winget_id, urls):
        ranked = rank_repos(make_app(name, publisher=publisher), urls, winget_id)
        return [c.repo for c in ranked]

    def test_real_apps_find_their_own_repo(self):
        cases = [
            ("Git", "The Git Development Community", "Git.Git", [
                "https://gitforwindows.org", "https://github.com/git-for-windows/git/wiki/FAQ",
                "https://github.com/git-for-windows/git/releases/download/v2.51.0.windows.1/Git-2.51.0-64-bit.exe",
                "https://github.com/sponsors/git-for-windows"], "git-for-windows/git"),
            ("7-Zip 25.01 (x64)", "Igor Pavlov", "7zip.7zip", [
                "https://www.7-zip.org/", "https://github.com/ip7z/7zip/releases/download/25.01/7z2501-x64.msi"],
             "ip7z/7zip"),
            ("Notepad++ (64-bit x64)", "Notepad++ Team", "Notepad++.Notepad++", [
                "https://notepad-plus-plus.org/", "https://github.com/notepad-plus-plus/notepad-plus-plus/issues"],
             "notepad-plus-plus/notepad-plus-plus"),
            ("PowerToys (Preview) x64", "Microsoft Corporation", "Microsoft.PowerToys", [
                "https://github.com/microsoft/winget-pkgs", "https://github.com/microsoft/PowerToys/"],
             "microsoft/PowerToys"),
            ("Windows Terminal", "Microsoft Corporation", "Microsoft.WindowsTerminal", [
                "https://github.com/microsoft/terminal"], "microsoft/terminal"),
            ("Telegram Desktop", "Telegram FZ-LLC", "Telegram.TelegramDesktop", [
                "https://github.com/telegramdesktop/tdesktop/releases/download/v5.5.5/tsetup-x64.5.5.5.exe"],
             "telegramdesktop/tdesktop"),
            ("GitHub Desktop", "GitHub, Inc.", "GitHub.GitHubDesktop", ["https://github.com/desktop/desktop/issues"],
             "desktop/desktop"),
            ("Brave", "Brave Software Inc", "Brave.Brave", ["https://github.com/brave/brave-browser/issues"],
             "brave/brave-browser"),
        ]
        for name, publisher, winget_id, urls, repo in cases:
            with self.subTest(app=name):
                self.assertEqual(self.best(name, publisher, winget_id, urls)[:1], [repo])

    def test_a_build_number_in_the_name_and_no_publisher_do_not_hide_the_repo(self):
        app = make_app("Hiddify version 4.1.1+40101", publisher="")
        self.assertEqual([c.repo for c in rank_repos(app, ["https://github.com/hiddify/hiddify-app"])],
                         ["hiddify/hiddify-app"])
        app = make_app("Spotube version 3.9.0+38", publisher="")
        self.assertEqual([c.repo for c in rank_repos(app, ["https://github.com/KRTirtho/spotube/releases"])],
                         ["KRTirtho/spotube"])

    def test_docs_sponsor_and_other_project_links_are_not_the_app(self):
        urls = ["https://github.com/sponsors/audacity", "https://github.com/orgs/audacity/people",
                "https://github.com/audacity/audacity.github.io", "https://github.com/audacity/audacity-docs",
                "https://github.com/other/unrelated/issues", "https://gist.github.com/a/b",
                "https://api.github.com/repos/audacity/audacity/releases"]
        self.assertEqual(self.best("Audacity", "Audacity Team", "Audacity.Audacity", urls), [])
        self.assertEqual(self.best("Audacity", "Audacity Team", "Audacity.Audacity",
                                   [*urls, "https://github.com/audacity/audacity/releases/latest"]), ["audacity/audacity"])

    def test_same_publisher_different_project_is_rejected(self):
        for urls in (["https://github.com/microsoft/vscode-docs", "https://github.com/microsoft/vscode/issues"],
                     ["https://github.com/mozilla/gecko-dev"]):
            self.assertEqual(self.best("Mozilla Firefox", "Mozilla", "Mozilla.Firefox", urls), [])
        self.assertEqual(self.best("Visual Studio Code", "Microsoft Corporation", "Microsoft.VisualStudioCode",
                                   ["https://github.com/microsoft/vscode/issues"]), [])

    def test_a_sibling_platform_repo_loses_to_the_desktop_one(self):
        urls = ["https://github.com/videolan/vlc-android/issues", "https://github.com/videolan/vlc"]
        self.assertEqual(self.best("VLC media player", "VideoLAN", "VideoLAN.VLC", urls), ["videolan/vlc"])

    def test_release_download_repo_ranks_first_and_both_stay_candidates(self):
        urls = ["https://github.com/git/git/blob/master/COPYING",
                "https://github.com/git-for-windows/git/releases/download/v2.51.0.windows.1/Git-2.51.0-64-bit.exe"]
        self.assertEqual(self.best("Git", "The Git Development Community", "Git.Git", urls),
                         ["git-for-windows/git", "git/git"])

    def test_an_app_without_latin_letters_is_matched_by_publisher_download_or_the_only_repo(self):
        telegram = "https://github.com/telegramdesktop/tdesktop"
        cases = [
            ("the only repo the links name", TELEGRAM_FA, "Telegram FZ-LLC", [f"{telegram}/releases"],
             ["telegramdesktop/tdesktop"]),
            ("the same repo linked twice is still one", TELEGRAM_FA, "Telegram FZ-LLC",
             [f"{telegram}/releases", f"{telegram}/issues"], ["telegramdesktop/tdesktop"]),
            ("a download from the publisher's owner wins", "テレグラム", "Acme Inc",
             ["https://github.com/other/thing", "https://github.com/acme/desk/releases/download/v1/setup.exe"],
             ["acme/desk"]),
            ("two repos and no evidence", TELEGRAM_FA, "Telegram FZ-LLC", [telegram, "https://github.com/foo/bar"], []),
            ("a download from an unrelated owner is not enough", TELEGRAM_FA, "Telegram FZ-LLC",
             [telegram, "https://github.com/foo/bar/releases/download/v1/x.exe"], []),
            ("docs are not the app even when alone", TELEGRAM_FA, "Acme", ["https://github.com/acme/docs"], []),
            ("another platform's repo is not the app", TELEGRAM_FA, "Acme", ["https://github.com/acme/acme-android"], []),
            ("only the usable repo counts as alone", TELEGRAM_FA, "Acme",
             ["https://github.com/acme/acme-docs", "https://github.com/acme/desk"], ["acme/desk"]),
        ]
        for label, name, publisher, urls, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.best(name, publisher, None, urls), expected)

    def test_a_winget_id_gives_a_non_latin_app_its_name_back(self):
        urls = ["https://github.com/telegramdesktop/tdesktop/releases", "https://github.com/foo/bar"]
        self.assertEqual(self.best(TELEGRAM_FA, "Telegram FZ-LLC", "Telegram.TelegramDesktop", urls),
                         ["telegramdesktop/tdesktop"])

    def test_app_installer_is_built_in_winget_cli(self):
        download = ("https://github.com/microsoft/winget-cli/releases/download/v1.7.10861/"
                    "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle")
        name, publisher, winget_id = "App Installer", "Microsoft Corporation", "Microsoft.AppInstaller"
        self.assertEqual(self.best(name, publisher, winget_id, [download]), ["microsoft/winget-cli"])
        self.assertEqual(self.best(name, publisher, winget_id, ["https://github.com/microsoft/winget-cli"]), [],
                         "a homepage link alone is not proof")
        self.assertEqual(self.best("Foo", "Foo Inc", "Foo.Foo", ["https://github.com/foo/winget-helper/issues"]), [])
        self.assertEqual(self.best("Foo", "Foo Inc", "Foo.Foo",
                                   ["https://github.com/other/winget-foo/releases/download/v1/x.exe"]), [],
                         "the penalty is lifted, but the repo still has to look like the app")
        self.assertEqual(self.best("Winget Helper", "Foo Inc", "Foo.winget-helper",
                                   ["https://github.com/foo/winget-helper"]), ["foo/winget-helper"],
                         "the app's own id says winget")

    def test_no_github_link_no_repo(self):
        self.assertEqual(self.best("Python 3.12.4 (64-bit)", "Python Software Foundation", "Python.Python.3.12",
                                   ["https://www.python.org/"]), [])

    def test_repo_from_urls_for_a_typed_link(self):
        for text, repo in [("owner/repo", None), ("github.com/owner/repo", "owner/repo"),
                           ("https://github.com/owner/repo.git", "owner/repo"),
                           ("https://www.github.com/Owner/Repo/releases", "Owner/Repo"),
                           ("https://github.com/sponsors/owner", None), ("https://gist.github.com/o/abc", None),
                           ("https://github.com/microsoft/winget-pkgs/pull/1", None)]:
            with self.subTest(text=text):
                self.assertEqual(repo_from_urls([text]), repo)
        self.assertEqual(repo_from_urls(["https://github.com/a/home", "https://github.com/b/c/releases/download/v1/x.exe"]),
                         "b/c")


class DiscoverTest(unittest.TestCase):
    URLS = ["https://github.com/foo-org/foo", "https://github.com/foo-win/foo"]

    def releases(self):
        return {"foo-org/foo": [api_release("v1.0", ["foo-1.0.tar.gz", "foo-1.0-linux-x64.zip"])],
                "foo-win/foo": [api_release("v1.0", ["Foo-Setup-1.0-x64.exe"])]}

    def test_the_repo_with_a_windows_installer_wins_a_tie(self):
        gh = github_with(self.releases())
        self.assertEqual(gh.discover(make_app("Foo", publisher="Foo"), self.URLS, None, "x64"), "foo-win/foo")
        self.assertEqual(sorted(gh.calls), ["foo-org/foo", "foo-win/foo"])
        gh.discover(make_app("Foo", publisher="Foo"), self.URLS, None, "x64")
        self.assertEqual(len(gh.calls), 2, "the second look is served from the cache")

    def test_one_candidate_costs_no_api_call(self):
        gh = github_with({})
        self.assertEqual(gh.discover(make_app("Foo", publisher="Foo"), self.URLS[:1], None, "x64"), "foo-org/foo")
        self.assertEqual(gh.calls, [])

    def test_no_candidate_is_none(self):
        self.assertIsNone(github_with({}).discover(make_app("Foo"), ["https://example.com/foo"], None, "x64"))

    def test_rate_limit_falls_back_to_the_text_ranking(self):
        gh = GitHub(FakeCache(), None)

        def limited(path, etag):
            raise RateLimited(None)

        gh._request = limited
        self.assertEqual(gh.discover(make_app("Foo", publisher="Foo"), self.URLS, None, "x64"), "foo-org/foo")


class CheckTest(unittest.TestCase):
    def check(self, releases, app, **kwargs):
        gh = github_with({"o/r": releases}, **kwargs.pop("client", {}))
        return gh.check("o/r", app, "x64", **kwargs)

    def test_git_for_windows(self):
        files = ["Git-2.51.0-64-bit.exe", "Git-2.51.0-arm64.exe", "Git-2.51.0-32-bit.exe",
                 "PortableGit-2.51.0-64-bit.7z.exe", "MinGit-2.51.0-64-bit.zip", "Git-2.51.0-64-bit.tar.bz2"]
        releases = [api_release("v2.51.0.windows.1", files), api_release("v2.50.1.windows.1", [])]
        res = self.check(releases, make_app("Git", "2.50.1", "The Git Development Community"))
        self.assertEqual((res.release.version, res.asset.name), ("2.51.0.windows.1", "Git-2.51.0-64-bit.exe"))
        self.assertIsNone(self.check(releases, make_app("Git", "2.51.0")).asset, "installed 2.51.0 is windows.1")
        newer = [api_release("v2.51.0.windows.2", files)] + releases
        self.assertEqual(self.check(newer, make_app("Git", "2.51.0")).asset.name, "Git-2.51.0-64-bit.exe")

    def test_notepad_plus_plus(self):
        files = ["npp.8.8.3.Installer.x64.exe", "npp.8.8.3.Installer.exe", "npp.8.8.3.Installer.arm64.exe",
                 "npp.8.8.3.portable.x64.zip", "npp.8.8.3.Installer.x64.exe.sig"]
        res = self.check([api_release("v8.8.3", files)], make_app("Notepad++ (64-bit x64)", "8.8.2"))
        self.assertEqual(res.asset.name, "npp.8.8.3.Installer.x64.exe")

    def test_seven_zip_follows_the_installed_family(self):
        files = ["7z2501-x64.exe", "7z2501.exe", "7z2501-arm64.exe", "7z2501-x64.msi", "7z2501-extra.7z", "7zr.exe",
                 "7z2501-linux-x64.tar.xz", "7z2501-src.7z"]
        releases = [api_release("25.01", files)]
        self.assertEqual(self.check(releases, make_app("7-Zip", "24.09", install_kind="nullsoft")).asset.name,
                         "7z2501-x64.exe")
        self.assertEqual(self.check(releases, make_app("7-Zip", "24.09", install_kind="msi", is_msi=True)).asset.name,
                         "7z2501-x64.msi")
        self.assertIsNone(self.check(releases, make_app("7-Zip", "25.1")).asset, "25.01 == 25.1")

    def test_prerelease_tags_are_skipped_unless_asked_for(self):
        releases = [api_release("v2.0.0-beta.1", ["demo-setup.exe"]), api_release("v1.9.0rc1", ["demo-setup.exe"]),
                    api_release("v1.5.0", ["demo-setup.exe"]), api_release("v3.0.0", ["demo-setup.exe"], prerelease=True)]
        self.assertEqual(self.check(releases, make_app(version="1.4.0")).release.version, "1.5.0")
        res = self.check(releases, make_app(version="1.4.0"), client={"include_prereleases": True})
        self.assertEqual(res.release.version, "3.0.0")

    def test_a_beta_installed_is_offered_its_release(self):
        res = self.check([api_release("v1.2.3", ["demo-setup.exe"])], make_app(version="1.2.3-beta.1"))
        self.assertEqual(res.release.version, "1.2.3")

    def test_a_beta_tag_without_the_prerelease_flag_is_not_an_update(self):
        releases = [api_release("v1.2.3b", ["demo-setup.exe"]), api_release("v1.2.3", ["demo-setup.exe"])]
        self.assertIsNone(self.check(releases[:1], make_app(version="1.2.3")).release)
        self.assertEqual(self.check(releases, make_app(version="1.2.3b")).release.version, "1.2.3")
        self.assertIsNone(self.check(releases[:1], make_app(version="1.2.2")).release,
                          "a beta is not offered unless pre-releases are asked for")
        res = self.check(releases[:1], make_app(version="1.2.2"), client={"include_prereleases": True})
        self.assertEqual(res.release.version, "1.2.3b")

    def test_an_openssl_style_patch_letter_is_an_update(self):
        release = [api_release("1.1.1k", ["demo-setup.exe"])]
        self.assertEqual(self.check(release, make_app(version="1.1.1")).release.version, "1.1.1k")
        self.assertIsNone(self.check(release, make_app(version="1.1.1k")).asset)

    def test_other_numbering_scheme_is_not_compared(self):
        releases = [api_release("2026.11.02", ["demo-setup.exe"]), api_release("v8.0.2", ["demo-setup.exe"])]
        res = self.check(releases, make_app(version="8.0.1"))
        self.assertEqual(res.release.version, "8.0.2")
        only_dates = self.check([api_release("2026.11.02", ["demo-setup.exe"])], make_app(version="8.0.1"))
        self.assertIsNone(only_dates.release)
        self.assertEqual(only_dates.incomparable, "2026.11.02")

    def test_newer_without_installer_reports_the_version(self):
        res = self.check([api_release("v2.0", ["demo.tar.gz"])], make_app(version="1.0"))
        self.assertEqual((res.latest, res.asset), ("2.0", None))

    def test_the_hint_from_winget_picks_the_same_kind_of_file(self):
        files = ["Demo-2.0-x64.msi", "Demo-2.0-x64-setup.exe"]
        res = self.check([api_release("v2.0", files)], make_app("Demo", "1.0"),
                         hint_url="https://github.com/o/r/releases/download/v1.0/Demo-1.0-x64.msi", hint_type="wix")
        self.assertEqual((res.asset.name, res.installer_type), ("Demo-2.0-x64.msi", "wix"))


class PickAssetTest(unittest.TestCase):
    def name(self, files, app, arch="x64", hint=None):
        picked = pick_asset(assets(*files), app, arch, hint)
        return picked.name if picked else None

    def test_installer_family_of_the_installed_copy(self):
        both = ["Foo-1.2-x64.msi", "Foo-Setup-1.2-x64.exe"]
        self.assertEqual(self.name(both, make_app("Foo", install_kind="inno")), "Foo-Setup-1.2-x64.exe")
        self.assertEqual(self.name(both, make_app("Foo", install_kind="burn")), "Foo-Setup-1.2-x64.exe")
        self.assertEqual(self.name(both, make_app("Foo", install_kind="msi", is_msi=True)), "Foo-1.2-x64.msi")
        self.assertEqual(self.name(["Foo-1.2.msix", "Foo-Setup-1.2.exe"], make_app("Foo", install_kind="msix")), "Foo-1.2.msix")

    def test_unknown_family_keeps_the_old_preference(self):
        both = ["Foo-1.2-x64.msi", "Foo-Setup-1.2-x64.exe"]
        self.assertEqual(self.name(both, make_app("Foo")), "Foo-Setup-1.2-x64.exe")
        self.assertEqual(self.name(both, make_app("Foo", is_msi=True)), "Foo-1.2-x64.msi")

    def test_what_the_release_says_about_its_exe(self):
        squirrel = assets("FooSetup.exe", "RELEASES", "Foo-1.2.0-full.nupkg", "Foo-1.2.0-delta.nupkg")
        electron_nsis = assets("Foo Setup 1.2.0.exe", "Foo Setup 1.2.0.exe.blockmap", "latest.yml")
        self.assertEqual(_asset_kind(squirrel[0], squirrel), "squirrel")
        self.assertEqual(_asset_kind(electron_nsis[0], electron_nsis), "nullsoft")
        self.assertEqual(_asset_kind(Asset("Foo.msi", "", 1, None), []), "msi")
        self.assertEqual(_asset_kind(Asset("Foo.msixbundle", "", 1, None), []), "msix")
        self.assertEqual(_asset_kind(Asset("Foo.exe", "", 1, None), []), "")

    def test_same_exe_framework_beats_an_unknown_one(self):
        release = assets("Foo-Installer-1.2.exe", "Foo Setup 1.2.exe", "Foo Setup 1.2.exe.blockmap")
        picked = pick_asset(release, make_app("Foo", install_kind="nullsoft"), "x64")
        self.assertEqual(picked.name, "Foo Setup 1.2.exe")
        picked = pick_asset(assets("FooSetup.exe", "RELEASES", "Foo-1.0-full.nupkg"), make_app("Foo", install_kind="squirrel"), "x64")
        self.assertEqual(picked.name, "FooSetup.exe")

    def test_architecture(self):
        files = ["Foo-1.2-x86.exe", "Foo-1.2-x64.exe", "Foo-1.2-arm64.exe"]
        self.assertEqual(self.name(files, make_app("Foo", arch="x64")), "Foo-1.2-x64.exe")
        self.assertEqual(self.name(files, make_app("Foo", arch="x86")), "Foo-1.2-x86.exe")
        self.assertEqual(self.name(files, make_app("Foo", arch="x64"), arch="arm64"), "Foo-1.2-arm64.exe")
        self.assertEqual(self.name(["Foo-1.2-arm64.exe"], make_app("Foo")), None, "an ARM-only build is never offered to x64")

    def test_per_user_or_per_machine(self):
        power = ["PowerToysSetup-0.95.1-x64.exe", "PowerToysUserSetup-0.95.1-x64.exe"]
        self.assertEqual(self.name(power, make_app("PowerToys", scope="machine")), "PowerToysSetup-0.95.1-x64.exe")
        self.assertEqual(self.name(power, make_app("PowerToys", scope="user")), "PowerToysUserSetup-0.95.1-x64.exe")
        wide = ["Foo-1.2-user-x64.exe", "Foo-1.2-all-users-x64.exe"]
        self.assertEqual(self.name(wide, make_app("Foo", scope="machine")), "Foo-1.2-all-users-x64.exe")
        self.assertEqual(self.name(wide, make_app("Foo", scope="user")), "Foo-1.2-user-x64.exe")

    def test_admin_and_system_in_a_name_say_nothing_about_the_scope(self):
        admin = ["pgadmin4-8.0-admin-x64.exe", "pgadmin4-8.0-x64.exe"]
        self.assertEqual(self.name(admin[::-1], make_app("pgAdmin 4", scope="machine")), "pgadmin4-8.0-x64.exe")
        for scope in ("machine", "user"):
            with self.subTest(scope=scope):
                self.assertEqual(self.name(admin, make_app("pgAdmin 4", scope=scope)), admin[0],
                                 "the scope is not what decides between them")
        self.assertEqual(self.name(["Foo-x64.exe", "Foo-system-x64.exe"], make_app("Foo", scope="machine")), "Foo-x64.exe")

    def test_machine_wide_words_still_count(self):
        for word in ("allusers", "all-users", "perMachine", "per_machine", "machine", "system-wide", "SystemWide"):
            wide = [f"Foo-1.2-{word}-x64.exe", "Foo-1.2-user-x64.exe"]
            with self.subTest(word=word):
                self.assertEqual(self.name(wide, make_app("Foo", scope="machine")), wide[0])
                self.assertEqual(self.name(wide, make_app("Foo", scope="user")), wide[1])

    def test_portable_symbols_and_archives_are_never_picked(self):
        junk = ["Foo-portable.exe", "Foo-symbols.zip", "Foo.zip", "Foo-debug.exe", "Foo-1.2.7z", "Foo-pdb.exe",
                "Foo-src.exe", "Foo-linux.exe", "Foo-macos.exe", "Foo-uninstall.exe", "Foo-1.2.exe.blockmap"]
        self.assertIsNone(self.name(junk, make_app("Foo")))
        self.assertEqual(self.name([*junk, "Foo-Setup.exe"], make_app("Foo")), "Foo-Setup.exe")

    def test_runtimes_shipped_beside_the_setup_lose(self):
        self.assertEqual(self.name(["vc_redist.x64.exe", "Foo-Setup-x64.exe"], make_app("Foo")), "Foo-Setup-x64.exe")
        self.assertEqual(self.name(["windowsdesktop-runtime-8.0.5-win-x64.exe", "app-x64.exe"], make_app("Foo")), "app-x64.exe")

    def test_the_winget_hint_still_decides(self):
        files = ["Foo-2.0-x64.msi", "Foo-2.0-x64-setup.exe"]
        self.assertEqual(self.name(files, make_app("Foo"), hint="Foo-1.0-x64.msi"), "Foo-2.0-x64.msi")


class WingetMatchTest(unittest.TestCase):
    def vc_apps(self):
        return [
            make_app("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.40.33810", "14.40.33810.0",
                     "Microsoft Corporation", reg_key=r"HKLM64\{A1}"),
            make_app("Microsoft Visual C++ 2015-2022 Redistributable (x86) - 14.40.33810", "14.40.33810.0",
                     "Microsoft Corporation", arch="x86", reg_key=r"HKLM32\{A2}"),
            make_app("Microsoft Visual C++ 2013 Redistributable (x64) - 12.0.40664", "12.0.40664.0",
                     "Microsoft Corporation", reg_key=r"HKLM64\{A3}"),
        ]

    def test_visual_cpp_redistributables_with_truncated_names(self):
        rows = [WingetRow("Microsoft Visual C++ 2015-2022 Redistrib…", "Microsoft.VCRedist.2015+.x86", "14.40.33810.0", ""),
                WingetRow("Microsoft Visual C++ 2015-2022 Redistrib…", "Microsoft.VCRedist.2015+.x64", "14.40.33810.0", "14.42.34433.0"),
                WingetRow("Microsoft Visual C++ 2013 Redistributable (x64) - 12.0.40664", "Microsoft.VCRedist.2013.x64", "12.0.40664.0", "")]
        matched = match_rows(self.vc_apps(), rows)
        self.assertEqual({k: r.id for k, r in matched.items()},
                         {r"HKLM64\{A1}": "Microsoft.VCRedist.2015+.x64", r"HKLM32\{A2}": "Microsoft.VCRedist.2015+.x86",
                          r"HKLM64\{A3}": "Microsoft.VCRedist.2013.x64"})

    def test_dotnet_runtimes_with_the_same_truncated_prefix_are_told_apart_by_version(self):
        apps = [make_app("Microsoft .NET Runtime - 8.0.5 (x64)", "8.0.5.33521", reg_key=r"HKLM64\{N8}"),
                make_app("Microsoft .NET Runtime - 6.0.29 (x64)", "6.0.29.33316", reg_key=r"HKLM64\{N6}")]
        rows = [WingetRow("Microsoft .NET Runtime - 6.0.29…", "Microsoft.DotNet.Runtime.6", "6.0.29.33316", ""),
                WingetRow("Microsoft .NET Runtime - 8.0.5…", "Microsoft.DotNet.Runtime.8", "8.0.5.33521", "8.0.8")]
        matched = match_rows(apps, rows)
        self.assertEqual(matched[r"HKLM64\{N8}"].id, "Microsoft.DotNet.Runtime.8")
        self.assertEqual(matched[r"HKLM64\{N6}"].id, "Microsoft.DotNet.Runtime.6")

    def test_equal_truncated_names_use_the_version_to_pick(self):
        apps = [make_app("Microsoft .NET Host FX Resolver - 8.0.5 (x64) long name", "8.0.5.1", reg_key=r"HKLM64\{H8}"),
                make_app("Microsoft .NET Host FX Resolver - 6.0.29 (x64) long name", "6.0.29.1", reg_key=r"HKLM64\{H6}")]
        rows = [WingetRow("Microsoft .NET Host FX Resolver - …", "Microsoft.DotNet.HostingBundle.8", "8.0.5.1", ""),
                WingetRow("Microsoft .NET Host FX Resolver - …", "Microsoft.DotNet.HostingBundle.6", "6.0.29.1", "")]
        matched = match_rows(apps, rows)
        self.assertEqual(matched[r"HKLM64\{H8}"].id, "Microsoft.DotNet.HostingBundle.8")
        self.assertEqual(matched[r"HKLM64\{H6}"].id, "Microsoft.DotNet.HostingBundle.6")

    def test_per_user_and_per_machine_copies_of_one_app(self):
        machine = make_app("Foo", "1.0", scope="machine", reg_key=r"HKLM64\Foo")
        user = make_app("Foo", "2.0", scope="user", reg_key=r"HKCU\Foo")
        one = match_rows([user, machine], [WingetRow("Foo", "Foo.Foo", "2.0", "")])
        self.assertEqual(list(one), [r"HKCU\Foo"], "the version says which copy winget listed")
        two = match_rows([machine, user], [WingetRow("Foo", "Foo.Foo", "1.0", ""), WingetRow("Foo", "Foo.Foo", "2.0", "")])
        self.assertEqual({k: r.version for k, r in two.items()}, {r"HKLM64\Foo": "1.0", r"HKCU\Foo": "2.0"})

    def test_one_row_is_never_given_to_two_apps(self):
        a = make_app("Foo", "1.0", scope="machine", reg_key=r"HKLM64\Foo")
        b = make_app("Foo", "1.0", scope="user", reg_key=r"HKCU\Foo")
        row = WingetRow("Foo", "Foo.Foo", "1.0", "1.1")
        for apps in ([a, b], [b, a]):
            matched = match_rows(apps, [row])
            self.assertEqual(list(matched), [r"HKLM64\Foo"], "the machine-wide copy takes the single row")
        rows = [WingetRow("Foo", "Foo.Foo", "1.0", ""), WingetRow("Foo", "Foo.Foo", "1.0", "")]
        matched = match_rows([a, b], rows)
        self.assertEqual(len(matched), 2)
        self.assertEqual(len({id(r) for r in matched.values()}), 2)

    def test_name_and_version_noise_falls_back_to_the_normalised_name(self):
        apps = [make_app("Foo 3.2.1 (64-bit)", "3.2.1", reg_key=r"HKLM64\Foo"), make_app("Foo Pro", "1.0", reg_key=r"HKLM64\FooPro")]
        matched = match_rows(apps, [WingetRow("Foo", "Foo.Foo", "3.2.1", "")])
        self.assertEqual(list(matched), [r"HKLM64\Foo"])

    def test_different_apps_with_a_shared_word_do_not_match(self):
        apps = [make_app("Docker Desktop", "4.0"), make_app("Desktop Docker Helper", "1.0", reg_key=r"HKLM64\Helper")]
        matched = match_rows(apps, [WingetRow("Docker", "Docker.Docker", "4.0", "")])
        self.assertEqual(matched, {})

    def test_an_arp_product_code_id_names_the_registry_entry(self):
        a = make_app("Same Name", "1.0", scope="machine", reg_key=r"HKLM64\{11111111-1111-1111-1111-111111111111}")
        b = make_app("Same Name", "1.0", scope="user", reg_key=r"HKCU\{22222222-2222-2222-2222-222222222222}")
        rows = [WingetRow("Whatever", r"ARP\User\X64\{22222222-2222-2222-2222-222222222222}", "9", ""),
                WingetRow("Whatever", r"ARP\Machine\X86\{33333333-3333-3333-3333-333333333333}", "9", "")]
        matched = match_rows([a, b], rows)
        self.assertEqual(list(matched), [b.reg_key])


WINGET_SHOW = """\
Found Git [Git.Git]
Version: 2.51.0
Publisher: The Git Development Community
Publisher Url: https://gitforwindows.org/
Homepage: https://gitforwindows.org/
License: GNU General Public License version 2
License Url: https://github.com/git-for-windows/git/blob/main/COPYING
Release Notes Url: https://github.com/git-for-windows/git/releases/tag/v2.51.0.windows.1
Documentation:
  Wiki: https://github.com/git-for-windows/git/wiki
Installer:
  Installer Type: inno
  Installer Url: https://github.com/git-for-windows/git/releases/download/v2.51.0.windows.1/Git-2.51.0-64-bit.exe
  Installer SHA256: abc
"""


class WingetShowTest(unittest.TestCase):
    def show(self, text):
        with mock.patch.object(winget, "_run", return_value=text.splitlines()):
            return winget.show("Git.Git")

    def test_collects_every_github_link_and_the_installer(self):
        meta = self.show(WINGET_SHOW)
        self.assertEqual(meta["v"], winget.META_VERSION)
        self.assertEqual(meta["urls"], [
            "https://github.com/git-for-windows/git/blob/main/COPYING",
            "https://github.com/git-for-windows/git/releases/tag/v2.51.0.windows.1",
            "https://github.com/git-for-windows/git/wiki",
            "https://github.com/git-for-windows/git/releases/download/v2.51.0.windows.1/Git-2.51.0-64-bit.exe"])
        self.assertEqual((meta["installer_type"], meta["homepage"], meta["version"]), ("inno", "https://gitforwindows.org/", "2.51.0"))
        self.assertTrue(meta["installer_url"].endswith("Git-2.51.0-64-bit.exe"))

    def test_localised_labels_still_yield_the_links(self):
        text = WINGET_SHOW.replace("Homepage", "Startseite").replace("License Url", "Lizenz-URL").replace("Installer Url", "Installationsprogramm-URL")
        meta = self.show(text)
        self.assertEqual(len(meta["urls"]), 4)
        self.assertTrue(meta["installer_url"].endswith("Git-2.51.0-64-bit.exe"))

    def test_links_to_other_hosts_and_duplicates_are_not_kept(self):
        meta = self.show("Homepage: https://example.com/a\nLicense Url: https://github.com/o/r/blob/main/LICENSE\n"
                         "Wiki: https://github.com/o/r/blob/main/LICENSE.\nTools: https://raw.githubusercontent.com/o/r/main/x")
        self.assertEqual(meta["urls"], ["https://github.com/o/r/blob/main/LICENSE"])


class FakeSettings:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}

    def get(self, key, default=None):
        return default

    def override(self, ident):
        return copy.deepcopy(self.overrides.get(ident, {}))

    def is_ignored(self, ident):
        return False


class RecordingGitHub:
    def __init__(self, found="auto/detected"):
        self.found, self.discovered, self.checked = found, [], []

    def discover(self, app, urls, winget_id=None, arch=None):
        self.discovered.append((list(urls), winget_id, arch))
        return self.found

    def check(self, repo, app, arch, hint_url=None, hint_type=None, force=False):
        self.checked.append(repo)
        from upstream.github import CheckResult
        return CheckResult(None, None, None, None)


class ScannerTest(unittest.TestCase):
    def scanner(self, overrides=None, github=None):
        sc = Scanner(FakeSettings(overrides), FakeCache())
        sc._gh = github or RecordingGitHub()
        return sc

    def test_a_typed_repo_always_wins(self):
        app = make_app("Foo", urls=["https://github.com/foo/foo"])
        sc = self.scanner({app.ident: {"github": "me/mine"}})
        status = AppStatus(app)
        sc.resolve(status, False)
        self.assertEqual(status.repo, "me/mine")
        self.assertEqual(sc._gh.discovered, [])
        self.assertEqual(sc._gh.checked, ["me/mine"])

    def test_discovery_gets_the_registry_links_and_the_winget_manifest_links(self):
        app = make_app("Foo", urls=["https://foo.example/", "https://github.com/foo/foo/issues"])
        sc = self.scanner()
        sc.cache.put("winget_meta", "Foo.Foo", {"v": winget.META_VERSION, "urls": ["https://github.com/foo/foo/releases"]})
        status = AppStatus(app, winget_id="Foo.Foo")
        sc.resolve(status, False)
        urls, winget_id, arch = sc._gh.discovered[0]
        self.assertEqual(urls, ["https://foo.example/", "https://github.com/foo/foo/issues", "https://github.com/foo/foo/releases"])
        self.assertEqual((winget_id, status.repo), ("Foo.Foo", "auto/detected"))

    def test_the_winget_installer_url_joins_the_links_given_to_discovery(self):
        installer = "https://github.com/foo/foo/releases/download/v2/Foo-2-x64.exe"
        for label, meta_installer, expected_tail in [
            ("a GitHub download is appended", installer, [installer]),
            ("a link already among the links is not repeated", "https://github.com/foo/foo/wiki", []),
            ("another host is left out", "https://cdn.example.com/Foo-2-x64.exe", []),
            ("no installer URL", None, []),
        ]:
            with self.subTest(label):
                app = make_app("Foo", urls=["https://foo.example/"])
                sc = self.scanner()
                sc.cache.put("winget_meta", "Foo.Foo", {"v": winget.META_VERSION, "installer_url": meta_installer,
                                                        "urls": ["https://github.com/foo/foo/wiki"]})
                sc.resolve(AppStatus(app, winget_id="Foo.Foo"), False)
                self.assertEqual(sc._gh.discovered[0][0],
                                 ["https://foo.example/", "https://github.com/foo/foo/wiki", *expected_tail])

    def test_app_installer_finds_winget_cli_through_the_installer_url_alone(self):
        installer = ("https://github.com/microsoft/winget-cli/releases/download/v1.7.10861/"
                     "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle")
        sc = self.scanner(github=github_with({"microsoft/winget-cli": []}))
        sc.cache.put("winget_meta", "Microsoft.AppInstaller",
                     {"v": winget.META_VERSION, "urls": [], "installer_url": installer})
        app = make_app("App Installer", "1.22", "Microsoft Corporation", reg_key=r"WINGET\Microsoft.AppInstaller")
        status = AppStatus(app, winget_id="Microsoft.AppInstaller")
        sc.resolve(status, False)
        self.assertEqual(status.repo, "microsoft/winget-cli")
        self.assertEqual(sc._gh.calls, ["microsoft/winget-cli"], "one release listing, none to choose a repo")

    def test_no_repo_means_no_github_check(self):
        sc = self.scanner(github=RecordingGitHub(found=None))
        status = AppStatus(make_app("Foo"))
        sc.resolve(status, False)
        self.assertIsNone(status.repo)
        self.assertEqual(sc._gh.checked, [])

    def test_an_uncomparable_latest_is_explained(self):
        from upstream.github import CheckResult
        gh = RecordingGitHub()
        gh.check = lambda *a, **k: CheckResult(None, None, None, None, "2026.11.02")
        sc = self.scanner(github=gh)
        status = AppStatus(make_app("Foo", "8.0.1"))
        sc.resolve(status, False)
        self.assertEqual(status.note, "GitHub's version {version} can't be compared with the installed one")
        self.assertEqual(status.note_args, {"version": "2026.11.02"})

    def test_cached_winget_manifests_in_the_old_shape_are_read_again(self):
        sc = self.scanner()
        sc.cache.put("winget_meta", "Old.Entry", {"repo": "x/y", "installer_url": None})
        sc.cache.put("winget_meta", "New.Entry", {"v": winget.META_VERSION, "urls": []})
        statuses = [AppStatus(make_app("Old"), winget_id="Old.Entry"), AppStatus(make_app("New"), winget_id="New.Entry")]
        with mock.patch.object(scanner.winget, "show", return_value={"v": winget.META_VERSION, "urls": []}) as show:
            sc._refresh_meta(statuses, lambda *a: None)
        self.assertEqual([c.args[0] for c in show.call_args_list], ["Old.Entry"])


class TranslationTest(unittest.TestCase):
    def test_the_new_note_has_a_persian_entry_with_the_same_placeholders(self):
        fields = lambda t: {name for _, name, _, _ in string.Formatter().parse(t) if name}
        self.assertTrue(detection.FA)
        for english, persian in detection.FA.items():
            self.assertEqual(fields(english), fields(persian), english)


if __name__ == "__main__":
    unittest.main()
