"""Decides, for every installed app, where its update comes from.

Order: the project's GitHub Releases, then a site rule the user set, then
winget (the vendor's own installer, hash-pinned in the winget manifest).
A pin (pins.py) can hold an app back; every finished scan is saved as a snapshot (see save_snapshot)
so the next start can show the last result at once."""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from . import badges, pins, site, winget
from .config import DATA_DIR
from .github import GitHub, RateLimited, Release, discover_token, machine_arch
from .i18n import render, tr
from .inventory import InstalledApp, read_installed_apps, still_listed
from .versioning import is_newer, parse_version

log = logging.getLogger("upstream.scan")

META_TTL = 7 * 24 * 3600
SNAPSHOT_FILE = DATA_DIR / "last_scan.json"
SNAPSHOT_SCHEMA = 1
_snapshot_lock = threading.Lock()


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
    winget_mode: str = "upgrade"   # upgrade | install | rollback (an older version, see pins.rollback_update)


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
    badges: list = field(default_factory=list)      # badges.Badge entries shown on the row (badges.compute)
    pin: dict | None = None                         # {"mode": "hold"|"major", "version": "1.2.3"} when pinned (pins.py)

    @property
    def uid(self) -> str:
        return self.app.reg_key

    def note_text(self) -> str:
        return render(self.note, self.note_args)

    def set_note(self, template: str, **args):
        self.note, self.note_args = template, args


# --- the last scan, kept for the next start ---
# Only what a row shows and the update queue needs; nothing from the settings or the GitHub client goes in.
_FITS = {
    "str": lambda v: isinstance(v, str),
    "str | None": lambda v: v is None or isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "dict": lambda v: isinstance(v, dict),
    "list[str]": lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v),
}


def _fits(annotation, value) -> bool:
    check = _FITS.get(annotation) if isinstance(annotation, str) else None
    return check is None or check(value)


def _from_dict(cls, raw):
    """cls(**raw), or None when `raw` is not a dict, lacks a required field or holds a value of the wrong type.
    Unknown keys are ignored and missing optional ones take their default, so the file survives a field being
    added or dropped."""
    if not isinstance(raw, dict):
        return None
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in raw:
            if not _fits(f.type, raw[f.name]):
                return None
            kwargs[f.name] = raw[f.name]
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            return None
    return cls(**kwargs)


_STATUS_SCALARS = ("repo", "winget_id", "winget_available", "winget_forced", "note", "note_args")


def _snapshot_entry(s: AppStatus) -> dict:
    return {
        "app": asdict(s.app), **{k: getattr(s, k) for k in _STATUS_SCALARS},
        "update": asdict(s.update) if s.update else None, "fallbacks": [asdict(u) for u in s.fallbacks],
        "badges": [asdict(b) for b in s.badges], "pin": s.pin,
    }


def _restore_status(raw) -> AppStatus | None:
    if not isinstance(raw, dict):
        return None
    app = _from_dict(InstalledApp, raw.get("app"))
    update = _from_dict(Update, raw["update"]) if raw.get("update") is not None else None
    fallbacks = [_from_dict(Update, u) for u in raw["fallbacks"]] if isinstance(raw.get("fallbacks"), list) else None
    types = {f.name: f.type for f in dataclasses.fields(AppStatus)}
    scalars = {k: raw[k] for k in _STATUS_SCALARS if k in raw}
    if (app is None or fallbacks is None or None in fallbacks or (update is None and raw.get("update") is not None)
            or not all(_fits(types[k], v) for k, v in scalars.items())):
        return None
    badge_list = raw.get("badges") if isinstance(raw.get("badges"), list) else []
    return AppStatus(app, update=update, fallbacks=fallbacks, pin=pins.clean_pin(raw.get("pin")),
                     badges=[b for b in (_from_dict(badges.Badge, b) for b in badge_list) if b], **scalars)


