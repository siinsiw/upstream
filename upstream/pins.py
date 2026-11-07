"""Hold an app on a version (or a major line) and roll back to an older one. Owner: the pins feature."""
from __future__ import annotations


def pin_for(settings, ident: str) -> dict | None:
    """{"mode": "hold" or "major", "version": "1.2.3"} or None."""
    return None


def set_pin(settings, ident: str, mode: str | None, version: str = "") -> None:
    """mode None removes the pin."""
    return None


def blocks(pin: dict | None, installed: str, candidate: str) -> bool:
    """True when the pin forbids updating to `candidate`."""
    return False


def available_versions(winget_id: str) -> list[str]:
    """Versions winget knows for a package, newest first. Slow (winget show --versions): call from a thread."""
    return []


def rollback_update(status, version: str):
    """A scanner.Update that installs `version` over the current one through winget (winget_mode='rollback')."""
    return None
