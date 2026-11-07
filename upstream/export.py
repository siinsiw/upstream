"""Export the installed-app list, to set up another PC. Owner: the export feature."""
from __future__ import annotations

from pathlib import Path

FORMATS = ("json", "csv", "winget")   # winget = a file that `winget import` accepts


def default_name(fmt: str) -> str:
    """File name suggestion, e.g. upstream-apps-2026-11-08.json."""
    return f"upstream-apps.{fmt}"


def export_apps(statuses, path: Path, fmt: str) -> int:
    """Write the list and return how many apps were written. Raises OSError / ValueError on failure."""
    return 0
