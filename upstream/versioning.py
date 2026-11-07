"""Lenient version parsing and ordering for strings like 'v1.2.3', '17.11-1', 'Git 2.51.0.windows.1',
'Notepad++ v8.8.3', 'spotube-v5.1.2', '3.9.0+38', '1.2.3-beta.1', '2024-03-14' or '1.2.3a'.

A version is a numeric core plus a tail. Trailing zeros do not matter (1.2.3.0 == 1.2.3), a pre-release
sorts before its release (1.2.3-beta.1 < 1.2.3, 3.13.0rc1 < 3.13.0), a bare letter after the digits is a
patch level as OpenSSL writes it (1.2.3 < 1.2.3a < 1.2.4), build metadata after '+' is ignored. Platform
words (windows, x64, setup, ...) are ignored. Two versions in different numbering schemes (a date against
a release number) have no order: compare() returns None and is_newer() says no."""
from __future__ import annotations

import re
from typing import NamedTuple

# A dotted number, optionally followed by one '-N' or '_N' build part (17.11-1).
_DOTTED = re.compile(r"\d+(?:\.\d+)+(?:[-_]\d+(?![.\w]))?")
_SINGLE = re.compile(r"\d+(?:[_-]\d+)*")
# Git for Windows writes 2.47.1.windows.2 for the second build of 2.47.1 and installs it as 2.47.1.2.
_REVISION = re.compile(r"[.\-_ ]windows[.\-_ ](\d+)", re.I)
_ATTACHED_LETTER = re.compile(r"([a-z])(?:(\d+)|(?![a-z0-9]))", re.I)
_WORD_OR_NUMBER = re.compile(r"[a-z]+|\d+")
_STAGES = {"dev": 0, "snapshot": 0, "nightly": 0, "canary": 0, "alpha": 1, "beta": 2, "preview": 3, "pre": 3,
           "insider": 3, "insiders": 3, "rc": 4}
_SHORT_STAGES = {"a": 1, "b": 2, "c": 4}   # 3.13.0a1, 3.13.0b2, 3.13.0c1 (PEP 440)
_RELEASE = (0,)


class _Parsed(NamedTuple):
    nums: tuple[int, ...]    # as written, zeros kept
    tail: tuple              # (-1, stage, n) pre-release, (0,) release, (1, letter) patch letter


def _core(text: str):
    s = text.strip()
    return _DOTTED.search(s) or _SINGLE.search(s)


def _tail(rest: str) -> tuple:
    rest = rest.split("+", 1)[0].lower()
    attached = _ATTACHED_LETTER.match(rest)
    if attached:
        letter, number = attached.groups()
        if number is None:
            return (1, letter)
        if letter in _SHORT_STAGES:
            return (-1, _SHORT_STAGES[letter], int(number))
    tokens = _WORD_OR_NUMBER.findall(rest)
    for i, token in enumerate(tokens):
        if token in _STAGES:
            following = tokens[i + 1] if i + 1 < len(tokens) else ""
            return (-1, _STAGES[token], int(following) if following.isdigit() else 0)
    return _RELEASE


def _parse(text: str | None) -> _Parsed | None:
    if not text:
        return None
    m = _core(text)
    if not m:
        return None
    nums = [int(p) for p in re.split(r"[._-]", m.group(0)) if p]
    if 19900101 <= nums[0] <= 21001231:   # 20240314 is the date 2024-03-14
        nums[:1] = [nums[0] // 10000, nums[0] // 100 % 100, nums[0] % 100]
    rest = text.strip()[m.end():]
    revision = _REVISION.match(rest)
    if revision:
        if int(revision.group(1)) > 1:
            nums.append(int(revision.group(1)))
        rest = rest[revision.end():]
    return _Parsed(tuple(nums), _tail(rest))


def _scheme(nums: tuple[int, ...]) -> str:
    first = nums[0]
    if len(nums) > 1 and 1990 <= first <= 2100:
        return "date"
    if len(nums) == 1 and first >= 1000:
        return "build"
    return "release"


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The numeric core without trailing zeros, or None when there is no number at all."""
    parsed = _parse(text)
    if parsed is None:
        return None
    nums = list(parsed.nums)
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def clean(text: str) -> str:
    """The version part of a tag or title: 'v8.8.3' -> '8.8.3', 'spotube-v5.1.2' -> '5.1.2'."""
    m = _core(text)
    return text.strip()[m.start():] if m else text.strip()


def is_prerelease(text: str | None) -> bool:
    parsed = _parse(text)
    return parsed is not None and parsed.tail[0] == -1


def compare(a: str | None, b: str | None) -> int | None:
    """-1, 0 or 1 like cmp(a, b); None when either has no number or they use different numbering schemes."""
    pa, pb = _parse(a), _parse(b)
    if pa is None or pb is None or _scheme(pa.nums) != _scheme(pb.nums):
        return None
    width = max(len(pa.nums), len(pb.nums))
    left = (pa.nums + (0,) * (width - len(pa.nums)), pa.tail)
    right = (pb.nums + (0,) * (width - len(pb.nums)), pb.tail)
    return (left > right) - (left < right)


def is_newer(candidate: str | None, installed: str | None) -> bool:
    return compare(candidate, installed) == 1


def same_version(a: str | None, b: str | None) -> bool:
    return compare(a, b) == 0
