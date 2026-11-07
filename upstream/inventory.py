"""Installed desktop apps, read from the same Uninstall registry keys that
Settings > Apps uses (machine 64-bit, machine 32-bit, current user)."""
from __future__ import annotations

import ctypes
import os
import re
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field

UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
INSTALLSTATE_UNKNOWN = -1  # msi.h: Windows Installer has no record of the product

_VIEWS = [
    (winreg.HKEY_LOCAL_MACHINE, "HKLM64", winreg.KEY_WOW64_64KEY, "machine", "x64"),
    (winreg.HKEY_LOCAL_MACHINE, "HKLM32", winreg.KEY_WOW64_32KEY, "machine", "x86"),
    (winreg.HKEY_CURRENT_USER, "HKCU", 0, "user", ""),
]
_SKIP_RELEASE_TYPES = {"update", "hotfix", "security update", "service pack"}
_URL_VALUES = ("URLUpdateInfo", "URLInfoAbout", "HelpLink", "Contact", "Readme")
_IDENT_NOISE = re.compile(
    r"\bv?\d+(?:[.\-_+]\d+)+\b|\bversion\b|\((?:x64|x86|64-bit|32-bit)\)|\b(?:x64|x86|amd64|64-bit|32-bit)\b",
    re.I,
)
GUID = re.compile(r"\{[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}\}", re.I)
_HIVES = {label: (hive, view) for hive, label, view, _scope, _arch in _VIEWS}
_msi = None
# What each framework leaves in its Uninstall key (see _install_kind).
_BURN_VALUES = ("BundleProviderKey", "BundleCachePath", "BundleVersion", "BundleUpgradeCode")
_INNO_UNINSTALLER = re.compile(r"unins\d{3}\.exe", re.I)
_SQUIRREL_UNINSTALL = re.compile(r'update\.exe"?\s+--uninstall', re.I)


def make_ident(name: str) -> str:
    """Name without version numbers or arch tags, so settings survive updates
    (MSI product codes and display names often change between versions)."""
    s = _IDENT_NOISE.sub(" ", name.lower())
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


@dataclass
class InstalledApp:
    reg_key: str          # unique per entry, e.g. HKLM64\{GUID}
    name: str
    version: str
    publisher: str
    scope: str            # machine | user
    arch: str             # x64 | x86
    install_location: str = ""
    icon_path: str = ""
    urls: list[str] = field(default_factory=list)
    is_msi: bool = False
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    no_remove: bool = False
    product_code: str | None = None   # the {GUID} subkey of a Windows Installer entry
    system_component: bool = False
    install_kind: str = ""        # installer framework of this copy, as installer.detect() names it; '' when the
                                  # Uninstall key can't tell (inplace.installed_kind then reads the uninstaller file)

    @property
    def ident(self) -> str:
        return make_ident(self.name)


def _value(key, name: str):
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value


def _text(key, name: str) -> str:
    value = _value(key, name)
    return str(value).strip() if value not in (None, "") else ""


def _flag(key, name: str) -> bool:
    return _value(key, name) in (1, "1")


def _icon_path(raw: str) -> str:
    if not raw:
        return ""
    s = os.path.expandvars(raw.strip())
    if s.startswith('"'):
        s = s[1:].split('"', 1)[0]
    else:
        s = re.sub(r",\s*-?\d+\s*$", "", s)
    return s if os.path.isfile(s) else ""


def folder_path(raw: str) -> str:
    """A folder written in the registry as a full path: quotes removed, %VARS% expanded.
    '' when it is not an absolute path, since a relative one would mean the current directory."""
    text = os.path.expandvars((raw or "").strip().strip('"').strip())
    return text if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", text) else ""


def _guess_arch(location: str, icon: str) -> str:
    probe = f"{location} {icon}".lower()
    return "x86" if "program files (x86)" in probe else "x64"


def _install_kind(key, subkey: str, is_msi: bool, commands: str) -> str:
    """Installer framework of this copy from what its Uninstall key says, in the names installer.detect()
    returns. NSIS leaves nothing here; only its uninstaller file gives it away (inplace.installed_kind)."""
    name, low = subkey.lower(), commands.lower()
    if any(_value(key, v) is not None for v in _BURN_VALUES):
        return "burn"
    if is_msi:
        return "msi"
    if name.endswith("_is1") or _value(key, "Inno Setup: Setup Version") is not None or _INNO_UNINSTALLER.search(low):
        return "inno"
    if name.startswith("installshield_") or "installshield installation information" in low:
        return "installshield"
    if _SQUIRREL_UNINSTALL.search(low):
        return "squirrel"
    if GUID.fullmatch(subkey) and "msiexec" in low:
        return "msi"
    return ""


