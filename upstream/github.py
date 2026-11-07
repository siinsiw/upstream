"""GitHub Releases: find a project's repo, its newest release, and the Windows
installer asset in it that fits the installed app."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import cmp_to_key

from . import net
from .inventory import InstalledApp
from .versioning import clean, compare, is_newer, is_prerelease, parse_version

log = logging.getLogger("upstream.github")

API = "https://api.github.com"
USER_AGENT = "Upstream-Updater/1.0"
CREATE_NO_WINDOW = 0x08000000
CACHE_FRESH_SECONDS = 15 * 60

# github.com or www.github.com only (not api., gist. or raw.), then owner, repo and the rest of the path.
_GITHUB_URL = re.compile(
    r"(?<![\w.-])(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)([^\s?#\"'<>]*)",
    re.I)
_NOT_REPO_OWNERS = {
    "sponsors", "orgs", "settings", "marketplace", "features", "topics", "apps", "login",
    "about", "pricing", "enterprise", "notifications", "users", "site", "collections", "search",
    "repos", "gist", "explore", "trending", "security", "readme", "contact", "customer-stories",
    "solutions", "resources", "join", "new", "pulls", "issues", "codespaces", "account",
}

_INSTALLABLE = (".msi", ".exe", ".msix", ".msixbundle", ".appx", ".appxbundle")
_NOT_WINDOWS = re.compile(r"linux|ubuntu|debian|fedora|centos|darwin|macos|\bmac\b|osx|apple|android|\bios\b|freebsd", re.I)
_NOT_INSTALLER = re.compile(r"portable|symbols?|debug|\bpdb\b|sources?\b|\bsrc\b|uninstall|delta|blockmap", re.I)
# Runtimes a project ships beside its own setup; installing one of them is never the update.
_PREREQUISITE = re.compile(r"vc_?redist|dotnet-|windowsdesktop-runtime|webview2|directx", re.I)
_MACHINE_WIDE = re.compile(r"all[-_ ]?users|per[-_ ]?machine|(?<![a-z])(?:machine|system|admin)(?![a-z])")
# Many projects publish betas without ticking GitHub's "pre-release" box.
_PRE_TAG = re.compile(r"(?<![a-z])(alpha|beta|rc|pre|preview|nightly|dev|canary|insiders?|snapshot)(?![a-z])", re.I)

# --- which repository belongs to an app ---
_NOISE = re.compile(
    r"\bv?\d+(?:[.\-_]\d+)+(?:\+\d+|\(\d+\))?|\bversion\b|\(?\b(?:x64|x86|amd64|arm64|64-bit|32-bit|win64|win32)\b\)?",
    re.I)
_COMPANY_WORDS = {
    "inc", "llc", "ltd", "gmbh", "corp", "corporation", "co", "company", "limited", "plc", "foundation", "team",
    "project", "software", "technologies", "technology", "the", "and", "org", "community", "development",
    "developers", "contributors", "group", "systems",
}
_GENERIC_WORDS = {
    "the", "for", "and", "of", "app", "apps", "desktop", "client", "windows", "win", "setup", "installer",
    "install", "edition", "free", "portable", "version", "bit", "en", "us", "with",
}
# A repository named like this is about the app, not the app itself.
_OTHER_PRODUCT = {
    "android", "ios", "iphone", "macos", "mac", "osx", "linux", "web", "website", "docs", "doc", "documentation",
    "wiki", "blog", "plugin", "plugins", "extension", "extensions", "addon", "addons", "theme", "themes", "sdk",
    "api", "translations", "l10n", "i18n", "sample", "samples", "example", "examples", "demo", "tests", "test",
    "benchmark", "mirror", "archive", "legacy", "packaging", "homebrew", "scoop", "chocolatey", "winget",
    "flatpak", "snap", "aur", "bot",
}
_SITE_REPO = re.compile(
    r"\.github\.io$|^(?:homebrew|scoop|chocolatey|awesome)-|^(?:docs?|www|site|website|wiki|blog)$", re.I)
_ACCEPT_SCORE = 45  # points a repo needs from the links that mention it plus how well it fits the app
_NEAR = 40          # candidates this close to the best are told apart by their releases
_VERIFY_AT_MOST = 3


def _repo_refs(url: str):
    """(owner/repo, rest of the path) for each repository a URL points into."""
    for m in _GITHUB_URL.finditer(url or ""):
        owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2), flags=re.I)
        if owner.lower() in _NOT_REPO_OWNERS or not repo or repo.startswith("."):
            continue
        if (owner.lower(), repo.lower()) == ("microsoft", "winget-pkgs"):
            continue
        yield f"{owner}/{repo}", m.group(3)


def repo_from_urls(urls) -> str | None:
    """First plausible owner/repo in the given URLs, whatever app it belongs to. A release-download
    URL wins over a homepage because it proves where the installer comes from."""
    fallback = None
    for url in urls:
        for repo, rest in _repo_refs(url):
            if rest.lower().startswith("/releases/download/"):
                return repo
            fallback = fallback or repo
    return fallback


def _words(text: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", _NOISE.sub(" ", text or "").replace("+", " plus "))
    return [w for w in re.findall(r"[a-z0-9]+", spaced.lower()) if len(w) > 1 or w.isdigit()]


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _NOISE.sub(" ", text or "").replace("+", "plus").lower())


def _closeness(a: str, b: str) -> int:
    """60 for the same name, 40 when one holds most of the other (powertoys / microsoftpowertoys), else 0."""
    if not a or not b:
        return 0
    if a == b:
        return 60
    short, long_ = sorted((a, b), key=len)
    return 40 if short in long_ and len(short) >= 5 and len(short) * 5 >= len(long_) * 2 else 0


@dataclass
class _Profile:
    names: list[str]            # compact spellings of the app's name: display name, without publisher, winget package
    words: set[str]             # the words of those names that say something
    publishers: list[str]       # compact spellings of the publisher
    publisher_words: set[str]


def _profile(app: InstalledApp, winget_id: str | None) -> _Profile:
    publisher_id, _, package = (winget_id or "").partition(".")
    publisher_words = {w for w in _words(f"{app.publisher} {publisher_id}") if w not in _COMPANY_WORDS}
    name_words = _words(app.name)
    words = {w for w in (*name_words, *_words(package)) if w not in _GENERIC_WORDS and w not in publisher_words}
    names = {_compact(app.name), "".join(w for w in name_words if w not in publisher_words), _compact(package)}
    publishers = {_compact(" ".join(w for w in _words(app.publisher) if w not in _COMPANY_WORDS)),
                  _compact(publisher_id)}
    return _Profile([n for n in names if len(n) > 1], words, [p for p in publishers if len(p) > 1], publisher_words)


def _url_weight(rest: str) -> int:
    """How much a link says about the repo: a release download proves the installer lives there."""
    path = rest.lower()
    if path.startswith("/releases/download/"):
        return 40
    if path.startswith("/releases"):
        return 20
    if path.startswith(("/issues", "/discussions", "/wiki", "/blob/", "/tree/", "/raw/")):
        return 12
    return 10


@dataclass(frozen=True)
class RepoCandidate:
    repo: str
    score: int


def rank_repos(app: InstalledApp, urls, winget_id: str | None = None) -> list[RepoCandidate]:
    """The repositories the URLs point to that plausibly are this app's, best first. A link alone proves
    nothing (help pages link sponsors, docs and other projects): the repo must also look like the app by
    name or owner, unless the link is a release download from an owner that matches the publisher."""
    found: dict[str, tuple[str, list[int]]] = {}
    for url in urls:
        for repo, rest in _repo_refs(url):
            found.setdefault(repo.lower(), (repo, []))[1].append(_url_weight(rest))
    profile = _profile(app, winget_id)
    ranked = []
    for repo, weights in found.values():
        owner, name = repo.split("/", 1)
        proof = max(weights) + min(10, 5 * (len(weights) - 1))
        repo_words = set(_words(name))
        shared = repo_words & profile.words
        spoken_for = shared and shared >= repo_words - _GENERIC_WORDS   # hiddify-app: its every word is in the name
        by_repo = max(max((_closeness(_compact(name), n) for n in profile.names), default=0),
                      40 if spoken_for else min(40, 20 * len(shared)))
        owner_c = _compact(owner)
        by_owner = 30 if owner_c in profile.names else 0   # telegramdesktop/tdesktop for Telegram Desktop
        published = max(max((_closeness(owner_c, p) for p in profile.publishers), default=0) * 5 // 12,
                        min(24, 12 * len(set(_words(owner)) & profile.publisher_words)))
        named = max(by_repo, by_owner)
        penalty = 100 if _SITE_REPO.search(name) else 50 if (repo_words & _OTHER_PRODUCT) - profile.words else 0
        score = named + published + proof - penalty
        if score >= _ACCEPT_SCORE and (named >= 20 or (proof >= 40 and published >= 12)):
            ranked.append(RepoCandidate(repo, score))
    ranked.sort(key=lambda c: -c.score)
    return ranked


def discover_token(use_gh_cli: bool) -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip()
    if use_gh_cli and shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                               timeout=10, creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def machine_arch() -> str:
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    return "arm64" if arch == "ARM64" else "x64"


class RateLimited(Exception):
    def __init__(self, reset_at: float | None):
        super().__init__("GitHub rate limit reached")
        self.reset_at = reset_at


@dataclass
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None


@dataclass
class Release:
    tag: str
    title: str
    url: str
    prerelease: bool
    draft: bool
    notes: str
    assets: list[Asset]

    @property
    def version(self) -> str:
        if parse_version(self.tag):
            return clean(self.tag)
        return self.title or self.tag


@dataclass
class CheckResult:
    latest: str | None
    release: Release | None
    asset: Asset | None
    installer_type: str | None
    incomparable: str | None = None   # newest release when none of them could be compared with the installed version


def _shape(name: str) -> str:
    """File name with versions blanked, to recognise the 'same' asset across releases."""
    return re.sub(r"\d+(?:[._-]\d+)*", "#", name.lower())


def _arch_of(name: str) -> str | None:
    if re.search(r"arm64|aarch64", name):
        return "arm64"
    if re.search(r"x64|x86[_-]64|amd64|win64|64[-_ ]?bit", name):
        return "x64"
    if re.search(r"x86|ia32|i[36]86|32[-_ ]?bit", name):
        return "x86"
    return None


def _asset_kind(asset: Asset, assets: list[Asset]) -> str:
    """Installer family when the release gives it away: the extension, a Squirrel RELEASES/.nupkg beside
    its Setup.exe, the .blockmap electron-builder writes beside an NSIS installer. '' when it can't tell."""
    n = asset.name.lower()
    if n.endswith(".msi"):
        return "msi"
    if n.endswith((".msix", ".msixbundle", ".appx", ".appxbundle")):
        return "msix"
    names = {a.name.lower() for a in assets}
    if f"{n}.blockmap" in names:
        return "nullsoft"
    if "releases" in names or any(x.endswith(".nupkg") for x in names):
        return "squirrel"
    return ""


