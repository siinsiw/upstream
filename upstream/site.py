"""User-defined rule for an app's own website: a page, a regex that finds the
latest version on it, and optionally a download URL template."""
from __future__ import annotations

import re
import urllib.parse
import urllib.request

from . import net

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Upstream-Updater/1.0"


def check(rule: dict) -> tuple[str, str | None]:
    """(latest version, download URL or None)."""
    page = rule["url"]
    req = urllib.request.Request(page, headers={"User-Agent": USER_AGENT})
    with net.urlopen(req, timeout=20) as resp:
        html = resp.read(3 * 1024 * 1024).decode(resp.headers.get_content_charset() or "utf-8", "replace")
    m = re.search(rule["version_regex"], html, re.I | re.S)
    if not m:
        # English on purpose: the scan note translates it when it is displayed.
        raise ValueError("Version pattern not found on the page")
    version = (m.group(1) if m.groups() else m.group(0)).strip()
    template = (rule.get("download_url") or "").strip()
    if not template:
        return version, None
    parts = re.findall(r"\d+", version)
    url = template.format(version=version, version_nodots="".join(parts),
                          major=parts[0] if parts else version)
    return version, urllib.parse.urljoin(page, url)
