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

from .inventory import InstalledApp
from .versioning import is_newer, parse_version

log = logging.getLogger("upstream.github")

API = "https://api.github.com"
USER_AGENT = "Upstream-Updater/1.0"
CREATE_NO_WINDOW = 0x08000000
CACHE_FRESH_SECONDS = 15 * 60

_REPO_RE = re.compile(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)", re.I)
_NOT_REPO_OWNERS = {
    "sponsors", "orgs", "settings", "marketplace", "features", "topics", "apps", "login",
    "about", "pricing", "enterprise", "notifications", "users", "site", "collections", "search",
}

_INSTALLABLE = (".msi", ".exe", ".msix", ".msixbundle", ".appx", ".appxbundle")
_NOT_WINDOWS = re.compile(r"linux|ubuntu|debian|fedora|centos|darwin|macos|\bmac\b|osx|apple|android|\bios\b|freebsd", re.I)
_NOT_INSTALLER = re.compile(r"portable|symbols?|debug|\bpdb\b|sources?\b|\bsrc\b|uninstall|delta|blockmap", re.I)
# Many projects publish betas without ticking GitHub's "pre-release" box.
_PRE_TAG = re.compile(r"(?<![a-z])(alpha|beta|rc|pre|preview|nightly|dev|canary|insiders?|snapshot)(?![a-z])", re.I)


def _date_like(version: str | None) -> bool:
    parsed = parse_version(version)
    return bool(parsed) and 1990 <= parsed[0] <= 2100


def repo_from_urls(urls) -> str | None:
    """First plausible owner/repo in the given URLs. A release-download URL wins
    over a homepage because it proves where the installer comes from."""
    fallback = None
    for url in urls:
        if not url:
            continue
        for m in _REPO_RE.finditer(url):
            owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
            if owner.lower() in _NOT_REPO_OWNERS or not repo or repo.startswith("."):
                continue
            if (owner.lower(), repo.lower()) == ("microsoft", "winget-pkgs"):
                continue
            candidate = f"{owner}/{repo}"
            if "/releases/download/" in url.lower():
                return candidate
            fallback = fallback or candidate
    return fallback


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
            return re.sub(r"^[^\d]*", "", self.tag) or self.tag
        return self.title or self.tag


@dataclass
class CheckResult:
    latest: str | None
    release: Release | None
    asset: Asset | None
    installer_type: str | None


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


def pick_asset(assets: list[Asset], app: InstalledApp, arch: str, hint_name: str | None = None) -> Asset | None:
    hint = _shape(hint_name) if hint_name else None
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
        if "user" in n:
            score += 6 if app.scope == "user" else -6

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
            with urllib.request.urlopen(req, timeout=20) as resp:
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

    def check(self, repo: str, app: InstalledApp, arch: str, hint_url: str | None = None,
              hint_type: str | None = None, force: bool = False) -> CheckResult:
        def usable(r: Release) -> bool:
            if r.draft or not parse_version(r.version):
                return False
            if not self.include_prereleases and (r.prerelease or _PRE_TAG.search(r.tag)):
                return False
            # 2026.11.02 vs 8.0.1: different numbering schemes, comparing them means nothing.
            return not app.version or _date_like(r.version) == _date_like(app.version)

        candidates = [r for r in self.releases(repo, force) if usable(r)]
        candidates.sort(key=lambda r: parse_version(r.version), reverse=True)
        if not candidates:
            return CheckResult(None, None, None, None)

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
