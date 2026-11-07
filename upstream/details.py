"""Facts about one installed app, and its Repair / Modify actions. Owner: the app-details feature."""
from __future__ import annotations

from dataclasses import dataclass, field

from .inventory import InstalledApp


@dataclass
class AppDetails:
    name: str
    version: str
    publisher: str
    scope: str                       # machine | user
    arch: str
    install_location: str = ""
    install_date: str = ""           # YYYY-MM-DD, empty when unknown
    size_bytes: int = 0              # 0 = unknown
    uninstall_string: str = ""
    urls: list = field(default_factory=list)
    can_repair: bool = False
    can_modify: bool = False


def gather(app: InstalledApp) -> AppDetails:
    """Fast: registry values and file-system facts only (no PowerShell)."""
    return AppDetails(app.name, app.version, app.publisher, app.scope, app.arch)


def signer_of(app: InstalledApp) -> tuple[str, str]:
    """(Authenticode status, signer CN) of the app main executable. Slow (PowerShell): call from a thread."""
    return "Unknown", ""


def repair(app: InstalledApp, progress, cancel=None) -> tuple[bool, str, dict]:
    """Run the app repair. Returns (ok, English template, values)."""
    return False, "Repair is not available for this app", {}


def modify(app: InstalledApp) -> tuple[bool, str, dict]:
    """Open the vendor Modify/Change action. Returns (started, English template, values)."""
    return False, "Modify is not available for this app", {}
