"""What an app may have left on the PC, and how sure we are. Read-only.

Nothing here deletes, moves or edits anything. Every candidate passes guard.py
and a check against the other installed apps; what fails either is reported as
'protected' (with the reason) instead of being dropped.

certain = an explicit link (inside the install folder, or a value, shortcut,
service or entry pointing into it); likely = an exact name match under a known
place; possible = a weaker match. A folder guessed from the uninstaller or icon
location is only a guess, so links into it are likely at best."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field

from . import guard
from .inventory import GUID, InstalledApp, make_ident, read_installed_apps

log = logging.getLogger("upstream.leftovers")

CERTAIN, LIKELY, POSSIBLE, PROTECTED = "certain", "likely", "possible", "protected"
_RANK = {CERTAIN: 0, LIKELY: 1, POSSIBLE: 2, PROTECTED: 3}
_DOWN = {CERTAIN: LIKELY, LIKELY: POSSIBLE, POSSIBLE: POSSIBLE}

# Reason templates. The UI translates these; {other} is another app's name.
R_ROOT = "The app's install folder"
R_ROOT_GUESSED = "The install folder, worked out from where the uninstaller or icon is"
R_INSIDE_ROOT = "Inside the app's install folder"
R_INSIDE_OTHER = "Another installed app uses this folder: {other}"
R_HOLDS_OTHER = "Another installed app lives inside this folder: {other}"
R_SAME_NAME = "Another installed app has the same name: {other}"
R_LINK = "A link to somewhere else; Upstream never follows links"
R_NAMED = "Folder named after the app"
R_NAMED_UNDER_PUBLISHER = "Folder named after the app, inside its publisher's folder"
R_LOOKS_LIKE = "Name looks like the app's name"
R_PUBLISHER_EMPTY = "The publisher's folder would be left empty"
R_KEY_NAMED = "Registry key named after the app"
R_KEY_NAMED_UNDER_PUBLISHER = "Registry key named after the app, under its publisher's key"
R_KEY_LOOKS_LIKE = "Registry key whose name looks like the app's name"
R_KEY_REFERS = "Registry key with values that point into the install folder"
R_KEY_OTHER = "Registry key that points into another installed app: {other}"
R_SHORTCUT_TARGET = "Shortcut to a program in the install folder"
R_SHORTCUT_FOLDER = "Shortcut in the app's own Start menu folder"
R_SHORTCUT_DANGLING = "Shortcut named after the app whose program no longer exists"
R_SHORTCUT_OTHER = "Shortcut to another installed app: {other}"
R_START_FOLDER = "Start menu folder that would be empty without the app's shortcuts"
R_RUN = "Startup entry that runs a program from the install folder"
R_RUN_FLAG = "On/off switch of a startup entry that points into the install folder"
R_APP_PATHS = "App Paths entry for a program in the install folder"
R_SERVICE = "Windows service that runs a program from the install folder"
R_UNINSTALL_OWN = "The app's own entry in Settings > Apps"
R_UNINSTALL_TWIN = "A second Settings > Apps entry for the same app, and its uninstaller is gone"
R_UNINSTALL_TWIN_LIVE = "A matching Settings > Apps entry still has a working uninstaller"
R_UNINSTALL_TWIN_UNCHECKED = "A matching Settings > Apps entry whose uninstaller Upstream could not check"
C_PROGRAMS = "It contains programs (.exe or .dll files), so check it before removing"
C_RECENT = "It was changed in the last 10 minutes"
C_LARGE = "It is large (over 5,000 files or 2 GB)"

# Every English template the scan can produce, for the translation table.
REASONS = tuple(v for k, v in sorted(globals().items()) if k[:2] in ("R_", "C_") and isinstance(v, str)) \
    + guard.REASONS

MAX_FILES = 5000
MAX_BYTES = 2 * 1024 ** 3
_MEASURE_SECONDS = 2.0
_ATTR_REPARSE = 0x400
_CREATE_NO_WINDOW = 0x08000000

_PUBLISHER_NOISE = {"inc", "incorporated", "llc", "ltd", "limited", "gmbh", "corp", "corporation", "co", "company",
                    "sa", "sro", "ag", "bv", "oy", "ab", "plc", "team", "the", "pty", "kg", "srl", "spa", "lp", "llp"}
# Names too generic to prove anything on their own (the design's stop-list).
_STOP = {"data", "config", "settings", "cache", "logs", "temp", "common", "shared", "update", "updater", "bin", "lib",
         "plugins", "tools", "users", "default", "profiles", "backup", "packages", "microsoft", "windows", "terminal",
         "notepad", "app", "apps", "application", "program", "programs", "setup", "installer", "install", "uninstall",
         "resources", "assets", "runtime", "runtimes", "core", "client", "server", "service", "services", "launcher",
         "player", "editor", "viewer", "manager", "helper", "system", "office", "studio", "code", "desktop", "browser",
         "driver", "drivers", "media", "games", "game", "music", "video", "videos", "files", "documents", "software"}
_GENERIC_DIRS = {"installer", "uninstall", "uninstaller", "bin", "bin32", "bin64", "application", "app", "x64", "x86",
                 "win32", "win64", "amd64", "release", "setup", "program", "programs"}
_VERSION_DIR = re.compile(r"^(?:app-|v)?\d+(?:[.\-_]\d+)*$", re.I)
_FILE_SUFFIXES = (".exe", ".dll", ".ico", ".msi", ".lnk", ".bat", ".cmd", ".ocx", ".sys")
_PATH_COMMAND = re.compile(r"(.+?\.(?:exe|dll|sys|bat|cmd|com|msi|lnk|ico|ocx))(?=[\s,]|$)", re.I)

_UNINSTALL = "Microsoft\\Windows\\CurrentVersion\\Uninstall"
_VIEWS = {  # label used by inventory -> (hive, physical path in the 64-bit view)
    "HKLM64": ("HKLM", f"SOFTWARE\\{_UNINSTALL}"),
    "HKLM32": ("HKLM", f"SOFTWARE\\WOW6432Node\\{_UNINSTALL}"),
    "HKCU": ("HKCU", f"SOFTWARE\\{_UNINSTALL}"),
}
_HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
_SOFTWARE_ROOTS = [("HKCU", "Software"), ("HKCU", "Software\\Wow6432Node"),
                   ("HKLM", "SOFTWARE"), ("HKLM", "SOFTWARE\\WOW6432Node")]
_CURRENT = "Microsoft\\Windows\\CurrentVersion"
_RUN_KEYS = [("HKCU", f"Software\\{_CURRENT}\\Run"), ("HKCU", f"Software\\{_CURRENT}\\RunOnce"),
             ("HKLM", f"SOFTWARE\\{_CURRENT}\\Run"), ("HKLM", f"SOFTWARE\\{_CURRENT}\\RunOnce"),
             ("HKLM", f"SOFTWARE\\WOW6432Node\\{_CURRENT}\\Run"), ("HKLM", f"SOFTWARE\\WOW6432Node\\{_CURRENT}\\RunOnce")]
_APP_PATHS = [("HKCU", f"Software\\{_CURRENT}\\App Paths"), ("HKLM", f"SOFTWARE\\{_CURRENT}\\App Paths"),
              ("HKLM", f"SOFTWARE\\WOW6432Node\\{_CURRENT}\\App Paths")]
_SERVICES = "SYSTEM\\CurrentControlSet\\Services"


@dataclass
class Leftover:
    kind: str          # folder | file | regkey | regvalue | shortcut | service
    location: str      # full path, "HKCU\\Software\\X", "HKLM\\...|ValueName" for values
    confidence: str    # certain | likely | possible | protected
    reason: str        # English template; the UI translates it
    reason_values: dict = field(default_factory=dict)
    size: int | None = None    # bytes, None when unknown or the walk was cut short
    caveat: str = ""           # English template: why the confidence was lowered
    large: bool = False        # over MAX_FILES files or MAX_BYTES: never preselect


# ---------------------------------------------------------------- text helpers

def _compact(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _name_keys(text: str) -> set[str]:
    return {_compact(text), _compact(make_ident(text))} - {""}


def _within_one(a: str, b: str) -> bool:
    """Equal, or one insertion, deletion or substitution apart."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] if len(a) == len(b) else a[i:] == b[i + 1:]


