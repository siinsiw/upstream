"""Search for new apps and install them. Owner: the discover feature."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Package:
    id: str                         # winget package id
    name: str
    version: str
    publisher: str = ""
    source: str = "winget"


def search(query: str, limit: int = 30) -> list[Package]:
    """winget search; slow, call from a thread. Raises RuntimeError with an English message when winget is missing."""
    return []


def status_for(pkg: Package):
    """A scanner.AppStatus the update queue can run: a synthetic app plus Update('winget', winget_mode='install')."""
    return None
