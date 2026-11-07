"""Persian translations, one module per feature so parallel work never edits the same file.
i18n.FA is the union of every module's FA. Add a new module here when you add one."""
from . import (
    background, badges, cleanup, details, detection, discover, elevation, exitcodes, export, history, inplace,
    library, net, pipeline, pins, queue_cancel, running, settings_ui, settings_w2, shell, stability, trust, verify,
)

PARTS = (
    pipeline, detection, exitcodes, verify, inplace, trust, running, net, queue_cancel, settings_ui,
    cleanup, history, stability, background, details, export, elevation, pins, badges, discover, library, shell,
    settings_w2,
)


def collect() -> dict[str, str]:
    merged: dict[str, str] = {}
    for part in PARTS:
        clash = merged.keys() & part.FA.keys()
        if clash:
            raise ValueError(f"{part.__name__} repeats translations already defined: {sorted(clash)[:3]}")
        merged.update(part.FA)
    return merged