def _publisher_words(publisher: str) -> list[list[str]]:
    """The publisher as folders spell it: whole, without trailing company
    suffixes one at a time ('Lone Co Ltd.' -> 'lone co' -> 'lone'), and without any."""
    words = re.findall(r"\w+", publisher.lower().replace(".", ""))
    candidates, trimmed = [words], words
    while len(trimmed) > 1 and trimmed[-1] in _PUBLISHER_NOISE:
        trimmed = trimmed[:-1]
        candidates.append(trimmed)
    candidates.append([w for w in words if w not in _PUBLISHER_NOISE])
    variants: list[list[str]] = []
    for candidate in candidates:
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


class _Names:
    """The app's name in the shapes folders and keys are usually written in."""

    def __init__(self, name: str, publisher: str):
        bare = make_ident(re.sub(r"\([^)]*\)", " ", name))
        variants = {make_ident(name), bare}
        for ident in list(variants):
            words = ident.split()
            while len(words) > 1 and words[-1].isdigit() and len(words[-1]) <= 4:
                words = words[:-1]  # "PostgreSQL 17" is usually a folder called PostgreSQL
                variants.add(" ".join(words))
        pub = _publisher_words(publisher)
        for ident in list(variants):
            words = ident.split()
            for lead in pub:
                if len(words) > len(lead) and words[:len(lead)] == lead:
                    variants.add(" ".join(words[len(lead):]))  # "Mozilla Firefox" -> "firefox"
        self.keys = {_compact(v) for v in variants} - {""}
        self.words = set(bare.split())
        self.publisher_keys = {k for k in (_compact("".join(v)) for v in pub) if len(k) >= 4}

    def classify(self, folder: str, under_publisher: bool = False) -> tuple[str, str] | None:
        """('exact' | 'weak' | 'fuzzy', key). 'weak' is an exact match on a name
        too short or too generic to count without other evidence."""
        keys = _name_keys(folder)
        hit = keys & self.keys
        if hit:
            key = max(hit, key=len)
            return ("exact" if under_publisher or self.usable(key) else "weak"), key
        for key in keys:
            if len(key) >= 6 and key not in _STOP and key not in self.publisher_keys \
                    and any(len(k) >= 6 and _within_one(key, k) for k in self.keys):
                return "fuzzy", key
        folder_key = _compact(make_ident(folder))
        if self.words and any(len(k) >= 6 for k in self.keys) and self.words <= set(make_ident(folder).split()) \
                and folder_key not in _STOP and folder_key not in self.publisher_keys:
            return "fuzzy", folder_key
        return None

    def is_publisher(self, folder: str) -> bool:
        return bool(_name_keys(folder) & self.publisher_keys)

    @staticmethod
    def usable(key: str) -> bool:
        return len(key) >= 5 and key not in _STOP


# --------------------------------------------------------------- path helpers

def _exe_of(command: str) -> str:
    """The program in a command line: quoted, or unquoted with spaces in its path."""
    text = os.path.expandvars((command or "").strip())
    if text.startswith("\\??\\"):
        text = text[4:]
    elif text[:12].lower() == "\\systemroot\\":
        text = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), text[12:])
    if text.startswith('"'):
        return text[1:].split('"', 1)[0]
    found = _PATH_COMMAND.match(text)
    return found.group(1) if found else text.split(" ", 1)[0]


def _clean(raw: str) -> str:
    """A full folder path from registry text, or ''. A file path gives its folder."""
    text = os.path.expandvars((raw or "").strip().strip('"').strip())
    if not re.match(r"^[A-Za-z]:[\\/]", text):
        return ""
    text = os.path.normpath(text)
    return os.path.dirname(text) if text.lower().endswith(_FILE_SUFFIXES) else text


def _folders_of(*commands: str) -> list[str]:
    folders = []
    for command in commands:
        exe = _exe_of(command)
        if re.match(r"^[A-Za-z]:[\\/]", exe):
            folders.append(os.path.dirname(os.path.normpath(exe)))
    return folders


