"""winget as the 'official site' channel: its manifests point at each vendor's
own installer URLs and pin their SHA-256, so winget downloads from the vendor
and verifies the file. Its manifests also list the GitHub links the scanner scores to find an app's repo."""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from .errors import Cancelled
from .inventory import make_ident
from .versioning import compare

log = logging.getLogger("upstream.winget")

CREATE_NO_WINDOW = 0x08000000
META_VERSION = 2   # the shape of what show() returns; the scanner re-reads older cache entries
_URL = re.compile(r"https?://[^\s\"'<>]+")
_ARP_ID = re.compile(r"^ARP\\(Machine|User)\\(?:X64|X86|ARM64)\\(.+)$", re.I)
_ARCH_TAG = re.compile(r"(?<![a-z0-9])(x64|x86|arm64|amd64|64-bit|32-bit)(?![a-z0-9])", re.I)
_SIZE = re.compile(r"([\d.]+)\s*(KB|MB|GB)\s*/\s*([\d.]+)\s*(KB|MB|GB)", re.I)
_PERCENT = re.compile(r"(\d{1,3})\s*%")
# English lines winget prints once the download is over; other languages are caught by the output's shape (see update).
_INSTALL_STARTS = re.compile(r"starting package install|verified installer hash", re.I)
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


_ident = lru_cache(maxsize=4096)(make_ident)


@lru_cache(maxsize=4096)
def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def _arch_tag(text: str) -> str | None:
    m = _ARCH_TAG.search(text)
    return {"amd64": "x64", "64-bit": "x64", "32-bit": "x86"}.get(m.group(1).lower(), m.group(1).lower()) if m else None


