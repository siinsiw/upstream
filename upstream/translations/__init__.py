"""Persian translations, one module per feature so parallel work never edits the same file.
i18n.FA is the union of every module's FA. Add a new module here when you add one."""
from . import detection, exitcodes, inplace, net, pipeline, queue_cancel, running, settings_ui, trust, verify

PARTS = (pipeline, detection, exitcodes, verify, inplace, trust, running, net, queue_cancel, settings_ui)


def collect() -> dict[str, str]:
    merged: dict[str, str] = {}
    for part in PARTS:
        clash = merged.keys() & part.FA.keys()
        if clash:
            raise ValueError(f"{part.__name__} repeats translations already defined: {sorted(clash)[:3]}")
        merged.update(part.FA)
    return merged
