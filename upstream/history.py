"""Install, update and uninstall history, kept as JSON Lines in DATA_DIR/history.jsonl.

One line per entry, appended whole under a lock. A line torn by a crash is skipped when reading, and the next
append starts on a fresh line, so one bad line never costs the entries around it. Past about 2 MB the file is
moved to history.1.jsonl (one old file is kept). Messages are stored as an English template plus values and
translated when shown, so a language switch also applies to old entries."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from .config import DATA_DIR

log = logging.getLogger("upstream.history")

KINDS = ("install", "update", "rollback", "uninstall", "cleanup", "restore")
MAX_BYTES = 2 * 1024 * 1024     # the file is rotated once it is this big
MAX_TEXT = 500                  # longest text kept in one field: an installer's output can be pages long
_lock = threading.Lock()
_settings = None
_clean_file = None      # the file this process last appended to whole: it ends with a newline


@dataclass(frozen=True)
class HistoryEntry:
    id: str
    ts: float                       # time.time()
    kind: str                       # install | update | rollback | uninstall | cleanup | restore
    name: str
    version_from: str = ""
    version_to: str = ""
    ok: bool = True
    template: str = ""              # English message template, rendered with `values` when shown
    values: dict = field(default_factory=dict)
    source: str = ""                # github | site | winget | empty


def configure(settings) -> None:
    """Called once at start. With `history_enabled` false, record() does nothing (read live, so the setting applies at once)."""
    global _settings
    _settings = settings


def is_enabled() -> bool:
    try:
        return _settings is None or bool(_settings.get("history_enabled", True))
    except Exception:
        return True


def _file():
    return DATA_DIR / "history.jsonl"


def _old_file():
    return DATA_DIR / "history.1.jsonl"


def _text(value) -> str:
    return "" if value is None else str(value)[:MAX_TEXT]


def _clean_values(values) -> dict:
    """Only what JSON keeps as it is; anything else (a Path, an exception) becomes text."""
    if not isinstance(values, dict):
        return {}
    out = {}
    for key, value in values.items():
        if isinstance(value, bool) or (isinstance(value, (int, float)) and math.isfinite(value)):
            out[str(key)] = value
        else:
            out[str(key)] = _text(value)
    return out


def record(kind: str, name: str, version_from: str = "", version_to: str = "", ok: bool = True,
           template: str = "", values: dict | None = None, source: str = "", ts: float | None = None) -> None:
    """Append one entry. Never raises: history must not break an install."""
    try:
        if not is_enabled():
            return
        when = time.time() if ts is None or not math.isfinite(ts) else float(ts)
        entry = {
            "id": uuid.uuid4().hex[:12], "ts": when,
            "kind": _text(kind), "name": _text(name), "version_from": _text(version_from),
            "version_to": _text(version_to), "ok": bool(ok), "template": _text(template),
            "values": _clean_values(values), "source": _text(source),
        }
        line = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "replace")   # a stray surrogate in a path must not lose the entry
        with _lock:
            _append(line)
    except Exception:
        log.warning("Could not record a history entry", exc_info=True)


def _append(line: bytes) -> None:
    global _clean_file
    path = _file()
    try:
        if path.stat().st_size >= MAX_BYTES:
            os.replace(path, _old_file())
            _clean_file = None
    except OSError:
        pass    # no file yet, or the old copy is busy: keep appending, the next record tries again
    if _clean_file != path and _ends_mid_line(path):
        line = b"\n" + line     # the last write was cut short: start a new line, the torn one is skipped when reading
    _clean_file = None          # until this write is done
    try:
        f = open(path, "ab")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "ab")
    with f:
        f.write(line)           # one write, appended at the end, so lines never interleave
    _clean_file = path


def _ends_mid_line(path) -> bool:
    """Opening a file for reading is slow here (virus scanners), so this runs once per file, not for every record."""
    try:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False    # missing or empty


def _entry_from(raw) -> HistoryEntry | None:
    if not isinstance(raw, dict):
        return None
    ts, kind, name = raw.get("ts"), raw.get("kind"), raw.get("name")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return None
    if not isinstance(kind, str) or not kind or not isinstance(name, str):
        return None
    values = raw.get("values")
    return HistoryEntry(
        id=_text(raw.get("id")) or f"{ts}:{name}", ts=float(ts), kind=kind, name=name,
        version_from=_text(raw.get("version_from")), version_to=_text(raw.get("version_to")),
        ok=raw.get("ok") is not False, template=_text(raw.get("template")),
        values=values if isinstance(values, dict) else {}, source=_text(raw.get("source")))


def _read(path) -> list[HistoryEntry]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    out = []
    for line in data.split(b"\n"):
        line = line.replace(b"\x00", b"").strip()   # a crash can leave NULs; JSON text never contains one
        if not line:
            continue
        try:
            entry = _entry_from(json.loads(line.decode("utf-8")))
        except ValueError:      # not JSON, or not UTF-8 (UnicodeDecodeError is a ValueError)
            continue
        if entry:
            out.append(entry)
    return out


def load(limit: int = 500) -> list[HistoryEntry]:
    """Newest first. Entries with the same time keep the order they were written in."""
    try:
        with _lock:
            entries = _read(_old_file()) + _read(_file())
        order = sorted(enumerate(entries), key=lambda pair: (pair[1].ts, pair[0]), reverse=True)
        return [entry for _, entry in order[:max(0, limit)]]
    except Exception:
        log.warning("Could not read the history", exc_info=True)
        return []


def clear() -> None:
    with _lock:
        for path in (_file(), _old_file()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("Could not delete %s", path, exc_info=True)
