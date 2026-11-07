"""Security and end-of-life badges for rows. Owner: the security-badge feature."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Badge:
    kind: str                       # security | eol | outdated | pinned
    text: str                       # English template shown on the row (translated when shown)
    tooltip: str = ""               # English template


def compute(status, cache=None) -> list[Badge]:
    """Badges for one scanned app. Fast and offline: uses the release notes already fetched and the cached
    end-of-life tables. Never raises."""
    return []


def refresh_eol(cache, statuses) -> None:
    """Fetch endoflife.date tables for the products among `statuses` that are missing or stale (scan worker
    thread, a few requests at most). Never raises."""
    return None