def _pair_score(app, row: WingetRow) -> int:
    """How surely this winget row is this installed entry; 0 when it cannot be."""
    arp = _ARP_ID.match(row.id)
    if arp:   # ARP\Machine\X64\{product code} names the registry entry itself
        same = arp.group(1).lower() == app.scope and arp.group(2).lower() == app.reg_key.rsplit("\\", 1)[-1].lower()
        return 1000 if same else 0
    name, target = _fold(row.name), _fold(app.name)
    if name.endswith("…"):   # winget cuts long display names to the column width
        prefix = name.rstrip("…").strip()
        if not prefix or not target.startswith(prefix):
            return 0
        score = 50
    elif name == target:
        score = 60
    elif _ident(name) and _ident(name) == _ident(target):
        score = 40
    else:
        return 0
    row_arch, app_arch = _arch_tag(row.name) or _arch_tag(row.id), _arch_tag(app.name)
    if row_arch and app_arch and row_arch != app_arch:
        return 0   # the x86 and x64 redistributables share a name prefix
    if row_arch and not app_arch:
        score += 10 if row_arch == app.arch else -30
    if row.version:
        if row.version == app.version:
            score += 30
        elif compare(row.version, app.version) == 0:
            score += 25
        elif row.version in app.name:   # Visual C++ redistributables carry their version in the name
            score += 15
        elif compare(row.version, app.version) is not None:
            score -= 5
    id_words = set(re.findall(r"[a-z0-9]{3,}", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", row.id).lower()))
    shared = id_words & set(re.findall(r"[a-z0-9]{3,}", f"{app.name} {app.publisher}".lower()))
    return max(score + 3 * min(3, len(shared)), 0)


def match_rows(apps, rows: list[WingetRow]) -> dict[str, WingetRow]:
    """reg_key -> winget row. winget prints the Apps & Features display name, truncated with '…' when it
    is long, so a row is compared with every app by name, architecture, version and package id. The closest
    pairs are settled first and a row or an app is never used twice: two entries with the same name (.NET
    runtimes, a per-user and a per-machine copy) each get their own row, and the machine-wide copy wins
    a single row."""
    pairs = []
    for app in apps:
        for index, row in enumerate(rows):
            score = _pair_score(app, row)
            if score > 0:
                pairs.append((-score, app.scope != "machine", app.reg_key, index))
    by_key: dict[str, WingetRow] = {}
    used: set[int] = set()
    for _, _, reg_key, index in sorted(pairs):
        if reg_key not in by_key and index not in used:
            by_key[reg_key] = rows[index]
            used.add(index)
    return by_key


def show(pkg_id: str) -> dict:
    """Manifest facts for one package. Labels are localised, so URLs are
    classified by key text when it is English and by shape otherwise."""
    lines = _run(["show", "--id", pkg_id, "--exact", "--source", "winget"], timeout=120)
    meta = {"v": META_VERSION, "urls": [], "installer_url": None, "installer_type": None,
            "homepage": None, "version": None}
    for line in lines:
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        url = _URL.search(value)
        # Every GitHub link on the page, whatever its label says: the scanner scores them against the app.
        for found in _URL.findall(line):
            found = found.rstrip(".,;)")
            if "github.com/" in found.lower() and found not in meta["urls"] and len(meta["urls"]) < 12:
                meta["urls"].append(found)
        if meta["version"] is None and re.fullmatch(r"\d[\w.\-+]*", value):
            meta["version"] = value
        if url:
            u = url.group(0)
            if "installer" in key or "/releases/download/" in u.lower():
                meta["installer_url"] = meta["installer_url"] or u
            elif key == "homepage":
                meta["homepage"] = u
        elif key == "installer type":
            meta["installer_type"] = value.lower()
    return meta


def parse_versions(lines: list[str]) -> list[str]:
    """The one-column table `winget show --versions` prints: a header, a dashed line, then a version per line.
    The header is localised, the dashes are not. Anything else (package not found) gives an empty list."""
    for i, line in enumerate(lines):
        if i > 0 and re.fullmatch(r"-{3,}", line.strip()):
            body = lines[i + 1:]
            break
    else:
        return []
    found: list[str] = []
    for line in body:
        text = line.strip()
        if not text:
            break
        if text not in found:
            found.append(text)
    return found


def versions(pkg_id: str) -> list[str]:
    """Every version of a package the winget source has a manifest for, newest first. Slow: call from a thread."""
    return parse_versions(_run(["show", "--id", pkg_id, "--exact", "--source", "winget", "--versions"], timeout=120))


def _update_args(pkg_id: str, mode: str, version: str | None) -> list[str]:
    """The command line for an upgrade, a first install, or a rollback (`install --version V --force`).

    winget has no downgrade command: a rollback runs the older installer over the newer copy and --force lets it past
    winget's own "a newer version is already installed" refusal. Whether that works is up to the installer. It cannot
    work for Store/MSIX packages (Windows refuses to replace a package with an older one), MSI products that block
    downgrades (error 1638), Burn bundles that do the same (0x80070666), or an app that migrated its data on upgrade;
    it needs the old version's manifest and installer URL to still exist. Nothing is undone when it fails: the
    newer copy stays and the installer's exit code is reported."""
    flags = ["--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    if mode == "rollback":
        if not version:
            raise ValueError("A rollback needs the version to install")
        return ["winget", "install", "--id", pkg_id, "--version", version, "--exact", "--source", "winget",
                "--force", *flags]
    if mode not in ("upgrade", "install"):
        raise ValueError(f"Unknown winget mode: {mode!r}")
    return ["winget", mode, "--id", pkg_id, "--exact", "--source", "winget", *flags]


def _kill_tree(proc: subprocess.Popen) -> None:
    """End a winget that has not started installing, and whatever it started: taskkill /T on its pid only."""
    if proc.poll() is not None:
        return
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    try:
        subprocess.run([os.path.join(root, "System32", "taskkill.exe"), "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        log.warning("taskkill could not end winget (pid %s)", proc.pid, exc_info=True)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def update(pkg_id: str, mode: str, on_progress, on_line, *, cancel=None, on_install=None,
           version: str | None = None) -> tuple[int, str]:
    """Run `winget upgrade` (or `install` for a package winget did not match to
    this machine, or `install --version` for mode 'rollback', which needs `version`; see _update_args)
    and stream its progress. Returns (exit code, last message).

    `cancel` (a context.CancelToken) ends the run. While winget only prepares and downloads it is terminated with
    everything it started and Cancelled("Cancelled.") is raised. Once the install has begun it is left running
    (stopping an installer half way can leave the app broken): Cancelled says that Upstream stopped waiting.
    `on_progress(percent)` reports the download only; the moment the install begins `on_install()` is called
    once and `on_progress` falls silent. The install is recognised by what winget prints, whatever the language:
    a completed download, a percentage bar (the installer's own), any text after the download started, or
    the English "Starting package install"."""
    if cancel is not None:
        cancel.check()
    args = _update_args(pkg_id, mode, version)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW)
    chunks: queue.Queue[bytes | None] = queue.Queue()
    abandoned = threading.Event()

    def pump():
        """Reads on its own thread so the loop below can notice a cancel. It keeps reading after the loop gave
        up, so a winget that is still installing never blocks on a full pipe."""
        try:
            while chunk := proc.stdout.read1(4096):
                if not abandoned.is_set():
                    chunks.put(chunk)
        finally:
            chunks.put(None)
            proc.stdout.close()

    threading.Thread(target=pump, daemon=True).start()
    last = ""
    buf = b""
    downloading = installing = False

    def begin_install():
        nonlocal installing
        if not installing:
            installing = True
            if on_install:
                on_install()

    while True:
        if cancel is not None and cancel.is_set and proc.poll() is None:
            if not installing:
                _kill_tree(proc)
                raise Cancelled("Cancelled.")
            abandoned.set()
            raise Cancelled("Stopped waiting for the installer. It may still be running.")
        try:
            chunk = chunks.get(timeout=0.1)
        except queue.Empty:
            continue
        if chunk is None:
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
                if not installing:
                    downloading = True
                    on_progress(int(done * 100 / total) if total else -1)
                    if total and done >= total:
                        begin_install()
            elif pct and not re.search(r"[A-Za-z]{4,}", text):
                begin_install()
            elif re.search(r"\w{3,}", text):
                last = text
                on_line(text)
                if downloading or _INSTALL_STARTS.search(text):
                    begin_install()
    code = proc.wait()
    return code & 0xFFFFFFFF, last
