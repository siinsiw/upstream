"""Publisher check: refuse or question an update signed by someone other than the app's usual publisher.
Owner: the publisher-signature feature."""
from __future__ import annotations

from dataclasses import dataclass, field

from .inventory import InstalledApp


@dataclass(frozen=True)
class Decision:
    action: str                    # allow | ask | block
    template: str = ""             # English question or reason
    values: dict = field(default_factory=dict)
    detail: str = ""               # English template for the second paragraph, same values


def evaluate(settings, app: InstalledApp, sig_status: str, signer: str) -> Decision:
    """sig_status is Authenticode's status (Valid, NotSigned, ...); signer is the certificate's CN."""
    return Decision("allow")


def remember(settings, app: InstalledApp, signer: str) -> None:
    """Called after a successful install: this signer is now the app's known publisher."""
    return None
