"""Applies a UI language to Qt itself: layout direction and Qt's own strings
(the Cut/Copy/Paste menu of text fields, standard dialog buttons)."""
from __future__ import annotations

from PySide6.QtCore import QLibraryInfo, Qt, QTranslator

from .. import i18n

# Held at module level: PySide6 does not keep installed translators alive,
# and a collected translator silently turns Qt's menus back to English.
_qt_translator: QTranslator | None = None


def apply_language(app, lang: str):
    global _qt_translator
    i18n.set_language(lang)
    app.setLayoutDirection(Qt.RightToLeft if i18n.is_rtl() else Qt.LeftToRight)
    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None
    if i18n.language() != "en":
        translator = QTranslator()
        if translator.load(f"qtbase_{i18n.language()}", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(translator)
            _qt_translator = translator
