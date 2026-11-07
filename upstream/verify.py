"""Check, after an installer returns, what really happened on the PC. Owner: the install-verification feature."""
from __future__ import annotations

from dataclasses import dataclass, field

from .exitcodes import Outcome
from .inventory import InstalledApp, find_current
from .versioning import is_newer


@dataclass
class Snapshot:
    """The PC's state just before the installer runs."""
    entries: list[InstalledApp] = field(default_factory=list)
    pending_reboot: bool = False


@dataclass(frozen=True)
class Verdict:
    kind: str                      # installed | restart | side_by_side | unchanged | unknown
    version: str                   # the version Windows reports now (or the expected one when unknown)
    template: str                  # English message shown on the row
    values: dict = field(default_factory=dict)
    ok: bool = True


def snapshot(app: InstalledApp) -> Snapshot:
    return Snapshot()


def check(app: InstalledApp, before: Snapshot, outcome: Outcome, expected_version: str) -> Verdict:
    current = find_current(app)
    new_version = current.version if current else expected_version
    if current and not is_newer(new_version, app.version):
        return Verdict("unchanged", new_version, "Installer finished, but Windows still reports {version}",
                       {"version": new_version})
    return Verdict("installed", new_version, "Installed {version}", {"version": new_version})