def _into(norm: str) -> re.Pattern:
    """Text that names this normalized folder or something inside it: a path, or a command line holding one."""
    return re.compile(r"(?:^|[\s\"'=,;(\[])" + re.escape(norm) + r"(?=$|[\\\s\"',;)\]])")


def _is_link(path: str) -> bool:
    """Junctions, symlinks and other reparse points, including a parent that is one."""
    try:
        st = os.lstat(path)
        if st.st_file_attributes & _ATTR_REPARSE or stat.S_ISLNK(st.st_mode):
            return True
        return guard.normalize(os.path.realpath(path)) != guard.normalize(path)
    except OSError:
        return False


def _ancestors(norm: str):
    """Parents of a normalized path, nearest first, stopping before the drive root."""
    current = norm
    while True:
        parent = os.path.dirname(current)
        if parent == current or len(parent) <= 3:
            return
        yield parent
        current = parent


def _measure(path: str) -> tuple[int | None, bool]:
    """(bytes, large). bytes is None when the walk was cut short, which counts as large."""
    total = files = 0
    deadline = time.monotonic() + _MEASURE_SECONDS
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None, True
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_file_attributes & _ATTR_REPARSE:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                        continue
                    files += 1
                    total += st.st_size
                    if files > MAX_FILES:
                        return None, True
        except OSError:
            continue
    return total, total > MAX_BYTES


def _changed_recently(path: str) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime < 600
    except OSError:
        return False


def _has_programs(path: str) -> bool:
    """An .exe or .dll in the folder or one level down."""
    try:
        with os.scandir(path) as entries:
            for seen, entry in enumerate(entries):
                if seen >= 300:
                    break
                if entry.name.lower().endswith((".exe", ".dll")):
                    return True
                if entry.is_dir(follow_symlinks=False):
                    try:
                        with os.scandir(entry.path) as inner:
                            if any(sub.name.lower().endswith((".exe", ".dll")) for sub in inner):
                                return True
                    except OSError:
                        continue
    except OSError:
        pass
    return False


# ------------------------------------------------------------ registry helpers

def _open(hive: str, sub: str):
    return winreg.OpenKey(_HIVES[hive], sub, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)


def _subkeys(hive: str, sub: str) -> list[str]:
    names: list[str] = []
    try:
        with _open(hive, sub) as key:
            while len(names) < 5000:
                try:
                    names.append(winreg.EnumKey(key, len(names)))
                except OSError:
                    break
    except OSError:
        pass
    return names


def _strings(hive: str, sub: str, limit: int = 64) -> list[tuple[str, str]]:
    """(name, text) for the string values of a key; empty when it can't be read."""
    out: list[tuple[str, str]] = []
    try:
        with _open(hive, sub) as key:
            for index in range(limit):
                try:
                    name, data, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(data, str) and data:
                    out.append((name, os.path.expandvars(data)))
    except OSError:
        pass
    return out


def _string(hive: str, sub: str, name: str) -> str:
    try:
        with _open(hive, sub) as key:
            data, kind = winreg.QueryValueEx(key, name)
        return os.path.expandvars(data) if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(data, str) else ""
    except OSError:
        return ""


def _key_exists(hive: str, sub: str) -> bool:
    try:
        _open(hive, sub).Close()
        return True
    except OSError:
        return False


_msi = ctypes.WinDLL("msi")
_msi.MsiQueryProductStateW.argtypes = [wintypes.LPCWSTR]
_msi.MsiQueryProductStateW.restype = ctypes.c_int


def _msi_installed(product_code: str) -> bool:
    """INSTALLSTATE_LOCAL, SOURCE or DEFAULT: Windows Installer still has the product."""
    return _msi.MsiQueryProductStateW(product_code) >= 3


def _program_of(command: str) -> str:
    """The program a command line starts. Only a line that begins with a quote, a drive or a
    UNC/NT prefix names a path; otherwise the first token is a bare name Windows finds on PATH.
    Without this, 'RunDll32 C:\\Windows\\...\\x.dll,Entry' would read as one missing path."""
    text = os.path.expandvars((command or "").strip())
    if re.match(r'(?:"|[A-Za-z]:[\\/]|\\\\|\\\?\?\\|\\systemroot\\)', text, re.I):
        return _exe_of(text)
    return text.split(None, 1)[0] if text else ""


def _twin_uninstaller_gone(sub: str, command: str) -> bool | None:
    """Whether a twin Uninstall entry's uninstaller is gone: True, False, or None when the
    command can't be checked. A bare program name (MsiExec.exe, rundll32, winget) is found by
    Windows, not in the current folder, so it is looked up on PATH instead of as a file path."""
    program = _program_of(command)
    if re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", sub) and (not command or "msiexec" in command.lower()):
        return not _msi_installed(sub)
    if not command:
        return True
    if os.path.basename(program).lower() in ("msiexec", "msiexec.exe"):
        found = GUID.search(command)   # the product is named in the command, whatever the key is called
        return not _msi_installed(found.group(0)) if found else None
    if re.search(r"[\\/]", program):
        return not os.path.exists(program)
    return False if shutil.which(program) else None


@dataclass
class _Entry:
    """A raw Uninstall entry, including the ones inventory hides."""
    view: str
    sub: str
    name: str
    publisher: str
    location: str
    uninstall: str
    icon: str
    parent: str
    inno_path: str

    @property
    def key(self) -> str:
        return f"{self.view}\\{self.sub}".lower()


def _text(key, name: str) -> str:
    try:
        data, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    ok = kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(data, str)
    return os.path.expandvars(data.strip()) if ok else ""


def _uninstall_entries() -> list[_Entry]:
    entries = []
    for view, (hive, path) in _VIEWS.items():
        for sub in _subkeys(hive, path):
            try:
                key = _open(hive, f"{path}\\{sub}")
            except OSError:
                continue
            with key:
                entries.append(_Entry(
                    view, sub, _text(key, "DisplayName"), _text(key, "Publisher"), _text(key, "InstallLocation"),
                    _text(key, "UninstallString"), _text(key, "DisplayIcon"),
                    _text(key, "ParentKeyName") or _text(key, "ParentDisplayName"), _text(key, "Inno Setup: App Path")))
    return entries