def save_snapshot(statuses: list[AppStatus], taken: float | None = None) -> bool:
    """Write DATA_DIR/last_scan.json (a temp file, then an atomic replace). Scan() calls it when a scan ends; the
    window may call it again after changes it wants the next start to show. False when it could not be written."""
    tmp = SNAPSHOT_FILE.with_name(SNAPSHOT_FILE.name + ".tmp")
    try:
        text = json.dumps({"schema": SNAPSHOT_SCHEMA, "time": taken or time.time(),
                           "statuses": [_snapshot_entry(s) for s in statuses]},
                          ensure_ascii=False, separators=(",", ":"), default=str)
        with _snapshot_lock:
            SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, "utf-8")
            os.replace(tmp, SNAPSHOT_FILE)
        return True
    except Exception:
        log.warning("Could not save the scan snapshot", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _still_there(app: InstalledApp) -> bool:
    """Store/MSIX rows have no Uninstall key to look up (winget answers for those): they stay until the rescan."""
    if app.reg_key.startswith("WINGET\\"):
        return True
    try:
        return still_listed(app)
    except Exception:
        log.warning("Could not tell whether %s is still installed", app.name, exc_info=True)
        return True


def load_snapshot() -> tuple[list[AppStatus], float] | None:
    """(statuses, scan time as a Unix timestamp) from the last saved scan, or None when there is none or it can't be
    trusted (missing, unreadable, another schema). An entry that doesn't validate, or whose app is no longer
    installed, is left out; the rescan that follows puts everything right."""
    try:
        raw = json.loads(SNAPSHOT_FILE.read_text("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SNAPSHOT_SCHEMA or not isinstance(raw.get("statuses"), list):
        return None
    taken = raw.get("time")
    if isinstance(taken, bool) or not isinstance(taken, (int, float)) or not math.isfinite(taken) or taken <= 0:
        return None
    statuses, seen = [], set()
    for entry in raw["statuses"]:
        status = _restore_status(entry)
        if status and status.uid not in seen and _still_there(status.app):
            seen.add(status.uid)
            statuses.append(status)
    return statuses, float(taken)


class _LineGitHub(GitHub):
    """A GitHub client that sees only the releases of one major version, so check() offers the newest release
    of the line a pin keeps the app on."""

    def __init__(self, base: GitHub, line: int):
        super().__init__(base.cache, base.token, base.include_prereleases)
        self.base, self.line = base, line

    def releases(self, repo: str, force: bool = False) -> list[Release]:
        return [r for r in self.base.releases(repo, force) if (parse_version(r.version) or (None,))[0] == self.line]


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
        self._refresh_eol(active)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.resolve, s, False) for s in active]
            for done, future in enumerate(as_completed(futures), 1):
                progress(tr("Checking GitHub and official sites…"), done, len(futures))
                try:
                    future.result()
                except Exception:
                    log.exception("resolve failed")
        self.cache.save()
        save_snapshot(statuses)
        return statuses

    def recheck(self, status: AppStatus) -> AppStatus:
        self._apply_override(status)
        if status.winget_id:
            self._refresh_meta([status], lambda *a: None, force=status.winget_forced)
        self._refresh_eol([status])
        self.resolve(status, True)
        self.cache.save()
        return status

    def _apply_override(self, s: AppStatus):
        forced = self.settings.override(s.app.ident).get("winget")
        if forced and forced != s.winget_id:
            s.winget_id, s.winget_available, s.winget_forced = forced, "", True

    def _refresh_eol(self, statuses):
        """End-of-life tables for the badges, fetched once per scan before the apps are resolved."""
        if not self.settings.get("security_badges", True):
            return
        try:
            badges.refresh_eol(self.cache, statuses)
        except Exception:
            log.exception("end-of-life refresh failed")

    def _compute_badges(self, s: AppStatus):
        s.badges = []
        if not self.settings.get("security_badges", True):
            return
        try:
            s.badges = list(badges.compute(s, self.cache))
        except Exception:
            log.exception("badges for %s failed", s.app.name)

    def _meta(self, pkg_id: str | None) -> dict:
        return (self.cache.entry("winget_meta", pkg_id) or {}) if pkg_id else {}

    def _refresh_meta(self, statuses, progress, force=False):
        ids = sorted({s.winget_id for s in statuses if s.winget_id})
        stale = [i for i in ids if force or time.time() - self._meta(i).get("ts", 0) > META_TTL
                 or self._meta(i).get("v") != winget.META_VERSION]
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

    def _github_update(self, repo: str, res) -> Update:
        return Update("github", res.release.version, repo, download_url=res.asset.url, file_name=res.asset.name,
                      sha256=res.asset.sha256, page_url=res.release.url, notes=res.release.notes,
                      installer_type=res.installer_type)

    def resolve(self, s: AppStatus, fresh: bool):
        app = s.app
        override = self.settings.override(app.ident)
        s.pin = pin = pins.pin_for(self.settings, app.ident)
        held = False    # the pin kept a newer version from being offered
        meta = self._meta(s.winget_id)
        # A repo the user typed always wins; otherwise the links on the app and in its winget manifest are scored.
        # `winget show` lists the installer URL last and the cache keeps only a dozen links, so the strongest
        # evidence (a /releases/download/ link) is added from the installer URL itself.
        urls = [*app.urls, *meta.get("urls", [])]
        installer_url = meta.get("installer_url")
        if isinstance(installer_url, str) and "github.com/" in installer_url.lower() and installer_url not in urls:
            urls.append(installer_url)
        s.repo = override.get("github") or self.github.discover(app, urls, s.winget_id, self.arch)
        s.update, s.fallbacks = None, []
        s.set_note("")
        chain: list[Update] = []

        if s.repo:
            try:
                res = self.github.check(s.repo, app, self.arch, meta.get("installer_url"),
                                        meta.get("installer_type"), force=fresh)
                if res.asset and pins.blocks(pin, app.version, res.release.version):
                    held = True
                    # Held back: nothing. Kept on a major line: the newest release of that line, from the releases
                    # check() just fetched.
                    line = pins.major_line(pin, app.version)
                    res = (_LineGitHub(self.github, line).check(s.repo, app, self.arch, meta.get("installer_url"),
                                                                meta.get("installer_type"))
                           if pin["mode"] == "major" and line is not None else None)
                if res is None:
                    pass
                elif res.asset:
                    chain.append(self._github_update(s.repo, res))
                elif res.latest and is_newer(res.latest, app.version):
                    s.set_note("GitHub has {version}, but no Windows installer", version=res.latest)
                elif res.incomparable:
                    s.set_note("GitHub's version {version} can't be compared with the installed one",
                               version=res.incomparable)
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
                    if pins.blocks(pin, app.version, version):
                        held = True
                    else:
                        chain.append(Update("site", version, rule["url"], download_url=url, page_url=rule["url"]))
            except Exception as e:
                log.warning("site check for %s failed: %s", app.name, e)
                if not s.note:
                    s.set_note("Site check failed: {error}", error=str(e))

        if s.winget_id:
            offer = None
            if s.winget_available:
                offer = Update("winget", s.winget_available, s.winget_id, page_url=meta.get("homepage"))
            elif s.winget_forced and is_newer(meta.get("version"), app.version):
                offer = Update("winget", meta["version"], s.winget_id, page_url=meta.get("homepage"),
                               winget_mode="install")
            if offer and pins.blocks(pin, app.version, offer.version):
                held = True
            elif offer:
                chain.append(offer)

        if chain:
            s.update, s.fallbacks = chain[0], chain[1:]
        if held:
            line = pins.major_line(pin, app.version)
            if pin["mode"] == "major" and line is not None:
                s.set_note("Staying on version {line}", line=f"{line}.x")
            else:
                s.set_note("Held at {version}", version=pin["version"] or app.version)
        self._compute_badges(s)
