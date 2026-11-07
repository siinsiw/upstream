"""What an installer's exit code means. Owner: the exit-code feature."""
from __future__ import annotations

from dataclasses import dataclass, field


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


def classify(code: int, kind: str) -> Outcome:
    """`kind` is the installer framework from installer.detect() (msi, inno, nullsoft, ...)."""
    if code == 0:
        return Outcome("ok")
    if code in (3010, 1641):
        return Outcome("restart")
    return Outcome("failed", "Installer exited with code {code}", {"code": code})


def classify_winget(code: int, last_line: str) -> Outcome:
    """winget's exit code (unsigned 32-bit) plus the last line it printed."""
    if code == 0:
        return Outcome("ok")
    if last_line:
        return Outcome("failed", "{error}", {"error": last_line})
    return Outcome("failed", "winget exited with {code}", {"code": f"0x{code:08X}"})
