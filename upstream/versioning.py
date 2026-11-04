"""Lenient version parsing for strings like 'v1.2.3', '17.11-1', 'Git 2.51.0.windows.1',
'spotube-v5.1.2' or '3.9.0+38'. Only the numeric core is compared."""
from __future__ import annotations

import re

# A dotted number, optionally followed by one '-N' or '_N' build part (17.11-1).
_DOTTED = re.compile(r"\d+(?:\.\d+)+(?:[-_]\d+(?![.\w]))?")
_SINGLE = re.compile(r"\d+(?:[_-]\d+)*")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    s = text.strip().split("+", 1)[0]  # drop semver build metadata
    m = _DOTTED.search(s) or _SINGLE.search(s)
    if not m:
        return None
    parts = [int(p) for p in re.split(r"[._-]", m.group(0)) if p]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(candidate: str | None, installed: str | None) -> bool:
    a, b = parse_version(candidate), parse_version(installed)
    if a is None or b is None:
        return False
    return a > b


def same_version(a: str | None, b: str | None) -> bool:
    pa, pb = parse_version(a), parse_version(b)
    return pa is not None and pa == pb
