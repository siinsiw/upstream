"""Single instance, crash reports, log and download housekeeping. Owner: the stability feature."""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def acquire_single_instance(on_activate: Callable[[], None]) -> bool:
    """True when this process is the only instance. False when another one is running: it has been told to
    show its window (its on_activate runs) and this process should exit."""
    return True


def install_crash_handlers() -> None:
    """Uncaught exceptions (main and worker threads) and hard crashes are written to a report file."""
    return None


def pending_crash_report() -> Path | None:
    """A report from an earlier run the user has not been told about yet, else None."""
    return None


def mark_reported(path: Path) -> None:
    return None


def housekeeping() -> None:
    """Rotate the log, delete stale downloads and old crash reports. Never raises."""
    return None