def _family(kind: str) -> str:
    return kind if kind in ("msi", "msix") else "exe"


def _mentions_app(asset_name: str, app: InstalledApp) -> bool:
    flat = _compact(asset_name)
    return any(w in flat for w in _words(app.name) if w not in _GENERIC_WORDS and len(w) >= 3)


def pick_asset(assets: list[Asset], app: InstalledApp, arch: str, hint_name: str | None = None) -> Asset | None:
    hint = _shape(hint_name) if hint_name else None
    have = app.install_kind or ("msi" if app.is_msi else "")
    best, best_score = None, None
    for asset in assets:
        n = asset.name.lower()
        ext = next((e for e in _INSTALLABLE if n.endswith(e)), None)
        if not ext or _NOT_WINDOWS.search(n) or _NOT_INSTALLER.search(n):
            continue
        asset_arch = _arch_of(n)
        if asset_arch == "arm64" and arch != "arm64":
            continue

        score = 0
        if hint and _shape(asset.name) == hint:
            score += 100
        if ext == ".msi":
            score += 30 if app.is_msi else 18
        elif ext == ".exe":
            score += 20 if app.is_msi else 30
        else:
            score += 8

        if have:
            kind = _asset_kind(asset, assets)
            if _family(kind or ext.lstrip(".")) != _family(have):
                score -= 15
            else:
                score += 20
                if kind and kind != have and _family(kind) == "exe":
                    score -= 20     # a Squirrel setup over an Inno install would leave a second copy
                elif kind == have:
                    score += 10

        want = "arm64" if arch == "arm64" else (app.arch or "x64")
        if asset_arch is None:
            score += 6
        elif asset_arch == want:
            score += 15
        elif asset_arch == "x86" and want != "x86":
            score -= 12
        else:
            score -= 4

        if re.search(r"setup|install", n):
            score += 10
        if re.search(r"win(dows)?(?![a-z])|win64|win32", n):
            score += 4
        machine = _MACHINE_WIDE.search(n) is not None
        per_user = not machine and "user" in n
        if per_user or machine:
            score += 12 if (per_user and app.scope == "user") or (machine and app.scope == "machine") else -12
        if _mentions_app(n, app):
            score += 10
        elif _PREREQUISITE.search(n):
            score -= 25

        if best_score is None or score > best_score:
            best, best_score = asset, score
    return best


