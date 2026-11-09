"""Export the installed-app list, to set up another PC.

Three formats: json (everything Upstream knows about each app), csv (the same table for a spreadsheet; UTF-8 with a
BOM so Excel shows Persian text correctly) and winget (the package file `winget import -i <file>` accepts, schema
2.0; only apps that have a winget ID). System components are never listed, and the file never holds paths or tokens."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .i18n import N_, tr, trn

FORMATS = ("json", "csv", "winget")   # winget = a file that `winget import` accepts
FORMAT_LABELS = {"json": N_("JSON list"), "csv": N_("Spreadsheet (CSV)"), "winget": N_("winget import file")}

CSV_COLUMNS = ("Name", "Version", "Publisher", "Scope", "Architecture", "winget ID", "GitHub repository")
# The shape `winget export` writes for the community source (packages.schema.2.0.json).
WINGET_SCHEMA = "https://aka.ms/winget-packages.schema.2.0.json"
WINGET_SOURCE = {"Argument": "https://cdn.winget.microsoft.com/cache", "Identifier": "Microsoft.Winget.Source_8wekyb3d8bbwe",
                 "Name": "winget", "Type": "Microsoft.PreIndexed.Package"}
_NOT_INSTALLED = "WINGET\\NEW\\"       # discover.status_for(): a package the user has not installed yet
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_WINGET_ID = re.compile(r"^[^\s\\/\"<>|…]+\.[^\s\\/\"<>|…]+$")   # Publisher.Package; ARP\... and MSIX\... are not importable


@dataclass(frozen=True)
class ExportReport:
    """What an export holds. `left_out` names the apps that cannot go into this format (winget file: no winget ID)."""
    fmt: str
    written: int
    left_out: tuple[str, ...] = ()

    @property
    def can_write(self) -> bool:
        """False when export_apps() would refuse: `winget import` rejects a package file with no packages in it."""
        return self.fmt != "winget" or self.written > 0

    def summary(self) -> str:
        """One or two sentences for the message shown after saving (or why nothing can be saved)."""
        if not self.can_write:
            return tr("No app has a winget ID, so no file was saved.")
        parts = [trn("Saved a list of {n} app.", "Saved a list of {n} apps.", self.written)]
        if self.left_out:
            parts.append(trn("{n} app has no winget ID, so it was left out.",
                             "{n} apps have no winget ID, so they were left out.", len(self.left_out)))
        return " ".join(parts)


def _check(fmt: str) -> None:
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}")


def default_name(fmt: str) -> str:
    """File name suggestion, e.g. upstream-apps-2026-11-08.json."""
    _check(fmt)
    stamp = date.today().isoformat()
    return f"upstream-winget-{stamp}.json" if fmt == "winget" else f"upstream-apps-{stamp}.{fmt}"


def file_filter(fmt: str) -> str:
    """Filter text for a save dialog, e.g. 'Spreadsheet (CSV) (*.csv)'."""
    _check(fmt)
    return f"{tr(FORMAT_LABELS[fmt])} (*.{'json' if fmt == 'winget' else fmt})"


def _apps(statuses) -> list:
    """The statuses that belong in a list of installed apps, one per Uninstall entry, ordered by name."""
    seen: dict[str, object] = {}
    for status in statuses:
        app = status.app
        if app.system_component or not app.name.strip() or app.reg_key.startswith(_NOT_INSTALLED):
            continue
        seen.setdefault(app.reg_key, status)
    return sorted(seen.values(), key=lambda s: (s.app.name.casefold(), s.app.version, s.app.reg_key))


def _winget_id(status) -> str | None:
    wid = (status.winget_id or "").strip()
    return wid if _WINGET_ID.match(wid) else None


def _plan(statuses, fmt: str) -> tuple[list, list[str]]:
    """(statuses that go into the file, names of the ones that don't)."""
    _check(fmt)
    apps = _apps(statuses)
    if fmt != "winget":
        return apps, []
    packages: dict[str, object] = {}
    left_out = []
    for status in apps:
        wid = _winget_id(status)
        if wid is None:
            left_out.append(status.app.name)
        else:
            packages.setdefault(wid.casefold(), status)
    return sorted(packages.values(), key=lambda s: _winget_id(s).casefold()), left_out


def export_report(statuses, fmt: str) -> ExportReport:
    """What export_apps() with the same arguments would write and leave out. Nothing is written."""
    included, left_out = _plan(statuses, fmt)
    return ExportReport(fmt, len(included), tuple(left_out))


def _record(status) -> dict:
    app = status.app
    return {"name": app.name, "version": app.version, "publisher": app.publisher, "scope": app.scope, "arch": app.arch,
            "winget_id": _winget_id(status), "github_repo": status.repo or None}


def _cell(text: str) -> str:
    """A spreadsheet runs text that starts with = + - @ as a formula; app names come from other people's installers."""
    return "'" + text if text.startswith(_FORMULA_START) else text


def _json_bytes(statuses) -> bytes:
    doc = {"format": "upstream-apps", "version": 1, "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "apps": [_record(s) for s in statuses]}
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _csv_bytes(statuses) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for s in statuses:
        r = _record(s)
        writer.writerow([_cell(str(r[key] or "")) for key in
                         ("name", "version", "publisher", "scope", "arch", "winget_id", "github_repo")])
    return out.getvalue().encode("utf-8-sig")


def _winget_bytes(statuses) -> bytes:
    doc = {"$schema": WINGET_SCHEMA,
           "CreationDate": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
           "Sources": [{"Packages": [{"PackageIdentifier": _winget_id(s)} for s in statuses],
                        "SourceDetails": dict(WINGET_SOURCE)}]}
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    """Temp file next to the target, then replace: a failure leaves the old file (or nothing) and no half-written list."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_apps(statuses, path: Path, fmt: str) -> int:
    """Write the list and return how many apps were written. Raises OSError / ValueError on failure; a winget file
    with no packages is a ValueError (check export_report().can_write first), because winget would not import it."""
    included, _ = _plan(statuses, fmt)
    if fmt == "winget" and not included:
        raise ValueError("No app has a winget ID, so there is nothing to import")
    data = {"json": _json_bytes, "csv": _csv_bytes, "winget": _winget_bytes}[fmt](included)
    _write_atomic(Path(path), data)
    return len(included)