# ----------------------------------------------------------------- shortcuts

_LNK_CACHE: dict[str, tuple[int, str]] = {}  # lower-case path -> (mtime_ns, target)


def _walk_shortcuts(base: str, recursive: bool) -> dict[str, int]:
    found: dict[str, int] = {}
    stack = [base]
    while stack and len(found) < 4000:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_file_attributes & _ATTR_REPARSE:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if recursive:
                            stack.append(entry.path)
                    elif entry.name.lower().endswith((".lnk", ".url")):
                        found[entry.path] = st.st_mtime_ns
        except OSError:
            continue
    return found


def _read_targets(recursive: list[str], flat: list[str]) -> dict[str, str] | None:
    """Shortcut targets via WScript.Shell, which also understands the shortcuts
    that MSI installers write. One PowerShell call; it only reads."""
    def quote(path: str) -> str:
        return "'" + path.replace("'", "''") + "'"

    walk = "".join(f"Get-ChildItem -LiteralPath {quote(d)} -Filter *.lnk -File {flag}-Force | ForEach-Object {{ "
                   "$t = ''; try { $t = $sh.CreateShortcut($_.FullName).TargetPath } catch {}; "
                   "[Console]::Out.WriteLine($_.FullName + [char]9 + $t) }; "
                   for d, flag in [(d, "-Recurse ") for d in recursive] + [(d, "") for d in flat])
    script = ("$ErrorActionPreference = 'SilentlyContinue'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "$sh = New-Object -ComObject WScript.Shell; " + walk)
    try:
        run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, timeout=90, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Could not resolve shortcut targets: %s", e)
        return None
    targets = {}
    for line in run.stdout.decode("utf-8-sig", "replace").splitlines():
        path, _, target = line.partition("\t")
        if path:
            targets[path.lower()] = target.strip()
    return targets if run.returncode == 0 else None


def _shortcut_targets(files: dict[str, int], recursive: list[str], flat: list[str]) -> dict[str, str]:
    """Target per shortcut; PowerShell runs only when a shortcut is new or changed."""
    lnks = {p: m for p, m in files.items() if p.lower().endswith(".lnk")}
    if any(_LNK_CACHE.get(p.lower(), (None, ""))[0] != m for p, m in lnks.items()):
        resolved = _read_targets(recursive, flat)
        if resolved is not None:
            for path, mtime in lnks.items():
                _LNK_CACHE[path.lower()] = (mtime, resolved.get(path.lower(), ""))
    return {p: _LNK_CACHE.get(p.lower(), (0, ""))[1] for p in lnks}


# ------------------------------------------------------------------- the scan

@dataclass
class _Root:
    norm: str
    certain: bool           # recorded by the installer, not guessed
    pattern: re.Pattern


class _Scan:
    def __init__(self, app: InstalledApp, others: list[InstalledApp]):
        self.app = app
        self.names = _Names(app.name, app.publisher)
        self.found: dict[tuple[str, str], Leftover] = {}
        self.owners: dict[str, str] = {}           # normalized root of another app -> its name
        self.owner_parents: dict[str, str] = {}    # every folder above such a root
        self._owner_patterns: list[tuple[str, re.Pattern, str]] | None = None
        self.other_keys: dict[str, str] = {}       # name key of another app -> its name
        self.roots: list[_Root] = []
        self.twins: list[_Entry] = []
        self.publisher_folders: list[str] = []
        self._indexed: set[str] = set()
        self.entries = _uninstall_entries()
        self._dirs: dict[str, list[tuple[str, bool]]] = {}
        self._index_owners(others)
        self._find_roots()

    # ownership ---------------------------------------------------------------

    def _own_paths(self) -> list[str]:
        app = self.app
        raw = [app.install_location, getattr(app, "inno_app_path", "")]
        raw += _folders_of(getattr(app, "uninstall_string", ""), getattr(app, "quiet_uninstall_string", ""), app.icon_path)
        return [guard.normalize(p) for p in map(_clean, raw) if p]

    def _add_owners(self, raws: list[str], label: str):
        for path in map(_clean, raws):
            if not path or path.lower() in self._indexed:
                continue
            self._indexed.add(path.lower())
            if guard.protected_path(path):
                continue  # a container or system folder is nobody's own
            norm = guard.normalize(path)
            self.owners.setdefault(norm, label)
            for parent in _ancestors(norm):
                self.owner_parents.setdefault(parent, label)

    def _add_keys(self, name: str, publisher: str, label: str):
        for key in _Names(name, publisher).keys:
            self.other_keys.setdefault(key, label)

    def _index_owners(self, others: list[InstalledApp]):
        """Every other install the PC knows about: the visible apps, then the
        Uninstall entries inventory hides (components, helpers, twins)."""
        app, mine = self.app, self._own_paths()
        known = {app.reg_key.lower()}
        for other in others:
            if other.reg_key.lower() in known:
                continue
            known.add(other.reg_key.lower())
            self._add_keys(other.name, other.publisher, other.name)
            self._add_owners([other.install_location, getattr(other, "inno_app_path", ""),
                              *_folders_of(getattr(other, "uninstall_string", ""), other.icon_path)], other.name)
        own_sub = app.reg_key.partition("\\")[2].lower()
        for entry in self.entries:
            if entry.key in known or not (entry.name or entry.location or entry.uninstall):
                continue
            if "--uninstall-app-id" in entry.uninstall:
                continue  # a website installed as an app: it runs from the browser's folder, it doesn't own it
            if entry.parent and entry.parent.lower() in (own_sub, app.name.lower()):
                continue  # a part of this app's own bundle
            raws = [entry.location, entry.inno_path, *_folders_of(entry.uninstall, entry.icon)]
            paths = [p for p in map(_clean, raws) if p]
            if entry.name and make_ident(entry.name) == app.ident \
                    and (not paths or any(self._overlaps(guard.normalize(p), mine) for p in paths)):
                self.twins.append(entry)
                continue
            self._add_owners(raws, entry.name or entry.sub)
            if entry.name:
                self._add_keys(entry.name, entry.publisher, entry.name)

    @staticmethod
    def _overlaps(norm: str, others: list[str]) -> bool:
        return any(guard.inside(norm, o) or guard.inside(o, norm) for o in others)

    def _owner_of(self, norm: str) -> str | None:
        """Name of the other app whose folder is this path or contains it."""
        for current in (norm, *_ancestors(norm)):
            if current in self.owners:
                return self.owners[current]
        return None

    def _holds_owner(self, norm: str) -> str | None:
        return self.owner_parents.get(norm)

    def _root_of(self, norm: str) -> _Root | None:
        return next((r for r in self.roots if guard.inside(norm, r.norm)), None)

    @staticmethod
    def _spelled(text: str) -> str:
        return os.path.normcase(os.path.expandvars(text)).replace("\\??\\", "")

    def _refers_to(self, text: str) -> _Root | None:
        """The install root a command line or registry value points into, if any."""
        lowered = self._spelled(text)
        return next((r for r in self.roots if r.norm in lowered and r.pattern.search(lowered)), None)

    def _owner_in(self, text: str) -> str | None:
        """Name of another app whose folder a command line or registry value points into, whether it
        holds a program, a command with arguments or just a folder."""
        if self._owner_patterns is None:
            self._owner_patterns = [(norm, _into(norm), label) for norm, label in self.owners.items()]
        lowered = self._spelled(text)
        return next((label for norm, pattern, label in self._owner_patterns
                     if norm in lowered and pattern.search(lowered)), None)

    # install folders -----------------------------------------------------------

    def _find_roots(self):
        app = self.app
        candidates = [(app.install_location, "recorded"), (getattr(app, "inno_app_path", ""), "inno")]
        guesses = [*_folders_of(getattr(app, "uninstall_string", ""), getattr(app, "quiet_uninstall_string", ""),
                                app.icon_path)]
        guesses += [c for c in (_clean(r) for r, _ in candidates) if c and self._generic(os.path.basename(c))]
        seen: set[str] = set()
        for raw, source in candidates:
            path = _clean(raw)
            if path and guard.normalize(path) not in seen:
                seen.add(guard.normalize(path))
                self._accept_root(path, source)
        for start in guesses:
            path = self._walk_up(_clean(start))
            if path and guard.normalize(path) not in seen:
                seen.add(guard.normalize(path))
                self._accept_root(path, "derived")

    @staticmethod
    def _generic(folder: str) -> bool:
        return folder.lower() in _GENERIC_DIRS or bool(_VERSION_DIR.match(folder))

    def _walk_up(self, start: str) -> str:
        """The product folder above an uninstaller or icon: skip version and
        generic folders (Installer, bin, app-1.2.3) for at most three levels. The folder
        has to be named after the product or sit in its publisher's folder. Sitting in
        Program Files is not enough: 'Steam' (the uninstall command of its redistributables
        is steam.exe) and 'Microsoft Visual Studio' hold other products too."""
        folder = start
        for _ in range(4):
            name = os.path.basename(folder)
            if not name:
                return ""
            if self._generic(name):
                folder = os.path.dirname(folder)
                continue
            parent = os.path.dirname(folder)
            hit = self.names.classify(name)   # a short exact name (X-VPN) is fine: the uninstaller is in this folder
            named = bool(hit) and (hit[0] == "exact" or hit[0] == "weak" and hit[1] not in _STOP)
            under_publisher = guard.normalize(os.path.dirname(parent)) in guard.containers() \
                and self.names.is_publisher(os.path.basename(parent))
            return folder if named or under_publisher else ""
        return ""

    def _accept_root(self, path: str, source: str):
        """Decide whether this folder can be used as evidence, and report it if
        it is a real folder that must stay."""
        norm = guard.normalize(path)
        exists = os.path.isdir(path)
        why = guard.protected_path(path)
        owner = self._owner_of(norm)
        if why or owner or (exists and _is_link(path)):
            if exists and source != "derived":
                if why:
                    self._add("folder", path, PROTECTED, why)
                elif owner:
                    self._add("folder", path, PROTECTED, R_INSIDE_OTHER, {"other": owner})
                else:
                    self._add("folder", path, PROTECTED, R_LINK)
            return
        if any(guard.inside(norm, r.norm) for r in self.roots):
            return  # already covered by a folder above it
        self.roots.append(_Root(norm, source != "derived", _into(norm)))
        if exists:
            holds = self._holds_owner(norm)
            if holds:
                self._add("folder", path, PROTECTED, R_HOLDS_OTHER, {"other": holds})
            else:
                self._folder(path, CERTAIN if source != "derived" else LIKELY,
                             R_ROOT if source != "derived" else R_ROOT_GUESSED, root=True)

    # collecting findings -------------------------------------------------------

    def _add(self, kind: str, location: str, confidence: str, reason: str, values: dict | None = None,
             size: int | None = None, caveat: str = "", large: bool = False):
        key = (kind, location.lower() if kind in ("regkey", "regvalue", "service") else guard.normalize(location))
        old = self.found.get(key)
        if old is None or _RANK[confidence] < _RANK[old.confidence]:
            self.found[key] = Leftover(kind, location, confidence, reason, values or {}, size, caveat, large)

    def _level(self, norm: str, level: str, reason: str) -> tuple[str, str]:
        """Inside an install folder the link is explicit, whatever found it."""
        root = self._root_of(norm)
        if not root:
            return level, reason
        return (CERTAIN if root.certain else LIKELY), R_INSIDE_ROOT

    def _folder(self, path: str, level: str, reason: str, values: dict | None = None, *, root: bool = False,
                key: str = "", caveats: bool = True):
        norm = guard.normalize(path)
        why = guard.protected_path(path)
        owner = self._owner_of(norm) or ""
        holds = "" if root else (self._holds_owner(norm) or "")
        if why:
            return self._add("folder", path, PROTECTED, why)
        if owner:
            return self._add("folder", path, PROTECTED, R_INSIDE_OTHER, {"other": owner})
        if holds:
            return self._add("folder", path, PROTECTED, R_HOLDS_OTHER, {"other": holds})
        if _is_link(path):
            return self._add("folder", path, PROTECTED, R_LINK)
        if not root:
            level, reason = self._level(norm, level, reason)
            if level != CERTAIN and key and key in self.other_keys:
                return self._add("folder", path, PROTECTED, R_SAME_NAME, {"other": self.other_keys[key]})
        size, large = _measure(path)
        caveat = ""
        if caveats and not root and not self._root_of(norm):
            if _has_programs(path):
                level, caveat = _DOWN[level], C_PROGRAMS
            elif large:
                level, caveat = _DOWN[level], C_LARGE
            elif _changed_recently(path):
                level, caveat = _DOWN[level], C_RECENT
        self._add("folder", path, level, reason, values, size, caveat, large)

    def _listing(self, path: str) -> list[tuple[str, bool]]:
        if path not in self._dirs:
            try:
                with os.scandir(path) as entries:
                    self._dirs[path] = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries]
            except OSError:
                self._dirs[path] = []
        return self._dirs[path]

    # folders under the usual containers ------------------------------------------

    def folders(self):
        where = guard.locations()
        containers = [where.get(k, "") for k in ("roaming", "local", "local_programs", "locallow", "programdata",
                                                 "programfiles", "programfiles_x86")]
        local = where.get("local", "")
        if local:
            containers += [os.path.join(local, "VirtualStore", sub) for sub in ("Program Files", "Program Files (x86)",
                                                                                  "ProgramData")]
        for container in dict.fromkeys(c for c in containers if c and os.path.isdir(c)):
            for name, is_dir in self._listing(container):
                if not is_dir:
                    continue
                path = os.path.join(container, name)
                hit = self.names.classify(name)
                if hit and hit[0] in ("exact", "fuzzy"):
                    self._named_folder(path, hit, R_NAMED)
                if self.names.is_publisher(name) and not guard.protected_path(path):
                    self.publisher_folders.append(path)
                    for child, child_is_dir in self._listing(path):
                        found = self.names.classify(child, True) if child_is_dir else None
                        if found and found[0] in ("exact", "fuzzy"):
                            self._named_folder(os.path.join(path, child), found, R_NAMED_UNDER_PUBLISHER)

    def _named_folder(self, path: str, hit: tuple[str, str], reason: str):
        kind, key = hit
        if kind == "fuzzy":
            self._folder(path, POSSIBLE, R_LOOKS_LIKE, key=key)
        else:
            self._folder(path, LIKELY, reason, key=key)

    def emptied_publisher_folders(self):
        """A publisher folder whose only contents are things already found."""
        for path in self.publisher_folders:
            names = [n for n, _ in self._listing(path)]
            if names and all(self._reported_folder(os.path.join(path, n)) for n in names):
                self._folder(path, POSSIBLE, R_PUBLISHER_EMPTY, caveats=False)

    def _reported_folder(self, path: str) -> bool:
        return self._reported(("folder", guard.normalize(path)))

    # registry -----------------------------------------------------------------

    def _key_refs(self, hive: str, sub: str) -> tuple[_Root | None, str]:
        """(install root any value of the key points into, other app any value points into). The key
        and its children are read to the end, so the answer does not depend on the order they are
        listed in: one value that belongs to another app makes the whole key theirs."""
        root, other = None, ""
        for path in [sub, *[f"{sub}\\{child}" for child in _subkeys(hive, sub)[:200]]]:
            for _, text in _strings(hive, path):
                root = root or self._refers_to(text)
                other = other or self._owner_in(text) or ""
        return root, other

    def _regkey(self, hive: str, sub: str, hit: tuple[str, str] | None, reason: str):
        location = f"{hive}\\{sub}"
        why = guard.protected_key(location)
        if why:
            return self._add("regkey", location, PROTECTED, why)
        root, other = self._key_refs(hive, sub)
        if other:
            return self._add("regkey", location, PROTECTED, R_KEY_OTHER, {"other": other})
        if root:
            return self._add("regkey", location, CERTAIN if root.certain else LIKELY, R_KEY_REFERS)
        if not hit or hit[0] not in ("exact", "fuzzy"):
            return
        if hit[1] in self.other_keys:
            return self._add("regkey", location, PROTECTED, R_SAME_NAME, {"other": self.other_keys[hit[1]]})
        if hit[0] == "fuzzy":
            return self._add("regkey", location, POSSIBLE, R_KEY_LOOKS_LIKE)
        self._add("regkey", location, LIKELY, reason)

    def software_keys(self):
        for hive, root in _SOFTWARE_ROOTS:
            for name in _subkeys(hive, root):
                sub = f"{root}\\{name}"
                hit = self.names.classify(name)
                if hit:
                    self._regkey(hive, sub, hit, R_KEY_NAMED)
                if self.names.is_publisher(name) and not guard.protected_key(f"{hive}\\{sub}"):
                    for child in _subkeys(hive, sub):
                        found = self.names.classify(child, True)
                        if found:
                            self._regkey(hive, f"{sub}\\{child}", found, R_KEY_NAMED_UNDER_PUBLISHER)

    def _protect_or(self, kind: str, location: str, why: str | None, owner: str | None) -> bool:
        """Report the item as protected when the guard or another app claims it."""
        if why:
            self._add(kind, location, PROTECTED, why)
        elif owner:
            self._add(kind, location, PROTECTED, R_INSIDE_OTHER, {"other": owner})
        return bool(why or owner)

    def startup_values(self):
        for hive, sub in _RUN_KEYS:
            wow = "\\WOW6432Node" in sub
            approved = sub.replace("\\WOW6432Node", "", 1).replace("\\Run", "\\Explorer\\StartupApproved\\Run", 1)
            approved += "32" if wow else ""
            try:
                key = _open(hive, sub)
            except OSError:
                continue
            with key:
                for index in range(500):
                    try:
                        name, data, kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    root = self._refers_to(data) if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) \
                        and isinstance(data, str) else None
                    if not root:
                        continue
                    location = f"{hive}\\{sub}|{name}"
                    if self._protect_or("regvalue", location, guard.protected_value(f"{hive}\\{sub}", name),
                                        self._owner_in(data)):
                        continue
                    level = CERTAIN if root.certain else LIKELY
                    self._add("regvalue", location, level, R_RUN)
                    flag = f"{hive}\\{approved}|{name}"
                    if self._has_value(hive, approved, name) and not guard.protected_value(f"{hive}\\{approved}", name):
                        self._add("regvalue", flag, level, R_RUN_FLAG)

    @staticmethod
    def _has_value(hive: str, sub: str, name: str) -> bool:
        try:
            with _open(hive, sub) as key:
                winreg.QueryValueEx(key, name)
            return True
        except OSError:
            return False

    def app_paths(self):
        for hive, sub in _APP_PATHS:
            for name in _subkeys(hive, sub):
                values = dict(_strings(hive, f"{sub}\\{name}", 16))
                root = self._refers_to(values.get("", "")) or self._refers_to(values.get("Path", ""))
                if not root:
                    continue
                location = f"{hive}\\{sub}\\{name}"
                owner = self._owner_in(values.get("", "")) or self._owner_in(values.get("Path", ""))
                if not self._protect_or("regkey", location, guard.protected_key(location), owner):
                    self._add("regkey", location, CERTAIN if root.certain else LIKELY, R_APP_PATHS)

    def services(self):
        if not self.roots:
            return
        for name in _subkeys("HKLM", _SERVICES):
            image = _string("HKLM", f"{_SERVICES}\\{name}", "ImagePath")
            root = self._refers_to(image) if image else None
            if not root:
                continue
            location = f"HKLM\\{_SERVICES}\\{name}"
            if not self._protect_or("service", location, guard.protected_key(location), self._owner_in(image)):
                self._add("service", location, CERTAIN if root.certain else LIKELY, R_SERVICE)

    def uninstall_entries(self):
        """The app's own entry if it is still there, and twin entries of the
        same product (the 32-bit view of a 64-bit app, say)."""
        app = self.app
        view, _, sub = app.reg_key.partition("\\")
        if view in _VIEWS:
            hive, path = _VIEWS[view]
            if _key_exists(hive, f"{path}\\{sub}"):
                self._add("regkey", f"{hive}\\{path}\\{sub}", CERTAIN, R_UNINSTALL_OWN)
        for entry in self.twins:
            hive, path = _VIEWS[entry.view]
            location = f"{hive}\\{path}\\{entry.sub}"
            if guard.protected_key(location):
                continue
            gone = _twin_uninstaller_gone(entry.sub, entry.uninstall)
            if gone is None:
                self._add("regkey", location, PROTECTED, R_UNINSTALL_TWIN_UNCHECKED)
            elif gone:
                self._add("regkey", location, CERTAIN, R_UNINSTALL_TWIN)
            else:
                self._add("regkey", location, PROTECTED, R_UNINSTALL_TWIN_LIVE)

    # shortcuts ------------------------------------------------------------------

    def _start_folder(self, path: str, bases: list[str]) -> tuple[str, str, bool] | None:
        """(folder, name key, name is proof on its own) of the Start menu folder
        named after the app that holds this file. A short name like 'Git' is not
        proof by itself; its shortcuts have to point into the install folder."""
        norm = guard.normalize(path)
        for base in bases:
            if not guard.inside(norm, guard.normalize(base)):
                continue
            parts = os.path.relpath(path, base).split("\\")[:-1]
            hit = self.names.classify(parts[0]) if parts else None
            if hit and hit[0] in ("exact", "weak"):
                return os.path.join(base, parts[0]), hit[1], hit[0] == "exact"
            if len(parts) > 1 and self.names.is_publisher(parts[0]):
                hit = self.names.classify(parts[1], True)
                if hit and hit[0] == "exact":
                    return os.path.join(base, parts[0], parts[1]), hit[1], True
        return None

    def shortcuts(self):
        where = guard.locations()
        recursive = [where[k] for k in ("start_user", "start_common") if where.get(k)]
        flat = [where[k] for k in ("desktop_user", "desktop_public") if where.get(k)]
        pinned = guard.known_folder("QuickLaunch")
        if pinned:
            flat.append(os.path.join(pinned, "User Pinned", "TaskBar"))
        files: dict[str, int] = {}
        for base in recursive:
            files.update(_walk_shortcuts(base, True))
        for base in flat:
            files.update(_walk_shortcuts(base, False))
        targets = _shortcut_targets(files, recursive, flat)
        folders: dict[str, tuple[str, bool, list[str]]] = {}
        for path in files:
            target = targets.get(path, "")
            norm_target = guard.normalize(target) if target else ""
            in_folder = self._start_folder(path, recursive)
            named = in_folder if in_folder and in_folder[2] else None
            root = self._root_of(norm_target) if target else None
            owner = self._owner_of(norm_target) if target else None
            if in_folder:
                folders.setdefault(in_folder[0], (in_folder[1], in_folder[2], []))[2].append(path)
            if owner and root:
                self._shortcut(path, PROTECTED, R_SHORTCUT_OTHER, {"other": owner})
            elif root:
                self._shortcut(path, CERTAIN if root.certain else LIKELY, R_SHORTCUT_TARGET)
            elif named:
                self._in_start_folder(path, target, named[1])
            elif path.lower().endswith(".lnk") and target and not os.path.exists(target):
                hit = self.names.classify(os.path.splitext(os.path.basename(path))[0])
                if hit and hit[0] == "exact" and hit[1] not in self.other_keys:
                    self._shortcut(path, LIKELY, R_SHORTCUT_DANGLING)
        for folder, (key, name_is_proof, members) in folders.items():
            marks = [(m, ("shortcut", guard.normalize(m))) for m in members]
            if not name_is_proof and any(k in self.found and self.found[k].reason == R_SHORTCUT_TARGET
                                         for _, k in marks):
                for member, k in marks:  # the shortcuts into the install folder vouch for the rest
                    if k not in self.found:
                        self._in_start_folder(member, targets.get(member, ""), key)
                name_is_proof = True
            listed = [guard.normalize(os.path.join(folder, n)) for n, is_dir in self._listing(folder)
                      if not is_dir and n.lower() != "desktop.ini"]
            if name_is_proof and listed and all(self._reported(("shortcut", n)) for n in listed) \
                    and not any(is_dir for _, is_dir in self._listing(folder)):
                self._folder(folder, LIKELY, R_START_FOLDER, caveats=False)

    def _in_start_folder(self, path: str, target: str, key: str):
        owner = self._owner_of(guard.normalize(target)) if target else None
        if owner:
            self._shortcut(path, PROTECTED, R_SHORTCUT_OTHER, {"other": owner})
        elif key in self.other_keys:
            self._shortcut(path, PROTECTED, R_SAME_NAME, {"other": self.other_keys[key]})
        else:
            # only a target that is known to be gone makes it likely; unresolved ones stay a guess
            self._shortcut(path, LIKELY if target and not os.path.exists(target) else POSSIBLE, R_SHORTCUT_FOLDER)

    def _reported(self, key: tuple[str, str]) -> bool:
        item = self.found.get(key)
        return bool(item) and item.confidence != PROTECTED

    def _shortcut(self, path: str, level: str, reason: str, values: dict | None = None):
        why = guard.protected_path(path)
        if why:
            level, reason, values = PROTECTED, why, None
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        self._add("shortcut", path, level, reason, values, size)

    def run(self) -> list[Leftover]:
        for step in (self.folders, self.software_keys, self.startup_values, self.app_paths, self.services,
                     self.uninstall_entries, self.shortcuts, self.emptied_publisher_folders):
            try:
                step()
            except Exception:
                log.exception("leftover scan step %s failed for %s", step.__name__, self.app.name)
        order = {"folder": 0, "file": 1, "shortcut": 2, "regkey": 3, "regvalue": 4, "service": 5}
        items = self._without_nested(list(self.found.values()))
        return sorted(items, key=lambda i: (_RANK[i.confidence], order[i.kind], i.location.lower()))

    @staticmethod
    def _without_nested(items: list[Leftover]) -> list[Leftover]:
        """Drop a folder or key that lies inside another one found on its own
        evidence at the same or a higher confidence: removing the parent covers it."""
        parents = {(i.kind, guard.normalize(i.location) if i.kind == "folder" else i.location.lower()): i
                   for i in items if i.kind in ("folder", "regkey")
                   and i.confidence != PROTECTED and i.reason not in (R_PUBLISHER_EMPTY, R_START_FOLDER)}

        def covered(item: Leftover) -> bool:
            if item.kind not in ("folder", "regkey") or item.confidence == PROTECTED:
                return False
            if item.kind == "folder":
                above = _ancestors(guard.normalize(item.location))
            else:
                parts = item.location.lower().split("\\")
                above = ("\\".join(parts[:n]) for n in range(len(parts) - 1, 1, -1))
            return any(p in parents and _RANK[parents[p].confidence] <= _RANK[item.confidence]
                       for p in ((item.kind, a) for a in above))

        return [i for i in items if not covered(i)]


