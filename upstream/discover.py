"""Find apps that are not installed yet in winget's catalogue, and turn a pick into something the update queue can run.

Nothing here installs anything: `search` runs `winget search` (read-only) and `status_for` only builds the
AppStatus that the normal update queue installs with `winget install`."""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass

from . import winget
from .errors import Cancelled
from .i18n import N_, english
from .inventory import InstalledApp
from .scanner import AppStatus, Update

log = logging.getLogger("upstream.discover")

NO_WINGET = N_("winget isn't installed on this PC.")
FLAGS = ["--source", "winget", "--accept-source-agreements", "--disable-interactivity"]
TIMEOUT = 90                # seconds; the first search after a long while also refreshes winget's catalogue
MAX_ROWS = 300              # asked of winget. It lists matches in id order, not by relevance, so a popular name fills the
                            # first 100 rows with its per-language packages (Mozilla.Firefox.<locale>) before ESR, Beta
                            # or Nightly come up. Winget sizes its columns from about the first 100 rows and cuts longer
                            # cells in later ones with "…"; those rows are left out (see search), the rest are kept
NO_MATCH = 0x8A150014       # NO_APPLICATIONS_FOUND, what `winget search` exits with when nothing matches
_ELLIPSIS = "…"
_VALID_ID = re.compile(r"\S+\.\S+|\S+…")   # Publisher.Package, or one cut short
_LOCALE_SUFFIX = re.compile(r"\s\([a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\)$")   # "Mozilla Firefox (de)"


@dataclass(frozen=True)
class Package:
    id: str                         # winget package id
    name: str
    version: str
    publisher: str = ""
    source: str = "winget"


class SearchError(RuntimeError):
    """winget is missing or the search failed. `template` and `values` are the English message and its values
    (translated when shown); str() is the English text."""

    def __init__(self, template: str, **values):
        super().__init__(english(template, values))
        self.template, self.values = template, values


