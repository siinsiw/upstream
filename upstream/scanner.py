"""Decides, for every installed app, where its update comes from.

Order: the project's GitHub Releases, then a site rule the user set, then
winget (the vendor's own installer, hash-pinned in the winget manifest)."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import site, winget
from .github import GitHub, RateLimited, discover_token, machine_arch, repo_from_urls
from .i18n import render, tr
from .inventory import InstalledApp, read_installed_apps
from .versioning import is_newer

log = logging.getLogger("upstream.scan")

META_TTL = 7 * 24 * 3600


@dataclass
class Update:
    source: str                    # github | site | winget
    version: str
    origin: str                    # owner/repo, site URL or winget id
    download_url: str | None = None
    file_name: str | None = None
    sha256: str | None = None
    page_url: str | None = None
    notes: str = ""
    installer_type: str | None = None
    winget_mode: str = "upgrade"   # upgrade | install


@dataclass
class AppStatus:
    app: InstalledApp
    repo: str | None = None
    winget_id: str | None = None
    winget_available: str = ""
    winget_forced: bool = False
    update: Update | None = None
    fallbacks: list[Update] = field(default_factory=list)
    note: str = ""                  # English template; rendered when shown so a language switch applies
    note_args: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return self.app.reg_key

    def note_text(self) -> str:
        return render(self.note, self.note_args)

    def set_note(self, template: str, **args):
        self.note, self.note_args = template, args


class Scanner:
    def __init__(self, settings, cache):
        self.settings = settings
        self.cache = cache
        self.arch = machine_arch()
        self._gh: GitHub | None = None
        self._gh_lock = threading.Lock()

    @property
    def github(self) -> GitHub:
        # Worker threads all ask for the client; build it once.
        with self._gh_lock:
            if self._gh is None:
                token = discover_token(bool(self.settings.get("use_gh_cli_token", True)))
                self._gh = GitHub(self.cache, token, bool(self.settings.get("include_prereleases", False)))
            return self._gh

    def reset_github(self):
        """Settings for token or pre-releases changed; rebuild the client on next use."""
        with self._gh_lock:
            self._gh = None

    @property
    def github_if_ready(self) -> GitHub | None:
        return self._gh

    def scan(self, progress) -> list[AppStatus]:
        progress(tr("Reading installed apps…"), 0, 0)
        hidden: set[str] = set()
        statuses = [AppStatus(app) for app in read_installed_apps(hidden)]

        if winget.available():
            progress(tr("Getting the winget package list…"), 0, 0)
            try:
                rows = winget.list_installed()
                matched = winget.match_rows([s.app for s in statuses], rows)
                for s in statuses:
                    row = matched.get(s.uid)
                    if row:
                        s.winget_id, s.winget_available = row.id, row.available
                # Store/MSIX apps (Windows Terminal, WSL, ...) have no Uninstall
                # registry entry; winget still knows and can update them.
                taken = {row.id for row in matched.values()}
                for row in rows:
                    if row.id in taken or row.name.lower() in hidden:
                        continue
                    taken.add(row.id)
                    app = InstalledApp(reg_key=f"WINGET\\{row.id}", name=row.name.rstrip("…").strip(),
                                       version=row.version, publisher=row.id.split(".", 1)[0],
                                       scope="user", arch="x64")
                    statuses.append(AppStatus(app, winget_id=row.id, winget_available=row.available))
            except Exception:
                log.exception("winget list failed")

        active = [s for s in statuses if not self.settings.is_ignored(s.app.ident)]
        self.github  # noqa: B018 - build the client on this thread, before the pools start
        for s in active:
            self._apply_override(s)
        self._refresh_meta(active, progress)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.resolve, s, False) for s in active]
            for done, future in enumerate(as_completed(futures), 1):
                progress(tr("Checking GitHub and official sites…"), done, len(futures))
                try:
                    future.result()
                except Exception:
                    log.exception("resolve failed")
        self.cache.save()
        return statuses

    def recheck(self, status: AppStatus) -> AppStatus:
        self._apply_override(status)
        if status.winget_id:
            self._refresh_meta([status], lambda *a: None, force=status.winget_forced)
        self.resolve(status, True)
        self.cache.save()
        return status

    def _apply_override(self, s: AppStatus):
        forced = self.settings.override(s.app.ident).get("winget")
        if forced and forced != s.winget_id:
            s.winget_id, s.winget_available, s.winget_forced = forced, "", True

    def _meta(self, pkg_id: str | None) -> dict:
        return (self.cache.entry("winget_meta", pkg_id) or {}) if pkg_id else {}

    def _refresh_meta(self, statuses, progress, force=False):
        ids = sorted({s.winget_id for s in statuses if s.winget_id})
        stale = [i for i in ids if force or time.time() - self._meta(i).get("ts", 0) > META_TTL]
        if not stale:
            return
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(winget.show, pkg_id): pkg_id for pkg_id in stale}
            for done, future in enumerate(as_completed(futures), 1):
                progress(tr("Finding each app's official source…"), done, len(stale))
                try:
                    self.cache.put("winget_meta", futures[future], future.result())
                except Exception:
                    log.exception("winget show %s failed", futures[future])
        self.cache.save()

    def resolve(self, s: AppStatus, fresh: bool):
        app = s.app
        override = self.settings.override(app.ident)
        meta = self._meta(s.winget_id)
        s.repo = override.get("github") or repo_from_urls(app.urls) or meta.get("repo")
        s.update, s.fallbacks = None, []
        s.set_note("")
        chain: list[Update] = []

        if s.repo:
            try:
                res = self.github.check(s.repo, app, self.arch, meta.get("installer_url"),
                                        meta.get("installer_type"), force=fresh)
                if res.asset:
                    chain.append(Update("github", res.release.version, s.repo,
                                        download_url=res.asset.url, file_name=res.asset.name,
                                        sha256=res.asset.sha256, page_url=res.release.url,
                                        notes=res.release.notes, installer_type=res.installer_type))
                elif res.latest and is_newer(res.latest, app.version):
                    s.set_note("GitHub has {version}, but no Windows installer", version=res.latest)
            except RateLimited:
                s.set_note("GitHub rate limit reached; using winget")
            except Exception as e:
                log.warning("GitHub check %s failed: %s", s.repo, e)
                s.set_note("GitHub was unreachable")

        rule = override.get("site")
        if rule and rule.get("url") and rule.get("version_regex"):
            try:
                version, url = site.check(rule)
                if is_newer(version, app.version):
                    chain.append(Update("site", version, rule["url"], download_url=url, page_url=rule["url"]))
            except Exception as e:
                log.warning("site check for %s failed: %s", app.name, e)
                if not s.note:
                    s.set_note("Site check failed: {error}", error=str(e))

        if s.winget_id:
            if s.winget_available:
                chain.append(Update("winget", s.winget_available, s.winget_id, page_url=meta.get("homepage")))
            elif s.winget_forced and is_newer(meta.get("version"), app.version):
                chain.append(Update("winget", meta["version"], s.winget_id, page_url=meta.get("homepage"),
                                    winget_mode="install"))

        if chain:
            s.update, s.fallbacks = chain[0], chain[1:]
