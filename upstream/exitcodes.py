"""What an installer's exit code means.

Sources: Windows Installer error codes (learn.microsoft.com/windows/win32/msi/error-codes), Windows system error
codes (learn.microsoft.com/windows/win32/debug/system-error-codes), Inno Setup exit codes (jrsoftware.org/ishelp,
"Setup Exit Codes") and winget's return codes (microsoft/winget-cli doc/windows/package-manager/winget/returnCodes.md).
Codes that are not listed are reported as a plain failure with the code."""
from __future__ import annotations

from dataclasses import dataclass, field

from .i18n import N_


@dataclass(frozen=True)
class Outcome:
    kind: str                      # ok | restart | busy | cancelled | already_installed | newer_present
    #                                | needs_admin | disk_full | failed
    template: str = ""             # English message for anything that is not ok / restart
    values: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind in ("ok", "restart")

    @property
    def restart(self) -> bool:
        return self.kind == "restart"

    @property
    def cancelled(self) -> bool:
        return self.kind == "cancelled"

    @property
    def busy(self) -> bool:
        return self.kind == "busy"

    @property
    def present(self) -> bool:
        """The installer says this app, or a newer one, is already there; the registry decides whether that is so."""
        return self.kind in ("already_installed", "newer_present")


_CANCELLED = N_("The installation was cancelled")
_BUSY = N_("Another installation is running. Try again when it has finished.")
_ADMIN = N_("The installer needs administrator permission")
_DISK = N_("There isn't enough free disk space to install")

# Windows Installer / system codes. msiexec returns them directly, Burn bundles as HRESULT_FROM_WIN32(code).
_WINDOWS = {
    5: ("needs_admin", N_("The installer was denied access. It may need administrator permission (code {code})")),
    39: ("disk_full", _DISK),                       # ERROR_HANDLE_DISK_FULL
    112: ("disk_full", _DISK),                      # ERROR_DISK_FULL
    740: ("needs_admin", _ADMIN),                   # ERROR_ELEVATION_REQUIRED
    1223: ("cancelled", _CANCELLED),                # ERROR_CANCELLED
    1601: ("failed", N_("The Windows Installer service couldn't be reached (code {code})")),
    1602: ("cancelled", _CANCELLED),                # ERROR_INSTALL_USEREXIT
    1603: ("failed", N_("Windows Installer hit a fatal error during installation (code {code})")),
    1604: ("failed", N_("The installation was suspended before it finished (code {code})")),
    1618: ("busy", _BUSY),                          # ERROR_INSTALL_ALREADY_RUNNING
    1619: ("failed", N_("The installer package couldn't be opened (code {code})")),
    1620: ("failed", N_("The installer package is invalid or damaged (code {code})")),
    1621: ("failed", N_("Windows Installer couldn't show its window (code {code})")),
    1622: ("failed", N_("Windows Installer couldn't write its log file (code {code})")),
    1623: ("failed", N_("The installer's language isn't supported by this PC (code {code})")),
    1624: ("failed", N_("Windows Installer couldn't apply a customization (code {code})")),
    1625: ("failed", N_("System policy forbids this installation (code {code})")),
    1633: ("failed", N_("This installer isn't supported on this PC (code {code})")),
    1638: ("already_installed",                     # ERROR_PRODUCT_VERSION
           N_("Another version of this app is already installed, so this installer can't continue")),
    1639: ("failed", N_("Windows Installer was given an invalid command line (code {code})")),
    1640: ("failed", N_("Windows doesn't allow installing from a remote session (code {code})")),
    1645: ("failed", N_("Windows doesn't allow installing from a remote session (code {code})")),
}
_RESTART = (3010, 1641)   # ERROR_SUCCESS_REBOOT_REQUIRED, ERROR_SUCCESS_REBOOT_INITIATED: both mean success

# Inno Setup, which only ever returns 0-8 (and 0 even when a restart is due, unless /RESTARTEXITCODE is given).
_INNO = {
    1: ("failed", N_("Setup couldn't start (code {code})")),
    2: ("cancelled", _CANCELLED),                   # cancelled before the installation began
    3: ("failed", N_("Setup hit a fatal error while preparing to install (code {code})")),
    4: ("failed", N_("Setup hit a fatal error while installing (code {code})")),
    5: ("cancelled", _CANCELLED),                   # cancelled or aborted during the installation
    7: ("failed", N_("Setup decided it can't install on this PC (code {code})")),
    8: ("failed", N_("Setup can't continue until Windows is restarted")),
}
# NSIS: 1 means the user cancelled, 2 that the installer script aborted (same reading as uninstaller.py).
_NSIS = {
    1: ("cancelled", _CANCELLED),
    2: ("failed", N_("The installer stopped itself before it finished (code {code})")),
}

