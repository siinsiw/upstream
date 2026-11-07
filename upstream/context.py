"""What a running update can rely on: a cancel switch, a way to ask the user, and the settings."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .errors import Cancelled


class CancelToken:
    """Set from the UI thread, polled by the worker that downloads or waits for an installer."""

    def __init__(self):
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "user"):
        self.reason = reason
        self._event.set()

    @property
    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise Cancelled("Cancelled.")

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True as soon as the token is cancelled."""
        return self._event.wait(seconds)


@dataclass(frozen=True)
class Question:
    """A yes/no question for the user. All text is an English template (values are filled in
    when it is shown) so it follows the language that is active at that moment."""
    template: str
    values: dict = field(default_factory=dict)
    yes: str = "Yes"
    no: str = "No"
    detail: str = ""
    danger: bool = False          # warning icon; "No" is the default button


@dataclass
class UpdateContext:
    settings: object | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    ask_fn: Callable[[Question], bool] | None = None
    bulk: bool = False            # the job is part of "Update all"
    auto: bool = False            # started by an automatic-update rule while nobody is watching: never asks

    def setting(self, key: str, default=None):
        return self.settings.get(key, default) if self.settings is not None else default

    def ask(self, template: str, /, *, yes: str = "Yes", no: str = "No", detail: str = "",
            danger: bool = False, **values) -> bool:
        """Block the worker thread until the user answers. With no UI attached the answer is No."""
        self.cancel.check()
        if self.auto or self.ask_fn is None:
            return False
        answer = bool(self.ask_fn(Question(template, values, yes, no, detail, danger)))
        self.cancel.check()
        return answer