def _read_entry(key, reg_key: str, scope: str, arch: str) -> InstalledApp | None:
    name = _text(key, "DisplayName")
    if not name:
        return None
    if _value(key, "SystemComponent") == 1:
        return None
    if _text(key, "ParentKeyName") or _text(key, "ParentDisplayName"):
        return None
    if _text(key, "ReleaseType").lower() in _SKIP_RELEASE_TYPES:
        return None
    if re.match(r"KB\d{6,}", reg_key.rsplit("\\", 1)[-1]):
        return None
    # Website shortcuts installed by Chrome/Edge/Brave ("Install as app") update with the site.
    if "--uninstall-app-id" in _text(key, "UninstallString"):
        return None

    location = folder_path(_text(key, "InstallLocation"))
    icon = _icon_path(_text(key, "DisplayIcon"))
    urls = [u for u in (_text(key, v) for v in _URL_VALUES) if u]
    is_msi = _value(key, "WindowsInstaller") == 1
    subkey = reg_key.rsplit("\\", 1)[-1]
    uninstall, quiet = _text(key, "UninstallString"), _text(key, "QuietUninstallString")
    return InstalledApp(
        reg_key=reg_key,
        name=name,
        version=_text(key, "DisplayVersion"),
        publisher=_text(key, "Publisher"),
        scope=scope,
        arch=arch or _guess_arch(location, icon),
        install_location=location,
        icon_path=icon,
        urls=urls,
        is_msi=is_msi,
        uninstall_string=uninstall,
        quiet_uninstall_string=quiet,
        no_remove=_flag(key, "NoRemove"),
        product_code=subkey if is_msi and GUID.fullmatch(subkey) else None,
        system_component=_flag(key, "SystemComponent"),
        install_kind=_install_kind(key, subkey, is_msi, f"{uninstall} {quiet}"),
    )


def read_installed_apps(skipped_names: set[str] | None = None) -> list[InstalledApp]:
    """Visible apps. Names of entries filtered out (system parts, patches, web
    shortcuts) go into skipped_names so winget rows for them are not re-added."""
    apps: list[InstalledApp] = []
    seen: set[tuple[str, str]] = set()
    for hive, label, view, scope, arch in _VIEWS:
        try:
            root = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub) as key:
                        app = _read_entry(key, f"{label}\\{sub}", scope, arch)
                        if not app and skipped_names is not None and _text(key, "DisplayName"):
                            skipped_names.add(_text(key, "DisplayName").lower())
                except OSError:
                    continue
                if not app:
                    continue
                dedupe = (app.name.lower(), app.version)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                apps.append(app)
    apps.sort(key=lambda a: a.name.lower())
    return apps


def find_current(app: InstalledApp) -> InstalledApp | None:
    """The same app after an install: same registry key if it survived,
    otherwise the entry with the same ident (MSI upgrades change the key)."""
    current = read_installed_apps()
    for other in current:
        if other.reg_key == app.reg_key:
            return other
    matches = [o for o in current if o.ident == app.ident and o.scope == app.scope]
    return matches[0] if len(matches) == 1 else None


def _msi_state(product_code: str) -> int | None:
    """MsiQueryProductStateW, or None when msi.dll can't be used."""
    global _msi
    try:
        if _msi is None:
            _msi = ctypes.WinDLL("msi")
            _msi.MsiQueryProductStateW.argtypes = [wintypes.LPCWSTR]
            _msi.MsiQueryProductStateW.restype = ctypes.c_int
        return _msi.MsiQueryProductStateW(product_code)
    except (OSError, AttributeError):
        return None


def _key_exists(reg_key: str) -> bool:
    label, _, sub = reg_key.partition("\\")
    if label not in _HIVES or not sub:
        return False
    hive, view = _HIVES[label]
    try:
        winreg.CloseKey(winreg.OpenKey(hive, f"{UNINSTALL}\\{sub}", 0, winreg.KEY_READ | view))
    except FileNotFoundError:
        return False
    except OSError:
        return True  # present but unreadable
    return True


def still_listed(app: InstalledApp) -> bool:
    """Whether Windows still lists the app: its Uninstall key exists in the same
    hive and registry view. A Windows Installer product also counts as gone once
    Installer itself no longer knows it, even if a stale key was left behind.
    Store/MSIX rows have no key; winget answers for those (see uninstaller.is_gone)."""
    if app.reg_key.startswith("WINGET\\"):
        return False
    listed = _key_exists(app.reg_key)
    if app.product_code:
        state = _msi_state(app.product_code)
        if state == INSTALLSTATE_UNKNOWN:
            return False
        if state is not None and state > 0:
            return True  # Installer still has it, even while its key is mid-removal
    return listed