_WINGET_REBOOT = (0x8A150109, 0x8A15010B)   # INSTALL_REBOOT_REQUIRED_TO_FINISH, INSTALL_REBOOT_INITIATED
_WINGET_FILE_IN_USE = N_("The app is still running. Close it and try again")
_WINGET_INTEGRITY = N_("The download failed winget's integrity check; nothing was installed")
_WINGET = {
    0x8A150005: ("cancelled", _CANCELLED),          # CTRL_SIGNAL_RECEIVED
    0x8A150008: ("failed", N_("winget couldn't download the installer")),
    0x8A150010: ("failed", N_("winget has no installer that fits this PC")),        # NO_APPLICABLE_INSTALLER
    0x8A150011: ("failed", _WINGET_INTEGRITY),                                      # INSTALLER_HASH_MISMATCH
    0x8A150014: ("failed", N_("winget couldn't find this package")),                # NO_APPLICATIONS_FOUND
    0x8A150017: ("failed", N_("winget couldn't find this package")),                # NO_MANIFEST_FOUND
    0x8A150019: ("needs_admin", _ADMIN),            # COMMAND_REQUIRES_ADMIN
    0x8A15002B: ("already_installed",               # UPDATE_NOT_APPLICABLE
                 N_("winget has no update it can apply to this app")),
    0x8A15002D: ("failed", _WINGET_INTEGRITY),                                      # INSTALLER_SECURITY_CHECK_FAILED
    0x8A15002E: ("failed", _WINGET_INTEGRITY),                                      # DOWNLOAD_SIZE_MISMATCH
    0x8A150101: ("failed", _WINGET_FILE_IN_USE),    # INSTALL_PACKAGE_IN_USE
    0x8A150102: ("busy", _BUSY),                    # INSTALL_INSTALL_IN_PROGRESS
    0x8A150103: ("failed", _WINGET_FILE_IN_USE),    # INSTALL_FILE_IN_USE
    0x8A150104: ("failed", N_("A package this app depends on is missing")),
    0x8A150105: ("disk_full", _DISK),               # INSTALL_DISK_FULL
    0x8A150107: ("failed", N_("The installer needs an internet connection")),       # INSTALL_NO_NETWORK
    0x8A15010A: ("failed", N_("Windows must restart before winget can install this. Restart and try again")),
    0x8A15010C: ("cancelled", _CANCELLED),          # INSTALL_CANCELLED_BY_USER
    0x8A15010D: ("already_installed",               # INSTALL_ALREADY_INSTALLED
                 N_("winget says another version of this app is already installed")),
    0x8A15010E: ("newer_present",                   # INSTALL_DOWNGRADE
                 N_("winget says a newer version of this app is already installed")),
    0x8A15010F: ("failed", N_("Organization policy blocks this installation")),     # INSTALL_BLOCKED_BY_POLICY
    0x8A150110: ("failed", N_("A package this app depends on is missing")),         # INSTALL_DEPENDENCIES
    0x8A150111: ("failed", _WINGET_FILE_IN_USE),    # INSTALL_PACKAGE_IN_USE_BY_APPLICATION
    0x8A150113: ("failed", N_("This package isn't supported on this PC")),          # INSTALL_SYSTEM_NOT_SUPPORTED
    0x8A150114: ("failed", N_("The installer can't upgrade an installed copy")),    # INSTALL_UPGRADE_NOT_SUPPORTED
}


def _shown(code: int):
    """Small codes in decimal as installers document them, 32-bit ones (HRESULTs) in hex."""
    return code if code <= 0xFFFF else f"0x{code:08X}"


def _outcome(entry: tuple[str, str], code: int) -> Outcome:
    kind, template = entry
    return Outcome(kind, template, {"code": _shown(code)} if "{code}" in template else {})


def classify(code: int, kind: str) -> Outcome:
    """`kind` is the installer framework from installer.detect() (msi, inno, nullsoft, ...).
    Signed and unsigned 32-bit values mean the same code."""
    raw = code & 0xFFFFFFFF
    if raw == 0:
        return Outcome("ok")
    own = {"inno": _INNO, "nullsoft": _NSIS}.get(kind)
    if own is not None and raw in own:
        return _outcome(own[raw], raw)
    # HRESULT_FROM_WIN32(n) is 0x8007nnnn: Burn bundles and a few others return Windows errors that way.
    number = raw & 0xFFFF if own is None and raw >> 16 == 0x8007 else raw
    if number in _RESTART:
        return Outcome("restart")
    if number in _WINDOWS:
        return _outcome(_WINDOWS[number], raw)
    return Outcome("failed", "Installer exited with code {code}", {"code": _shown(raw)})


def classify_winget(code: int, last_line: str) -> Outcome:
    """winget's exit code (unsigned 32-bit) plus the last line it printed."""
    raw = code & 0xFFFFFFFF
    if raw == 0:
        return Outcome("ok")
    if raw in _WINGET_REBOOT:
        return Outcome("restart")
    if raw in _WINGET:
        return _outcome(_WINGET[raw], raw)
    if last_line:
        return Outcome("failed", "{error}", {"error": last_line})
    return Outcome("failed", "winget exited with {code}", {"code": f"0x{raw:08X}"})