def _clean(query: str) -> str:
    """One line, no control characters, and no leading dash: winget would read "-x" as an option."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", query).split()).lstrip("- ")[:80]


def _run(args: list[str], cancel=None) -> tuple[int, list[str]]:
    """`winget <args>` with the non-interactive flags; returns (exit code, output lines). A set `cancel` ends it."""
    if not winget.available():
        raise SearchError(NO_WINGET)
    try:
        proc = subprocess.Popen(["winget", *args, *FLAGS], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=winget.CREATE_NO_WINDOW)
    except OSError as e:
        raise SearchError(N_("winget couldn't be started: {error}"), error=str(e)) from e
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            out, _ = proc.communicate(timeout=0.2)
            return proc.returncode & 0xFFFFFFFF, winget._lines(out)
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set
            if cancelled or time.monotonic() >= deadline:
                break
    proc.kill()
    proc.communicate()
    if cancelled:
        raise Cancelled("Cancelled.")
    raise SearchError(N_("winget took too long to answer."))


def _starts(header: str) -> list[int]:
    """Where each header word begins, in screen cells. winget pads every column to its widest cell and puts
    one space between columns, so a column starts where its header word does (CJK names take two cells)."""
    starts, col, before = [], 0, " "
    for ch in header:
        if before == " " and ch != " ":
            starts.append(col)
        col += winget._width(ch)
        before = ch
    return starts


def _table(lines: list[str]) -> list[tuple[str, str, str]]:
    """(name, id, version) of every row. The header is localised but the dashed line under it is not, and the
    first three columns are Name, Id and Version in every language (a Match column may follow)."""
    for i, line in enumerate(lines):
        if i > 0 and re.fullmatch(r"-{10,}", line.strip()):
            starts = _starts(lines[i - 1])
            body = lines[i + 1:]
            break
    else:
        return []
    if len(starts) < 3:
        return []
    rows = []
    for line in body:
        if not line.strip():
            break
        cells = winget._cut(line, starts)
        # winget's "<additional entries truncated due to result limit>" note lands in these cells too;
        # a package id never contains a space.
        if cells[0] and _VALID_ID.fullmatch(cells[1]) and cells[2] and " " not in cells[2]:
            rows.append((cells[0], cells[1], cells[2]))
    return rows


def _squash(text: str) -> str:
    """"Visual Studio Code", "VisualStudioCode" and "7-Zip", "7zip" compare equal; "C++" keeps its plusses."""
    return re.sub(r"[\s._-]+", "", text.casefold())


def _tier(row: tuple[str, str, str], query: str) -> int:
    """How well a result answers the query: 0 exact, 1 its name starts with it, 2 its name contains it or its id
    ends with it, 3 it is somewhere in the name or id, 4 matched only by a tag, moniker or command
    (winget lists matches in id order, not by relevance)."""
    name, pid = row[0].casefold(), row[1].casefold()
    last = pid.rsplit(".", 1)[-1]
    q = query.casefold()
    flat = _squash(query)
    if q in (name, pid, last) or (flat and flat in (_squash(name), _squash(last))):
        return 0
    if name.startswith(q) or (flat and _squash(name).startswith(flat)):
        return 1
    if re.search(rf"{re.escape(q)}", name) or last.startswith(q):
        return 2
    text = f"{name} {pid}"
    if q in text:
        return 3
    return 3 if all(word in text for word in q.split()) else 4


def _family(row: tuple[str, str, str]) -> tuple[tuple[str, str], bool]:
    """(family, is a language edition) of a result. "Mozilla Firefox (de)" with id Mozilla.Firefox.de is the German
    edition of the package whose id is the same without the language. A name ending in a language code whose id
    does not end in that code ("Mozilla Firefox (en-US)" = Mozilla.Firefox) is the plain package itself."""
    name, pid, _ = row
    suffix = _LOCALE_SUFFIX.search(name)
    head, _, last = pid.rpartition(".")
    edition = bool(suffix and head and last.casefold() == suffix.group(0).strip()[1:-1].casefold())
    return ((head if edition else pid).casefold(), _LOCALE_SUFFIX.sub("", name).casefold()), edition


def _collapse_locales(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """One row per package family: the plain package, or the first language edition when the plain one is not
    listed. Without this the 90 or so languages of a browser use up the whole list."""
    best: dict[tuple[str, str], tuple[int, bool]] = {}
    for index, row in enumerate(rows):
        family, edition = _family(row)
        if family not in best or (best[family][1] and not edition):
            best[family] = (index, edition)
    keep = {index for index, _ in best.values()}
    return [row for index, row in enumerate(rows) if index in keep]


def _rank(rows: list[tuple[str, str, str]], query: str) -> list[tuple[str, str, str]]:
    """De-duplicated by id and ordered by _tier; winget's own order decides between equals. A catalogue
    often holds a package per language ("Mozilla Firefox (de)", ...): after the first of a family
    the rest go last, so they do not bury the other matches."""
    seen: set[str] = set()
    families: set[str] = set()
    scored = []
    for index, row in enumerate(rows):
        key = row[1].casefold()
        if key in seen:
            continue
        seen.add(key)
        score = _tier(row, query)
        if _LOCALE_SUFFIX.search(row[0]):
            family = _LOCALE_SUFFIX.sub("", row[0]).casefold()
            score += 10 if family in families else 0
            families.add(family)
        scored.append((score, index, row))
    return [row for _, _, row in sorted(scored)]


def search(query: str, limit: int = 30, cancel=None) -> list[Package]:
    """winget search; slow, call from a thread. Raises SearchError (a RuntimeError whose message is an English
    template) when winget is missing or fails, and Cancelled when `cancel` (a context.CancelToken) is set."""
    query = _clean(query)
    if not query or limit < 1:
        return []
    code, lines = _run(["search", query, "--count", str(MAX_ROWS)], cancel)
    rows = _table(lines)
    if not rows:
        if code not in (0, NO_MATCH):
            last = next((line.strip() for line in reversed(lines) if line.strip()), "") or f"{code:#010X}"
            raise SearchError(N_("winget couldn't search: {error}"), error=last)
        return []
    found: dict[str, Package] = {}
    for name, pid, version in _rank(_collapse_locales(rows), query):
        if pid.endswith(_ELLIPSIS):
            log.info("left out %s: winget cut its id short", pid)      # it could not be installed
        else:
            # winget's list has no publisher column; the first part of the id is the publisher.
            found.setdefault(pid.casefold(), Package(pid, name.rstrip(_ELLIPSIS).rstrip(),
                                                     "" if version.endswith(_ELLIPSIS) else version,
                                                     pid.split(".", 1)[0]))
        if len(found) == limit:
            break
    return list(found.values())


def status_for(pkg: Package) -> AppStatus:
    """A scanner.AppStatus the update queue can run: a synthetic app plus Update('winget', winget_mode='install')."""
    app = InstalledApp(reg_key=f"WINGET\\NEW\\{pkg.id}", name=pkg.name, version="", publisher=pkg.publisher,
                       scope="user", arch="x64")
    update = Update("winget", pkg.version, pkg.id, winget_mode="install")
    return AppStatus(app, winget_id=pkg.id, update=update)
