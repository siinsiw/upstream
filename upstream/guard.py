"""The hard list of places Upstream may never offer for removal.

The leftover scan asks here before it shows anything, and the cleanup step will
ask again right before it deletes. A reason is an English template without
placeholders (the UI translates it); None means the path or key is not on the
list. Nothing in this module touches the disk or the registry beyond reading
folder locations."""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import re
import sys
import uuid
from ctypes import wintypes

log = logging.getLogger("upstream.guard")

R_RELATIVE = "Not a full path"
R_NETWORK = "On a network or removable drive"
R_DRIVE = "A drive's root folder"
R_WINDOWS = "Part of Windows"
R_CONTAINER = "A folder that holds many apps' files"
R_PROFILE = "A user profile folder"
R_KNOWN = "A standard Windows folder"
R_VENDOR = "Owned by Microsoft or Windows"
R_SHARED_FILES = "Shared files used by many programs"
R_TEMP = "The temporary files folder"
R_UPSTREAM = "Upstream's own files or the Python it runs on"
R_CONTAINS = "Contains folders that must never be removed"

R_KEY_UNKNOWN = "Not a registry key Upstream can remove"
R_KEY_ROOT = "A top-level registry key"
R_KEY_SHARED = "A registry area shared by Windows and every app"
R_KEY_MICROSOFT = "Registry area owned by Microsoft or Windows"
R_KEY_CLASSES = "File-type and COM registrations shared with Windows"
R_KEY_POLICIES = "Group policy settings"
R_KEY_SYSTEM = "System registry area, changed only through Windows services"
R_KEY_ELSEWHERE = "Outside the Software branch of the registry"
R_VALUE = "Only startup entries are removed one value at a time"

REASONS = tuple(v for k, v in sorted(globals().items()) if k[:2] == "R_" and isinstance(v, str))

KNOWN_FOLDERS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "SavedGames": "4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4",
    "Favorites": "1777F761-68AD-4D8A-87BD-30B759FA33DD",
    "Links": "BFB9D5E0-C6A9-404C-B2B2-AE6DB6AF4968",
    "Contacts": "56784854-C6CB-462B-8169-88E350ACB882",
    "Recent": "AE50C081-EBD2-438A-8655-8A092E34987A",
    "SendTo": "8983036C-27C0-404B-8F08-102D10DCFD74",
    "Templates": "A63293E8-664E-48DB-A079-DF759E0509F7",
    "Screenshots": "B7BEDE81-DF94-4682-A7D8-57A52620B86F",
    "CameraRoll": "AB5FB87B-7CE2-4F83-915D-550846C9537B",
    "OneDrive": "A52BBA46-E9E1-435F-B3D9-28DAA648C0F6",
    "StartMenu": "625B53C3-AB48-4EC1-BA1F-A1EF4146FC19",
    "Programs": "A77F5D77-2E2B-44C3-A6A2-ABA601054A51",
    "Startup": "B97D20BB-F46A-4C97-BA10-5E3608430854",
    "QuickLaunch": "52A4F021-7B75-48A9-9F6B-4B87A210BC8F",
    "CommonStartMenu": "A4115719-D62E-491D-AA7C-E74B8BE3B067",
    "CommonPrograms": "0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8",
    "CommonStartup": "82A5EA35-D9CD-47C5-9629-E15D2F714E6E",
    "PublicDesktop": "C4AA340D-F20F-4863-AFEF-F87EF2E6BA25",
    "Public": "DFDF76A2-C82A-4D63-906A-5644AC457385",
    "PublicDocuments": "ED4824AF-DCE4-45A8-81E2-FC7965083634",
    "PublicDownloads": "3D644C9B-1FB8-4F30-9B45-F670235F79C0",
    "PublicMusic": "3214FAB5-9757-4298-BB61-92A9DEAA44FF",
    "PublicPictures": "B6EBFB86-6907-413C-9AF7-4FC2ABF07CC5",
    "PublicVideos": "2400183A-6185-49FB-A2D8-4A2A60C5B34A",
    "Fonts": "FD228CB7-AE11-4AE3-864C-16F3910AB8FE",
    "Profile": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
    "UserProfiles": "0762D272-C50A-4BB0-A382-697DCD729B80",
    "Windows": "F38BF404-1D43-42F2-9305-67DE0B28FC23",
    "System": "1AC14E77-02E7-4E5D-B744-2EB1AE5198B7",
    "SystemX86": "D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27",
    "ProgramFiles": "905E63B6-C1BF-494E-B29C-65B732D3D21A",
    "ProgramFilesX86": "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E",
    "ProgramFilesX64": "6D809377-6AF0-444B-8957-A3773F02200E",
    "ProgramFilesCommon": "F7F1ED05-9F6D-47A2-AAAE-29D317C6F066",
    "ProgramFilesCommonX86": "DE974D24-D9C6-4D3E-BF91-F4455120B917",
    "ProgramFilesCommonX64": "6365D5A7-0F0D-45E5-87F6-0DA56B6A4F7D",
    "ProgramData": "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97",
    "RoamingAppData": "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",
    "LocalAppData": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
    "LocalAppDataLow": "A520A1A4-1780-4FF6-BD18-167343C5AF16",
}
_CONTAINER_FOLDERS = ("ProgramFiles", "ProgramFilesX86", "ProgramFilesX64", "ProgramData", "RoamingAppData",
                      "LocalAppData", "LocalAppDataLow")
