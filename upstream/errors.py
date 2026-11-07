"""Exceptions shared by the install pipeline."""
from __future__ import annotations


class InstallError(Exception):
    """Carries an English template and its values so the UI can show it in
    whichever language is active when the message is displayed."""

    def __init__(self, template: str, **values):
        super().__init__(template.format(**values) if values else template)
        self.template, self.values = template, values


class Cancelled(InstallError):
    """The user (or a policy the user chose) stopped the install. Not an error:
    no other source is tried afterwards."""