class GitHub:
    def __init__(self, cache, token: str | None, include_prereleases: bool = False):
        self.cache = cache
        self.token = token
        self.include_prereleases = include_prereleases
        self.limited_until: float | None = None
        self.remaining: str | None = None

    def _request(self, path: str, etag: str | None):
        if self.limited_until and time.time() < self.limited_until:
            raise RateLimited(self.limited_until)
        req = urllib.request.Request(API + path, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        })
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with net.urlopen(req, timeout=20) as resp:
                self.remaining = resp.headers.get("X-RateLimit-Remaining", self.remaining)
                return 200, json.load(resp), resp.headers.get("ETag")
        except urllib.error.HTTPError as e:
            self.remaining = e.headers.get("X-RateLimit-Remaining", self.remaining)
            if e.code == 304:
                return 304, None, etag
            if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.code == 429):
                reset = e.headers.get("X-RateLimit-Reset")
                self.limited_until = float(reset) if reset else time.time() + 600
                raise RateLimited(self.limited_until) from None
            if e.code == 404:
                return 404, None, None
            raise

    def releases(self, repo: str, force: bool = False) -> list[Release]:
        cached = self.cache.entry("github", repo)
        if cached and not force and time.time() - cached.get("ts", 0) < CACHE_FRESH_SECONDS:
            raw = cached.get("releases", [])
        else:
            status, data, etag = self._request(f"/repos/{repo}/releases?per_page=15",
                                               cached.get("etag") if cached else None)
            if status == 304 and cached:
                raw = cached.get("releases", [])
            elif status == 404 or not isinstance(data, list):
                raw = []
            else:
                raw = [{
                    "tag": r.get("tag_name") or "",
                    "title": r.get("name") or "",
                    "url": r.get("html_url") or f"https://github.com/{repo}/releases",
                    "prerelease": bool(r.get("prerelease")),
                    "draft": bool(r.get("draft")),
                    "notes": (r.get("body") or "")[:1500],
                    "assets": [{
                        "name": a.get("name") or "",
                        "url": a.get("browser_download_url") or "",
                        "size": a.get("size") or 0,
                        "sha256": (a.get("digest") or "").removeprefix("sha256:") or None,
                    } for a in r.get("assets", [])],
                } for r in data]
            self.cache.put("github", repo, {"etag": etag, "releases": raw})
        return [Release(**{**r, "assets": [Asset(**a) for a in r["assets"]]}) for r in raw]

    def discover(self, app: InstalledApp, urls, winget_id: str | None = None, arch: str | None = None) -> str | None:
        """The repo to check for this app. Usually the one candidate its links leave; when several fit
        about equally, the one whose recent releases carry a Windows installer for this app wins."""
        ranked = rank_repos(app, urls, winget_id)
        near = [c for c in ranked if ranked[0].score - c.score <= _NEAR][:_VERIFY_AT_MOST] if ranked else []
        if len(near) < 2:
            return near[0].repo if near else None
        arch = arch or machine_arch()
        return max(near, key=lambda c: c.score + self._installer_evidence(c.repo, app, arch)).repo

    def _installer_evidence(self, repo: str, app: InstalledApp, arch: str) -> int:
        try:
            releases = [r for r in self.releases(repo) if not r.draft][:5]
        except (RateLimited, urllib.error.URLError, OSError, ValueError) as e:
            log.debug("could not read the releases of %s to compare candidates: %s", repo, e)
            return 0
        for release in releases:
            asset = pick_asset(release.assets, app, arch)
            if asset:
                return 25 + (10 if _mentions_app(asset.name, app) else 0)
        return -25

    def check(self, repo: str, app: InstalledApp, arch: str, hint_url: str | None = None,
              hint_type: str | None = None, force: bool = False) -> CheckResult:
        def offered(r: Release) -> bool:
            if r.draft or not parse_version(r.version):
                return False
            return self.include_prereleases or not (r.prerelease or _PRE_TAG.search(r.tag) or is_prerelease(r.version))

        listed = [r for r in self.releases(repo, force) if offered(r)]
        # 2026.11.02 vs 8.0.1: different numbering schemes, comparing them means nothing.
        candidates = [r for r in listed if not app.version or compare(r.version, app.version) is not None]
        candidates.sort(key=cmp_to_key(lambda a, b: compare(a.version, b.version) or 0), reverse=True)
        if not candidates:
            return CheckResult(None, None, None, None, listed[0].version if listed else None)

        hint_name = None
        if hint_url and f"github.com/{repo.lower()}/releases/download/" in hint_url.lower():
            hint_name = hint_url.rsplit("/", 1)[-1]

        for release in candidates:
            if not is_newer(release.version, app.version):
                break
            asset = pick_asset(release.assets, app, arch, hint_name)
            if asset:
                same_as_hint = hint_name is not None and _shape(asset.name) == _shape(hint_name)
                return CheckResult(candidates[0].version, release, asset, hint_type if same_as_hint else None)
        return CheckResult(candidates[0].version, candidates[0], None, None)