_COMMON_FOLDERS = ("ProgramFilesCommon", "ProgramFilesCommonX86", "ProgramFilesCommonX64")
_PROFILE_FOLDERS = ("Profile", "UserProfiles", "Public")
# Shortcuts live here, below the vendor folders the rule further down protects.
_SHORTCUT_ZONES = ("Programs", "CommonPrograms", "Desktop", "PublicDesktop", "QuickLaunch")
_WINDOWS_FOLDERS = ("Windows", "System", "SystemX86")

# First folder below a container (Program Files, ProgramData, AppData...) that
# belongs to Windows or a vendor's shared plumbing, never to a single app.
_VENDOR_FOLDERS = {
    "microsoft", "common files", "package cache", "installer", "temp", "winsxs", "microsoft.net", "dotnet",
    "reference assemblies", "directx", "installshield installation information", "uninstall information",
    "internet explorer", "packages", "usoprivate", "usoshared", "d3dscache", "connecteddevicesplatform",
    "modifiablewindowsapps",
}
# Windows NT, Windows Defender, WindowsApps, WindowsPowerShell, ... and Microsoft's product family
# folders, which hold several products that share files. A single product's folder
# ('Microsoft VS Code', 'Microsoft OneDrive') is not in this list.
_VENDOR_PREFIXES = ("windows", "microsoft sql server", "microsoft office", "microsoft visual studio",
                    "microsoft sdks", "microsoft shared")

