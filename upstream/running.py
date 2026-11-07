"""Close the running app before its files are replaced. Owner: the close-running-apps feature."""
from __future__ import annotations

from dataclasses import dataclass

from .inventory import InstalledApp


@dataclass(frozen=True)
class RunningProcess:
    pid: int
    name: str
    path: str


def find_running(app: InstalledApp) -> list[RunningProcess]:
    return []


def close_for_update(ctx, app: InstalledApp, progress) -> None:
    """Ask / close the app's processes if any are running. `ctx` is a context.UpdateContext.
    Raises errors.Cancelled when the user declines. `progress(template, percent, **values)`."""
    return None