def scan(app: InstalledApp, others: list[InstalledApp]) -> list[Leftover]:
    """Everything that looks like it belongs to app, strongest evidence first.
    others are the apps that stay installed. Reads only; deletes nothing."""
    started = time.monotonic()
    items = _Scan(app, others).run()
    log.info("Leftover scan of %s: %d items (%s) in %.1fs", app.name, len(items),
             ", ".join(f"{sum(i.confidence == c for i in items)} {c}" for c in _RANK), time.monotonic() - started)
    return items


# -------------------------------------------------------------- command line

def _violations(item: Leftover, apps: list[InstalledApp], app: InstalledApp) -> list[str]:
    """Audit rule: a certain or likely item must not be protected, must not be a
    container, and must not equal, contain or lie inside another app's folder."""
    if item.confidence not in (CERTAIN, LIKELY):
        return []
    problems = []
    if item.kind in ("folder", "file", "shortcut"):
        if item.kind != "shortcut" and guard.protected_path(item.location):
            problems.append("protected path")
        norm = guard.normalize(item.location)
        for other in apps:
            location = _clean(other.install_location)
            if other.reg_key != app.reg_key and location and not guard.protected_path(location):
                theirs = guard.normalize(location)
                if item.kind == "folder" and (guard.inside(norm, theirs) or guard.inside(theirs, norm)):
                    problems.append(f"overlaps the folder of {other.name}")
    elif item.kind == "regvalue":
        if guard.protected_value(item.location.split("|", 1)[0], item.location.split("|", 1)[-1]):
            problems.append("protected value")
    elif guard.protected_key(item.location):
        problems.append("protected key")
    return problems


