"""Settings and caches, stored as JSON under %APPDATA% and %LOCALAPPDATA%."""
from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import shutil
import threading
import time
from pathlib import Path

APP_NAME = "Upstream"
_ROAMING = Path(os.environ.get("APPDATA") or Path.home())
_LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home())
CONFIG_DIR = _ROAMING / APP_NAME
DATA_DIR = _LOCAL / APP_NAME
DOWNLOAD_DIR = DATA_DIR / "downloads"
LOG_FILE = DATA_DIR / "upstream.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 2     # upstream.log plus .1 and .2: three files of about 1 MB


def migrate_legacy():
    """The app was called Tazeh before; carry its settings and cache over once.
    A failure is logged and skipped: losing old settings must not block startup."""
    log = logging.getLogger("upstream.config")
    for old_dir, new_dir, name in ((_ROAMING / "Tazeh", CONFIG_DIR, "settings.json"),
                                   (_LOCAL / "Tazeh", DATA_DIR, "cache.json")):
        old, new = old_dir / name, new_dir / name
        try:
            if old.is_file() and not new.exists():
                new_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(old, new)  # not copy2: a read-only attribute would break later saves
                log.info("Copied %s from the Tazeh folder", name)
        except OSError as e:
            log.warning("Could not copy %s from the Tazeh folder: %s", name, e)


class JsonStore:
    """A dict persisted to one JSON file. Every access goes through the lock
    because scans and installs run on worker threads."""

    def __init__(self, path: Path, defaults: dict):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(defaults)
        try:
            loaded = json.loads(path.read_text("utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        with self.lock:
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value):
        with self.lock:
            self.data[key] = value

    def save(self):
        # One lock across serialise + write + replace: a worker thread (trust.remember) and the UI thread
        # both save, and two writers sharing the .tmp file would leave a torn settings.json.
        with self.lock:
            text = json.dumps(self.data, ensure_ascii=False, indent=2)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, self.path)


class Settings(JsonStore):
    def __init__(self):
        super().__init__(CONFIG_DIR / "settings.json", {
            # ident -> {"github": "owner/repo", "winget": "Pkg.Id",
            #           "site": {"url", "version_regex", "download_url"}}
            "overrides": {},
            "ignored": [],
            # ident -> version the user chose to skip
            "skipped": {},
            "use_gh_cli_token": True,
            "include_prereleases": False,
            "language": "en",
            "theme": "system",
            # --- install safety (wave 1) ---
            "publisher_policy": "ask",        # ask | block | off: an update signed by a different publisher
            "signers": {},                    # ident -> certificate CN last seen on a verified install
            "signers_approved": {},           # ident -> [CN]: signers the user accepted after a warning
            "close_apps_policy": "ask",       # ask | auto | never: running app that an update would replace
            "proxy": {"mode": "system", "url": ""},   # system | none | custom
            "metered_policy": "ask",          # ask | skip | ignore: downloading on a metered connection
            "battery_policy": "ask",          # ask | skip | ignore: updating while on battery
            "install_timeout_min": 30,        # stop waiting for one installer after this many minutes
            # --- wave 2 ---
            "history_enabled": True,          # record installs, updates and uninstalls (history.py)
            "tray_enabled": False,            # tray icon next to the clock (background.py)
            "close_to_tray": False,           # closing the window keeps Upstream running in the tray
            "start_with_windows": False,      # HKCU Run entry; starts minimised to the tray
            "check_interval_hours": 0,        # scheduled background check; 0 = off
            "notify_updates": True,           # tray message when a background check finds updates
            "auto_update": [],                # idents whose updates install by themselves after a background check
            "pins": {},                       # ident -> {"mode": "hold"|"major", "version": "1.2.3"} (pins.py)
            "cleanup_backup_days": 30,        # keep leftover-cleanup backups this long (cleanup.py)
            "cleanup_restore_point": True,    # try to create a Windows restore point before a cleanup
            "security_badges": True,          # security / end-of-life badges on rows (badges.py)
            "export_dir": "",                 # last folder used by Export
            "last_tab": "updates",            # tab shown at start
        })

    def override(self, ident: str) -> dict:
        with self.lock:
            return copy.deepcopy(self.data["overrides"].get(ident, {}))

    def set_override(self, ident: str, value: dict | None):
        with self.lock:
            if value:
                self.data["overrides"][ident] = value
            else:
                self.data["overrides"].pop(ident, None)
        self.save()

    def is_ignored(self, ident: str) -> bool:
        with self.lock:
            return ident in self.data["ignored"]

    def set_ignored(self, ident: str, ignored: bool):
        with self.lock:
            items = self.data["ignored"]
            if ignored and ident not in items:
                items.append(ident)
            elif not ignored and ident in items:
                items.remove(ident)
        self.save()

    def skipped_version(self, ident: str) -> str | None:
        with self.lock:
            return self.data["skipped"].get(ident)

    def skip_version(self, ident: str, version: str | None):
        with self.lock:
            if version:
                self.data["skipped"][ident] = version
            else:
                self.data["skipped"].pop(ident, None)
        self.save()


class Cache(JsonStore):
    def __init__(self):
        super().__init__(DATA_DIR / "cache.json", {
            "winget_meta": {},   # winget id -> {repo, installer_url, installer_type, homepage, version, ts}
            "github": {},        # owner/repo -> {etag, releases, ts}
            "eol": {},           # endoflife.date product -> {cycles, ts} (badges.py)
        })

    def entry(self, section: str, key: str) -> dict | None:
        with self.lock:
            value = self.data[section].get(key)
            return copy.deepcopy(value) if value is not None else None

    def put(self, section: str, key: str, value: dict):
        value = dict(value, ts=time.time())
        with self.lock:
            self.data[section][key] = value


class RotatingLogHandler(logging.handlers.RotatingFileHandler):
    """Another Upstream process (a hand-over, an elevated relaunch) can have the log open while it is rotated;
    Windows then refuses the rename. Keep writing to the current file and rotate on a later record."""

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def log_handler(path: Path | None = None) -> logging.Handler:
    handler = RotatingLogHandler(path or LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    return handler


def setup_logging() -> logging.Logger:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("upstream")
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(log_handler())
    return root
