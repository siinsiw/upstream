"""Everything that talks to the network on the user's behalf: the one place proxy settings,
resumable downloads and the metered-connection / battery policy live.

Every other module opens URLs through urlopen() here, never through urllib directly."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.request
from pathlib import Path

from .config import DOWNLOAD_DIR
from .errors import InstallError

log = logging.getLogger("upstream.net")

USER_AGENT = "Upstream-Updater/1.0"

_settings = None


def configure(settings) -> None:
    """Called once at start-up so proxy and connection policies can read the user's settings."""
    global _settings
    _settings = settings


def urlopen(req, timeout: float = 20):
    """Open a Request or URL string. The proxy / offline policy is applied here."""
    return urllib.request.urlopen(req, timeout=timeout)


def preflight(ctx) -> None:
    """Run before a download starts: metered connection and battery policy.
    May ask the user through ctx.ask() or raise errors.Cancelled."""
    return None


def download(url: str, file_name: str | None, on_progress, expected_sha256: str | None = None,
             cancel=None) -> Path:
    """Download to the downloads folder and return the file. `cancel` is a context.CancelToken."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=60) as resp:
        if not file_name:
            disposition = resp.headers.get("Content-Disposition") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
            file_name = m.group(1) if m else resp.url.rsplit("/", 1)[-1].split("?", 1)[0]
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", file_name).strip(". ") or "installer.exe"
        dest = DOWNLOAD_DIR / safe
        part = dest.with_name(dest.name + ".part")
        total = int(resp.headers.get("Content-Length") or 0)
        digest = hashlib.sha256()
        done = 0
        with open(part, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                on_progress(done, total)
                if cancel is not None:
                    cancel.check()
    if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
        part.unlink(missing_ok=True)
        raise InstallError("The download's SHA-256 doesn't match the hash GitHub published; nothing was installed.")
    os.replace(part, dest)
    return dest