_SHARED_SOFTWARE = {"clients", "registeredapplications", "odbc"}
_VALUE_KEYS = {
    ("microsoft", "windows", "currentversion", "run"),
    ("microsoft", "windows", "currentversion", "runonce"),
    ("microsoft", "windows", "currentversion", "explorer", "startupapproved", "run"),
    ("microsoft", "windows", "currentversion", "explorer", "startupapproved", "run32"),
    ("microsoft", "windows", "currentversion", "explorer", "startupapproved", "startupfolder"),
}
_HIVES = {"hkcu": "hkcu", "hkey_current_user": "hkcu", "hklm": "hklm", "hkey_local_machine": "hklm",
          "hku": "hku", "hkey_users": "hku", "hkcr": "hkcr", "hkey_classes_root": "hkcr",
          "hkcc": "hkcc", "hkey_current_config": "hkcc"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


_shell32 = ctypes.WinDLL("shell32")
_ole32 = ctypes.WinDLL("ole32")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetKnownFolderPath.restype = ctypes.c_long
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_kernel32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetLongPathNameW.restype = wintypes.DWORD
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_kernel32.GetLogicalDrives.restype = wintypes.DWORD

_DRIVE_FIXED = 3
_KF_DONT_VERIFY = 0x4000


def known_folder(name: str) -> str:
    """Path of a Windows known folder, or '' when it is not available. Asks the
    shell, so a Desktop that OneDrive moved is found where it really is."""
    guid = uuid.UUID(KNOWN_FOLDERS[name])
    rfid = _GUID(guid.time_low, guid.time_mid, guid.time_hi_version, (ctypes.c_ubyte * 8)(*guid.bytes[8:]))
    out = ctypes.c_void_p()
    try:
        if _shell32.SHGetKnownFolderPath(ctypes.byref(rfid), _KF_DONT_VERIFY, None, ctypes.byref(out)) < 0:
            return ""
        return ctypes.wstring_at(out.value) if out.value else ""
    finally:
        if out.value:
            _ole32.CoTaskMemFree(out)


def _long_name(path: str) -> str:
    """Expand 8.3 names (PROGRA~1) so they cannot slip past a name comparison.
    Works on the longest part of the path that exists."""
    if "~" not in path:
        return path
    head, tail = path, []
    while head and len(head) > 3:
        buf = ctypes.create_unicode_buffer(32768)
        if _kernel32.GetLongPathNameW(head, buf, len(buf)):
            return os.path.join(buf.value, *reversed(tail)) if tail else buf.value
        head, name = os.path.split(head)
        tail.append(name)
    return path


def normalize(path: str) -> str:
    """Absolute, lower-case, long-name form without a trailing separator
    (a drive root keeps its backslash). The form every comparison uses."""
    p = os.path.expandvars(str(path).strip().strip('"'))
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    p = os.path.normcase(_long_name(os.path.abspath(p)))
    return p.rstrip("\\") if len(p) > 3 else p


def inside(path: str, parent: str) -> bool:
    """path equals parent or lies under it; both already normalized."""
    return path == parent or path.startswith(parent.rstrip("\\") + "\\")


def _is_full(path: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", path))


@functools.lru_cache(maxsize=None)
def _drive_fixed(root: str) -> bool:
    return _kernel32.GetDriveTypeW(root) == _DRIVE_FIXED


def _fixed_drives() -> list[str]:
    """Roots of the local fixed drives. Apps and games are often installed to D:\\Program Files
    and the like, which need the same protection as the ones on the system drive."""
    mask = _kernel32.GetLogicalDrives()
    roots = [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]
    return [root for root in roots if _drive_fixed(root)]


@functools.lru_cache(maxsize=1)
def locations() -> dict[str, str]:
    """Folders the scan looks in, from the shell with an environment fallback."""
    env = os.environ.get
    profile = known_folder("Profile") or env("USERPROFILE", "")
    local = known_folder("LocalAppData") or env("LOCALAPPDATA", "")
    found = {
        "profile": profile,
        "roaming": known_folder("RoamingAppData") or env("APPDATA", ""),
        "local": local,
        "locallow": known_folder("LocalAppDataLow") or (os.path.join(profile, "AppData", "LocalLow") if profile else ""),
        "local_programs": os.path.join(local, "Programs") if local else "",
        "programdata": known_folder("ProgramData") or env("PROGRAMDATA", ""),
        "programfiles": known_folder("ProgramFiles") or env("ProgramFiles", ""),
        "programfiles_x86": known_folder("ProgramFilesX86") or env("ProgramFiles(x86)", ""),
        "start_user": known_folder("Programs"),
        "start_common": known_folder("CommonPrograms"),
        "desktop_user": known_folder("Desktop"),
        "desktop_public": known_folder("PublicDesktop"),
        "windows": known_folder("Windows") or env("SystemRoot", ""),
        "temp": env("TEMP", "") or env("TMP", ""),
    }
    return {k: v for k, v in found.items() if v}


def _runtime_paths() -> list[str]:
    paths = [sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(sys.executable or "")]
    paths.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the folder Upstream runs from
    try:
        from .config import CONFIG_DIR, DATA_DIR
        paths += [str(CONFIG_DIR), str(DATA_DIR)]
    except ImportError:
        pass
    return [p for p in paths if p]


class _State:
    def __init__(self):
        self.exact: dict[str, str] = {}
        self.containers: list[str] = []
        self.zones = [normalize(p) for p in map(known_folder, _SHORTCUT_ZONES) if p]
        self.runtime = [normalize(p) for p in _runtime_paths()]
        self.windows = [normalize(locations().get("windows", "C:\\Windows"))]
        self.profile_roots: list[str] = []

        def add(path: str, reason: str):
            if path:
                self.exact.setdefault(normalize(path), reason)

        for name in _CONTAINER_FOLDERS:
            add(known_folder(name), R_CONTAINER)
        for name in _COMMON_FOLDERS:
            add(known_folder(name), R_SHARED_FILES)
        where = locations()
        add(where.get("local_programs", ""), R_CONTAINER)
        if where.get("local"):
            add(os.path.join(where["local"], "VirtualStore"), R_CONTAINER)
        if where.get("profile"):
            add(os.path.join(where["profile"], "AppData"), R_CONTAINER)
        for name in _PROFILE_FOLDERS:
            add(known_folder(name), R_PROFILE)
        add(os.environ.get("USERPROFILE", ""), R_PROFILE)
        profiles = known_folder("UserProfiles") or os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users")
        try:
            for entry in os.scandir(profiles):
                add(entry.path, R_PROFILE)
        except OSError:
            pass
        self.profile_roots.append(normalize(profiles))
        for name in _WINDOWS_FOLDERS:
            add(known_folder(name), R_WINDOWS)
        add(self.windows[0], R_WINDOWS)
        for drive in _fixed_drives():  # the system drive's folders have their own entries above
            for name in ("Program Files", "Program Files (x86)", "ProgramData"):
                add(drive + name, R_CONTAINER)
            add(drive + "Users", R_PROFILE)
            self.profile_roots.append(normalize(drive + "Users"))
            self.windows += [normalize(drive + name) for name in ("Windows", "WindowsApps")]
        for name in KNOWN_FOLDERS:
            if not {name} & {*_CONTAINER_FOLDERS, *_COMMON_FOLDERS, *_PROFILE_FOLDERS, *_WINDOWS_FOLDERS}:
                add(known_folder(name), R_KNOWN)
        for var in ("TEMP", "TMP"):
            add(os.environ.get(var, ""), R_TEMP)
        self.containers = sorted({p for p, why in self.exact.items() if why == R_CONTAINER},
                                 key=len, reverse=True)


@functools.lru_cache(maxsize=1)
def _state() -> _State:
    return _State()


def containers() -> list[str]:
    """Normalized folders that hold many apps (Program Files, AppData...), on every fixed drive."""
    return list(_state().containers)


def protected_path(path: str) -> str | None:
    """Reason this folder or file may never be removed, or None."""
    if not path or not isinstance(path, str):
        return R_RELATIVE
    text = os.path.expandvars(path.strip().strip('"'))
    if not _is_full(text):
        return R_RELATIVE
    p = normalize(text)
    if p.startswith("\\\\"):  # UNC shares, \\?\UNC\..., \\.\ devices
        return R_NETWORK
    if re.fullmatch(r"[a-z]:\\", p):
        return R_DRIVE
    if not _drive_fixed(p[:3]):
        return R_NETWORK
    st = _state()
    if any(inside(p, windows) for windows in st.windows):
        return R_WINDOWS
    reason = st.exact.get(p)
    if reason:
        return reason
    if os.path.dirname(p) in st.profile_roots:
        return R_PROFILE
    for known in st.exact:
        if known.startswith(p + "\\"):
            return R_CONTAINS
    for runtime in st.runtime:
        if inside(p, runtime) or inside(runtime, p):
            return R_UPSTREAM
    if any(p.startswith(zone + "\\") for zone in st.zones):
        return None
    for container in st.containers:
        if p.startswith(container + "\\"):
            first = p[len(container) + 1:].split("\\", 1)[0]
            if first in _VENDOR_FOLDERS or first.startswith(_VENDOR_PREFIXES):
                return {"temp": R_TEMP, "common files": R_SHARED_FILES}.get(first, R_VENDOR)
            break
    return None


def split_key(key: str) -> tuple[str, list[str]] | None:
    """('hklm', ['software', 'vendor']) for 'HKLM\\SOFTWARE\\Vendor'. A value
    suffix ('|Name') is ignored. None when the text is not a registry path."""
    text = key.split("|", 1)[0].strip().strip("\\")
    head, _, rest = text.partition("\\")
    hive = _HIVES.get(head.lower())
    if not hive:
        return None
    return hive, [part for part in rest.lower().split("\\") if part]


def _software_branch(parts: list[str]) -> list[str] | None:
    """Parts below SOFTWARE with a Wow6432Node level folded away, or None."""
    if not parts or parts[0] != "software":
        return None
    rest = parts[1:]
    return rest[1:] if rest[:1] == ["wow6432node"] else rest


def protected_key(key: str) -> str | None:
    """Reason this registry key may never be removed as a whole, or None.
    The only keys under Microsoft's branch that pass are one entry below
    Uninstall or App Paths; the only ones under SYSTEM are single services."""
    parsed = split_key(key) if isinstance(key, str) else None
    if not parsed:
        return R_KEY_UNKNOWN
    hive, parts = parsed
    if not parts:
        return R_KEY_ROOT
    if hive not in ("hkcu", "hklm"):
        return R_KEY_UNKNOWN
    if hive == "hklm" and parts[0] == "system":
        single_service = len(parts) == 4 and parts[1].startswith(("currentcontrolset", "controlset")) \
            and parts[2] == "services"
        return None if single_service else R_KEY_SYSTEM
    rest = _software_branch(parts)
    if rest is None:
        return R_KEY_ELSEWHERE
    if not rest:  # the Software root or the Wow6432Node root
        return R_KEY_SHARED
    first = rest[0]
    if first == "classes":
        return R_KEY_CLASSES
    if first == "policies":
        return R_KEY_POLICIES
    if first == "microsoft":
        slot = len(rest) == 5 and rest[:3] == ["microsoft", "windows", "currentversion"] \
            and rest[3] in ("uninstall", "app paths")
        return None if slot else R_KEY_MICROSOFT
    if first in _SHARED_SOFTWARE:
        return R_KEY_SHARED
    return None


def protected_value(key: str, name: str) -> str | None:
    """Reason a single registry value may not be removed: only Run, RunOnce and
    their StartupApproved twins qualify."""
    parsed = split_key(key) if isinstance(key, str) else None
    if not parsed or not name or parsed[0] not in ("hkcu", "hklm"):
        return R_VALUE
    rest = _software_branch(parsed[1])
    return None if rest is not None and tuple(rest) in _VALUE_KEYS else R_VALUE
