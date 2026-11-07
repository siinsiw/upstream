"""Remove leftovers after an uninstall, with a backup and an undo. Owner: the leftover-cleanup feature."""
from __future__ import annotations

from dataclasses import dataclass, field

from .leftovers import Leftover


@dataclass
class CleanupResult:
    removed: int = 0
    failed: list = field(default_factory=list)      # [(Leftover, English template, values)]
    backup_id: str | None = None                    # None when nothing needed a backup
    restore_point: bool | None = None               # True created, False failed, None not attempted


@dataclass(frozen=True)
class Backup:
    id: str
    app_name: str
    ts: float
    items: int
    size_bytes: int


@dataclass
class RestoreResult:
    restored: int = 0
    failed: list = field(default_factory=list)      # [(description, English template, values)]


def run(items: list[Leftover], app_name: str, progress, cancel=None) -> CleanupResult:
    """Back up then remove the ticked leftovers. progress(template, percent, **values)."""
    return CleanupResult()


def backups() -> list[Backup]:
    return []


def restore(backup_id: str, progress=None) -> RestoreResult:
    return RestoreResult()


def delete_backup(backup_id: str) -> None:
    return None


def purge_old(days: int) -> int:
    """Delete backups older than `days`; returns how many were removed."""
    return 0
