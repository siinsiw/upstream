"""Hold an app on a version (or a major line) and roll back to an older one.

A pin lives in settings["pins"]: {ident: {"mode": "hold" | "major", "version": "1.2.3"}}. Hold offers no update
at all. Major offers only updates inside the major line the app is on (3.12.4 -> 3.13.0, never 4.0.0)."""
from __future__ import annotations

import logging

from . import winget
from .versioning import parse_version

log = logging.getLogger("upstream.pins")

MODES = ("hold", "major")


def clean_pin(raw) -> dict | None:
    """A pin read from the settings or a snapshot, or None when it is not a valid one."""
    if not isinstance(raw, dict) or raw.get("mode") not in MODES:
        return None
    version = raw.get("version")
    return {"mode": raw["mode"], "version": version.strip() if isinstance(version, str) else ""}


def pin_for(settings, ident: str) -> dict | None:
    """{"mode": "hold" or "major", "version": "1.2.3"} or None."""
    pins = settings.get("pins", {})
    return clean_pin(pins.get(ident)) if isinstance(pins, dict) else None


def set_pin(settings, ident: str, mode: str | None, version: str = "") -> None:
    """mode None removes the pin. `version` is the version the app is held at (shown to the user)."""
    if mode is not None and mode not in MODES:
        raise ValueError(f"Unknown pin mode: {mode!r}")
    pins = settings.get("pins", {})
    pins = dict(pins) if isinstance(pins, dict) else {}
    if mode is None:
        pins.pop(ident, None)
    else:
        pins[ident] = {"mode": mode, "version": version}
    settings.set("pins", pins)
    settings.save()


def major_line(pin: dict | None, installed: str) -> int | None:
    """The major version a 'major' pin keeps the app on: the one installed now (the app may have moved on its
    own since the pin was set), else the one the pin was set at. None when neither has a number."""
    for text in (installed, (pin or {}).get("version")):
        core = parse_version(text)
        if core:
            return core[0]
    return None


def blocks(pin: dict | None, installed: str, candidate: str) -> bool:
    """True when the pin forbids updating to `candidate`. A candidate whose major version can't be read is
    held back too: it can't be shown to be inside the line."""
    pin = clean_pin(pin)
    if pin is None:
        return False
    if pin["mode"] == "hold":
        return True
    line, core = major_line(pin, installed), parse_version(candidate)
    return line is None or not core or core[0] != line


def available_versions(winget_id: str) -> list[str]:
    """Versions winget knows for a package, newest first. Slow (winget show --versions): call from a thread.
    Empty when winget is missing, fails, or does not know the package."""
    if not winget_id or not winget.available():
        return []
    try:
        found = winget.versions(winget_id)
    except Exception:
        log.warning("winget could not list the versions of %s", winget_id, exc_info=True)
        return []
    return [v for v in found if any(c.isdigit() for c in v)]


def rollback_update(status, version: str):
    """A scanner.Update that installs `version` over the current one through winget (winget_mode='rollback').
    None when winget does not know the app. Run it like any other update, with `version=update.version` handed
    to winget.update; winget.py explains which packages cannot go back."""
    if not status.winget_id or not version:
        return None
    from .scanner import Update   # scanner imports this module
    return Update("winget", version, status.winget_id, winget_mode="rollback")
