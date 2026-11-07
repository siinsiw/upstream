"""One UAC prompt for a whole batch: run Upstream itself as administrator. Owner: the elevation feature."""
from __future__ import annotations


def is_elevated() -> bool:
    return False


def relaunch_elevated(extra_args: tuple = ()) -> bool:
    """Start this program again with administrator rights (one UAC prompt). True when it was launched;
    the caller then quits. False when the user declined."""
    return False


def machine_installs_pending(statuses) -> int:
    """How many of the updates in `statuses` will probably ask for administrator rights."""
    return 0
