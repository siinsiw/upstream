"""winget as the 'official site' channel: its manifests point at each vendor's
own installer URLs and pin their SHA-256, so winget downloads from the vendor
and verifies the file. Also used to learn which GitHub repo an app ships from."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass

from .github import repo_from_urls

log = logging.getLogger("upstream.winget")

CREATE_NO_WINDOW = 0x08000000
_URL = re.compile(r"https?://[^\s\"'<>]+")
_SIZE = re.compile(r"([\d.]+)\s*(KB|MB|GB)\s*/\s*([\d.]+)\s*(KB|MB|GB)", re.I)
_PERCENT = re.compile(r"(\d{1,3})\s*%")
_UNITS = {"KB": 1, "MB": 1024, "GB": 1024 * 1024}


def available() -> bool:
    return shutil.which("winget") is not None


def _lines(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    # Progress spinners redraw with bare \r; keep what was left on screen.
    return [line.split("\r")[-1].rstrip() for line in text.split("\n")]


def _run(args: list[str], timeout: int = 300) -> list[str]:
    r = subprocess.run(["winget", *args, "--disable-interactivity"], capture_output=True,
                       timeout=timeout, creationflags=CREATE_NO_WINDOW)
    return _lines(r.stdout)


def _width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _cut(row: str, starts: list[int]) -> list[str]:
    cells = [""] * len(starts)
    col, pos = 0, 0
    for ch in row:
        while col + 1 < len(starts) and pos >= starts[col + 1]:
            col += 1
        cells[col] += ch
        pos += _width(ch)
    return [c.strip() for c in cells]


def _parse_table(lines: list[str]) -> list[list[str]]:
    for i, line in enumerate(lines):
        if i > 0 and re.fullmatch(r"-{10,}", line.strip()):
            header, body = lines[i - 1], lines[i + 1:]
            break
    else:
        return []
    starts = [0] + [m.end() for m in re.finditer(r" {2,}", header.rstrip())]
    if len(starts) < 3:
        starts = [0] + [m.end() for m in re.finditer(r" +", header.rstrip())]
    rows = []
    for line in body:
        if not line.strip():
            break
        cells = _cut(line, starts)
        if len(cells) >= 3 and cells[1]:
            rows.append(cells)
    return rows


@dataclass
class WingetRow:
    name: str
    id: str
    version: str
    available: str


def list_installed() -> list[WingetRow]:
    """Installed packages that winget matched to its community source.
    Column order is Name, Id, Version, Available whatever the UI language."""
    rows = []
    for cells in _parse_table(_run(["list", "--source", "winget"], timeout=240)):
        pkg_id = cells[1]
        if pkg_id.endswith("…") or pkg_id.startswith(("ARP\\", "MSIX\\")):
            continue
        rows.append(WingetRow(cells[0], pkg_id, cells[2] if len(cells) > 2 else "",
                              cells[3] if len(cells) > 3 else ""))
    return rows


def match_rows(apps, rows: list[WingetRow]) -> dict[str, WingetRow]:
    """reg_key -> winget row. winget prints the Apps & Features display name,
    truncated with '…' when it is long."""
    by_key: dict[str, WingetRow] = {}
    for row in rows:
        name = row.name.lower()
        truncated = name.endswith("…")
        prefix = name.rstrip("…").strip()
        hits = [a for a in apps if (a.name.lower().startswith(prefix) if truncated else a.name.lower() == name)]
        if len(hits) > 1:
            hits = [a for a in hits if a.version == row.version] or hits[:1]
        for app in hits:
            by_key.setdefault(app.reg_key, row)
    return by_key


def show(pkg_id: str) -> dict:
    """Manifest facts for one package. Labels are localised, so URLs are
    classified by key text when it is English and by shape otherwise."""
    lines = _run(["show", "--id", pkg_id, "--exact", "--source", "winget"], timeout=120)
    meta = {"repo": None, "installer_url": None, "installer_type": None, "homepage": None, "version": None}
    other_urls = []
    for line in lines:
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        url = _URL.search(value)
        if meta["version"] is None and re.fullmatch(r"\d[\w.\-+]*", value):
            meta["version"] = value
        if url:
            u = url.group(0)
            if "installer" in key or "/releases/download/" in u.lower():
                meta["installer_url"] = meta["installer_url"] or u
            elif key == "homepage":
                meta["homepage"] = u
            elif key in ("publisher url", "release notes url"):
                other_urls.append(u)
        elif key == "installer type":
            meta["installer_type"] = value.lower()
    repo_sources = [meta["installer_url"], meta["homepage"], *other_urls]
    meta["repo"] = repo_from_urls(u for u in repo_sources if u)
    return meta


def update(pkg_id: str, mode: str, on_progress, on_line) -> tuple[int, str]:
    """Run `winget upgrade` (or `install` for a package winget did not match to
    this machine) and stream its progress. Returns (exit code, last message)."""
    args = ["winget", mode, "--id", pkg_id, "--exact", "--source", "winget", "--silent",
            "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW)
    last = ""
    buf = b""
    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            text = part.decode("utf-8", "replace").strip()
            if len(text) <= 2:
                continue
            size = _SIZE.search(text)
            pct = _PERCENT.search(text)
            if size:
                done = float(size.group(1)) * _UNITS[size.group(2).upper()]
                total = float(size.group(3)) * _UNITS[size.group(4).upper()]
                on_progress(int(done * 100 / total) if total else -1)
            elif pct and not re.search(r"[A-Za-z]{4,}", text):
                on_progress(int(pct.group(1)))
            elif re.search(r"\w{3,}", text):
                last = text
                on_line(text)
    code = proc.wait()
    return code & 0xFFFFFFFF, last
