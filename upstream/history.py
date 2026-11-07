"""Install, update and uninstall history. Owner: the history feature."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HistoryEntry:
    id: str
    ts: float                       # time.time()
    kind: str                       # install | update | rollback | uninstall | cleanup | restore
    name: str
    version_from: str = ""
    version_to: str = ""
    ok: bool = True
    template: str = ""              # English message template, rendered with `values` when shown
    values: dict = field(default_factory=dict)
    source: str = ""                # github | site | winget | empty


def record(kind: str, name: str, version_from: str = "", version_to: str = "", ok: bool = True,
           template: str = "", values: dict | None = None, source: str = "") -> None:
    """Append one entry. Never raises: history must not break an install."""
    return None


def load(limit: int = 500) -> list[HistoryEntry]:
    """Newest first."""
    return []


def clear() -> None:
    return None