def main(argv: list[str] | None = None) -> int:
    """python -m upstream.leftovers [--audit] [name ...]: print what would be
    found for the installed apps whose names contain the given text."""
    args = list(sys.argv[1:] if argv is None else argv)
    audit = "--audit" in args
    names = [a.lower() for a in args if not a.startswith("--")]
    apps = read_installed_apps()
    chosen = apps if audit and not names else [a for a in apps if any(n in a.name.lower() for n in names)]
    bad = 0
    for app in chosen:
        items = scan(app, [o for o in apps if o is not app])
        problems = [(i, p) for i in items for p in _violations(i, apps, app)]
        bad += len(problems)
        if audit and not names:
            print(f"{app.name}: {len(items)} items, {len(problems)} violations")
        else:
            print(f"\n== {app.name} {app.version} [{app.reg_key}] root={app.install_location or '-'}")
            for i in items:
                size = "" if i.size is None else f"{i.size / 1024:.0f}K"
                tail = f"  ({i.caveat})" if i.caveat else ""
                print(f"  {i.confidence:9} {i.kind:8} {i.location[:110]:110} {size:>8}  {i.reason.format_map(i.reason_values)}{tail}")
        for item, problem in problems:
            print(f"  VIOLATION {problem}: {item.confidence} {item.kind} {item.location}")
    if audit:
        print(f"audit: {len(chosen)} apps, {bad} violations")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
