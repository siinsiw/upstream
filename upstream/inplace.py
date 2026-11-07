"""Make an update replace the installed copy instead of sitting next to it. Owner: the in-place-install feature."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .inventory import InstalledApp


@dataclass
class InstallPlan:
    extra_args: str = ""           # appended to the silent switches (e.g. /DIR="C:\Apps\Audacity" for Inno Setup)
    warnings: list = field(default_factory=list)    # [(English template, values)] logged with the install
    refuse: tuple | None = None    # (English template, values): installing would create a second copy


def installed_kind(app: InstalledApp) -> str:
    """Installer framework of the copy already on the PC (msi, inno, nullsoft, burn, ...), '' if unknown."""
    return app.install_kind


def plan(app: InstalledApp, new_kind: str, installer_path: Path) -> InstallPlan:
    return InstallPlan()
