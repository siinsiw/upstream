"""The settings dialog's install-safety controls, offscreen, against a fake settings object (the real file is never touched)."""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt                    # noqa: E402
from PySide6.QtGui import QPalette, QWheelEvent                   # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from upstream import i18n                                         # noqa: E402
from upstream.config import JsonStore                             # noqa: E402
from upstream.ui import settings as settings_ui                   # noqa: E402
from upstream.ui import theme                                     # noqa: E402
from upstream.ui.locale import apply_language                     # noqa: E402
from upstream.ui.settings import POLICIES, SettingsDialog, proxy_address  # noqa: E402

app = QApplication.instance() or QApplication([])

DEFAULTS = {"language": "en", "theme": "system", "use_gh_cli_token": True, "include_prereleases": False,
            "publisher_policy": "ask", "close_apps_policy": "ask", "metered_policy": "ask", "battery_policy": "ask",
            "proxy": {"mode": "system", "url": ""}, "install_timeout_min": 30}


class FakeSettings:
    """The part of config.Settings the dialog uses, in memory."""

    def __init__(self, **over):
        self.data = copy.deepcopy(DEFAULTS)
        self.data.update(over)
        self.saves = 0
        self.fail: OSError | None = None

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        if self.fail:
            raise self.fail
        self.saves += 1


def pump():
    for _ in range(5):
        app.processEvents()


class SettingsUiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(theme, "DATA_DIR", Path(self._tmp.name))   # theme glyphs are cached there
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._reset_app)
        self.dialogs = []

    @staticmethod
    def _reset_app():
        apply_language(app, "en")
        app.setStyleSheet("")
        app.setPalette(QPalette())
        theme._current = theme.THEMES["paper"]

    def make(self, **over) -> tuple[SettingsDialog, FakeSettings]:
        fake = FakeSettings(**over)
        dialog = SettingsDialog(fake)
        self.addCleanup(dialog.close)
        return dialog, fake

    @staticmethod
    def pick(combo, data):
        combo.setCurrentIndex(combo.findData(data))

    # -- loading -------------------------------------------------------
    def test_defaults_load(self):
        d, fake = self.make()
        for key, _title, _hint, options in POLICIES:
            self.assertEqual(d.policy[key].currentData(), "ask", key)
            self.assertEqual([d.policy[key].itemData(i) for i in range(d.policy[key].count())], [v for v, _ in options])
        self.assertEqual(d.proxy_mode.currentData(), "system")
        self.assertFalse(d.proxy_url.isEnabled())
        self.assertEqual(d.timeout.currentData(), 30)
        self.assertEqual(d.timeout.currentText(), "30 minutes")
        self.assertEqual(fake.saves, 0, "opening the dialog must not write")

    def test_saved_values_load(self):
        d, fake = self.make(publisher_policy="block", close_apps_policy="auto", metered_policy="skip", battery_policy="ignore",
                            proxy={"mode": "custom", "url": "http://proxy.corp:3128"}, install_timeout_min=0)
        self.assertEqual([d.policy[k].currentData() for k in
                          ("publisher_policy", "close_apps_policy", "metered_policy", "battery_policy")],
                         ["block", "auto", "skip", "ignore"])
        self.assertEqual(d.proxy_mode.currentData(), "custom")
        self.assertTrue(d.proxy_url.isEnabled())
        self.assertEqual(d.proxy_url.text(), "http://proxy.corp:3128")
        self.assertEqual(d.timeout.currentData(), 0)
        self.assertEqual(d.timeout.currentText(), "No time limit")
        self.assertTrue(d.proxy_note.isHidden())
        self.assertEqual(fake.saves, 0)

    def test_odd_saved_values_show_the_defaults_and_are_left_alone(self):
        for proxy in ("system", None, ["custom"], {"mode": "pigeon"}, {"mode": "custom", "url": None}):
            d, fake = self.make(publisher_policy="bogus", close_apps_policy=None, metered_policy=["skip"],
                                proxy=proxy, install_timeout_min="soon")
            self.assertEqual(d.policy["publisher_policy"].currentData(), "ask")
            self.assertEqual(d.policy["close_apps_policy"].currentData(), "ask")
            self.assertEqual(d.policy["metered_policy"].currentData(), "ask")
            self.assertIn(d.proxy_mode.currentData(), ("system", "custom"))
            self.assertEqual(d.timeout.currentData(), 30)
            self.assertEqual(fake.saves, 0)
            self.assertEqual(fake.data["publisher_policy"], "bogus")

    def test_a_saved_limit_that_is_not_a_preset_is_kept_in_the_list(self):
        d, _ = self.make(install_timeout_min=75)
        self.assertEqual(d.timeout.currentData(), 75)
        self.assertEqual(d.timeout.currentText(), "75 minutes")
        values = [d.timeout.itemData(i) for i in range(d.timeout.count())]
        self.assertEqual(values, sorted(values))
        d, _ = self.make(install_timeout_min=1)
        self.assertEqual(d.timeout.currentText(), "1 minute")

    # -- changing ------------------------------------------------------
    def test_policy_change_is_saved_at_once(self):
        d, fake = self.make()
        for key, _t, _h, options in POLICIES:
            for value, _label in options[1:]:
                before = fake.saves
                self.pick(d.policy[key], value)
                self.assertEqual(fake.data[key], value)
                self.assertEqual(fake.saves, before + 1)
        self.assertFalse(d.sources_dirty, "install-safety changes do not need a re-check of the sources")

    def test_timeout_change_is_saved_as_a_number(self):
        d, fake = self.make()
        self.pick(d.timeout, 0)
        self.assertEqual(fake.data["install_timeout_min"], 0)
        self.pick(d.timeout, 120)
        self.assertEqual(fake.data["install_timeout_min"], 120)
        self.assertIsInstance(fake.data["install_timeout_min"], int)

    def test_source_checkboxes_still_work(self):
        d, fake = self.make()
        d.pre.setChecked(True)
        self.assertIs(fake.data["include_prereleases"], True)
        self.assertTrue(d.sources_dirty)

    # -- proxy ---------------------------------------------------------
    def test_proxy_address_table(self):
        good = {
            "proxy.corp:3128": "http://proxy.corp:3128",
            "http://proxy.corp:3128/": "http://proxy.corp:3128",
            "  https://user:pa55@proxy.corp:8443 ": "https://user:pa55@proxy.corp:8443",
            "user:pa55@10.0.0.1:8080": "http://user:pa55@10.0.0.1:8080",
            "http://[::1]:8080": "http://[::1]:8080",
            "http://proxy": "http://proxy",
        }
        for text, url in good.items():
            self.assertEqual(proxy_address(text), url, text)
        for text in ("", "   ", "proxy corp:80", "ftp://proxy:21", "socks5://proxy:1080", "http://", "http://:8080",
                     "http://proxy:99999", "http://proxy:abc", "http://proxy:8080/pac", "http://proxy:8080?x=1"):
            self.assertIsNone(proxy_address(text), text)

    def test_proxy_modes(self):
        d, fake = self.make()
        self.pick(d.proxy_mode, "none")
        self.assertEqual(fake.data["proxy"], {"mode": "none", "url": ""})
        self.assertFalse(d.proxy_url.isEnabled())
        self.pick(d.proxy_mode, "system")
        self.assertEqual(fake.data["proxy"]["mode"], "system")

    def test_custom_proxy_waits_for_a_usable_address(self):
        d, fake = self.make(proxy={"mode": "none", "url": ""})
        saves = fake.saves
        self.pick(d.proxy_mode, "custom")
        self.assertTrue(d.proxy_url.isEnabled())
        self.assertEqual(fake.data["proxy"]["mode"], "none", "the earlier setting stays until the address is usable")
        self.assertFalse(d.proxy_note.isHidden())
        self.assertEqual(d.proxy_note.property("tone"), "muted")
        d.proxy_url.setText("not a proxy")
        d.proxy_url.editingFinished.emit()
        self.assertEqual(d.proxy_note.property("tone"), "danger")
        self.assertEqual(fake.saves, saves)
        d.proxy_url.setText("proxy.corp:3128")
        d.proxy_url.editingFinished.emit()
        self.assertEqual(fake.data["proxy"], {"mode": "custom", "url": "http://proxy.corp:3128"})
        self.assertEqual(d.proxy_url.text(), "http://proxy.corp:3128")
        self.assertTrue(d.proxy_note.isHidden())
        saves = fake.saves
        d.proxy_url.editingFinished.emit()
        self.assertEqual(fake.saves, saves, "focus leaving an unchanged field does not rewrite the file")

    def test_leaving_custom_keeps_the_address(self):
        d, fake = self.make(proxy={"mode": "custom", "url": "http://proxy.corp:3128"})
        self.pick(d.proxy_mode, "none")
        self.assertEqual(fake.data["proxy"], {"mode": "none", "url": "http://proxy.corp:3128"})
        self.pick(d.proxy_mode, "custom")
        self.assertEqual(fake.data["proxy"], {"mode": "custom", "url": "http://proxy.corp:3128"})

    def test_closing_saves_an_address_typed_but_not_confirmed(self):
        d, fake = self.make()
        self.pick(d.proxy_mode, "custom")
        d.proxy_url.setText("http://10.1.1.1:8080")
        d.accept()
        self.assertEqual(fake.data["proxy"], {"mode": "custom", "url": "http://10.1.1.1:8080"})

    def test_custom_without_an_address_shows_the_note_on_open(self):
        d, fake = self.make(proxy={"mode": "custom", "url": ""})
        self.assertFalse(d.proxy_note.isHidden())
        self.assertEqual(fake.saves, 0)

    def test_values_survive_a_round_trip_through_the_json_store(self):
        path = Path(self._tmp.name) / "settings.json"
        store = JsonStore(path, copy.deepcopy(DEFAULTS))
        d = SettingsDialog(store)
        self.addCleanup(d.close)
        self.pick(d.policy["publisher_policy"], "block")
        self.pick(d.policy["battery_policy"], "ignore")
        self.pick(d.timeout, 90)
        self.pick(d.proxy_mode, "custom")
        d.proxy_url.setText("proxy.corp:3128")
        d.accept()
        again = JsonStore(path, copy.deepcopy(DEFAULTS))
        self.assertEqual(again.get("publisher_policy"), "block")
        self.assertEqual(again.get("battery_policy"), "ignore")
        self.assertEqual(again.get("install_timeout_min"), 90)
        self.assertEqual(again.get("proxy"), {"mode": "custom", "url": "http://proxy.corp:3128"})
        reopened = SettingsDialog(again)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.policy["publisher_policy"].currentData(), "block")
        self.assertEqual(reopened.proxy_url.text(), "http://proxy.corp:3128")
        self.assertEqual(reopened.timeout.currentData(), 90)

    # -- failures and wheel --------------------------------------------
    def test_a_failed_save_is_shown_and_clears_on_the_next_good_one(self):
        d, fake = self.make()
        d.show()
        fake.fail = OSError("disk full")
        with self.assertLogs("upstream.ui.settings", "WARNING"):
            self.pick(d.policy["publisher_policy"], "off")
        self.assertTrue(d.save_error.isVisible())
        self.assertIn("disk full", d.save_error.text())
        self.assertEqual(fake.data["publisher_policy"], "off", "kept in memory for this session")
        fake.fail = None
        self.pick(d.policy["publisher_policy"], "ask")
        self.assertFalse(d.save_error.isVisible())

    def test_the_wheel_never_changes_a_value(self):
        d, fake = self.make()
        d.show()
        for combo in (d.lang, d.timeout, d.proxy_mode, *d.policy.values()):
            before = combo.currentIndex()
            wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                                Qt.NoModifier, Qt.NoScrollPhase, False)
            QApplication.sendEvent(combo, wheel)
            self.assertEqual(combo.currentIndex(), before)
        self.assertEqual(fake.saves, 0)

    # -- language, RTL, theme ------------------------------------------
    def _x(self, dialog, widget) -> int:
        return widget.mapTo(dialog, QPoint(0, 0)).x()

    def test_english_and_persian_both_render_with_the_right_direction(self):
        d, fake = self.make()
        d.language_changed.connect(lambda lang: apply_language(app, lang))
        d.show()
        pump()
        row = d.rows[0]
        self.assertEqual(row.title.text(), "Publisher check")
        self.assertLess(self._x(d, row.title), self._x(d, row.control), "English: text left, control right")
        english = [d.policy[k].itemText(1) for k, *_ in POLICIES] + [d.proxy_mode.itemText(0), d.timeout.currentText()]

        self.pick(d.lang, "fa")
        pump()
        self.assertTrue(i18n.is_rtl())
        self.assertEqual(d.layoutDirection(), Qt.RightToLeft)
        self.assertEqual(row.title.text(), i18n.FA["Publisher check"])
        self.assertGreater(self._x(d, row.title), self._x(d, row.control), "Persian: text right, control left")
        self.assertGreater(self._x(d, d.proxy_label), self._x(d, d.proxy_url), "the address label is on the right")
        self.assertEqual(d.proxy_url.layoutDirection(), Qt.LeftToRight, "an address stays left-to-right")
        persian = [d.policy[k].itemText(1) for k, *_ in POLICIES] + [d.proxy_mode.itemText(0), d.timeout.currentText()]
        self.assertTrue(all(p != e for p, e in zip(persian, english)), persian)
        shown = d.timeout.currentText().replace(i18n._FSI, "").replace(i18n._PDI, "")
        self.assertEqual(shown, i18n.FA["{n} minutes"].format(n=chr(0x6F3) + chr(0x6F0)), "numbers go through num()")
        self.assertEqual(d.safety_title.text(), i18n.FA["Install safety"])
        self.assertEqual(d.done_btn.text(), i18n.FA["Done"])
        self.assertEqual(d.policy["publisher_policy"].accessibleName(), i18n.FA["Publisher check"])
        self.assertEqual(fake.saves, 0, "switching language alone changes no safety value")
        self.assertEqual([d.policy[k].currentData() for k in d.policy], ["ask"] * 4)

        self.pick(d.lang, "en")
        pump()
        self.assertEqual(d.layoutDirection(), Qt.LeftToRight)
        self.assertEqual(row.title.text(), "Publisher check")
        self.assertEqual(d.timeout.currentText(), "30 minutes")

    def test_language_switch_keeps_choices_and_the_note(self):
        d, fake = self.make(publisher_policy="block", install_timeout_min=60, proxy={"mode": "custom", "url": ""})
        d.language_changed.connect(lambda lang: apply_language(app, lang))
        self.pick(d.lang, "fa")
        self.assertEqual(d.policy["publisher_policy"].currentData(), "block")
        self.assertEqual(d.timeout.currentData(), 60)
        self.assertEqual(d.proxy_note.text(), i18n.tr("Enter the proxy as http://host:port. The previous setting stays in use until then."))
        self.assertNotIn("Enter the proxy", d.proxy_note.text())

    def test_it_follows_the_theme(self):
        for key in ("paper", "graphite", "contrast"):
            theme.apply_theme(app, key)
            d, _ = self.make()
            d.show()
            pump()
            colours = theme.THEMES[key]
            self.assertEqual(d.grab().toImage().pixelColor(2, 2).name().upper(), colours["bg"].upper(), key)
            self.assertEqual(d.proxy_note.property("tone") or "", "")
            d.close()

    def test_it_fits_the_screen_with_a_parent_window(self):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        self.addCleanup(parent.close)
        d = SettingsDialog(FakeSettings(), parent)
        self.addCleanup(d.close)
        self.assertLessEqual(d.height(), parent.screen().availableGeometry().height())

    def test_it_fits_the_screen(self):
        d, _ = self.make()
        room = QApplication.primaryScreen().availableGeometry().height()
        self.assertLessEqual(d.height(), room)
        self.assertGreaterEqual(d.width(), 640)


class TextTest(unittest.TestCase):
    def test_every_label_has_a_persian_entry(self):
        templates = [t for _k, title, hint, options in POLICIES for t in (title, hint, *(label for _v, label in options))]
        templates += [label for _m, label in settings_ui.PROXY_MODES]
        for template in templates:
            self.assertIn(template, i18n.FA, template)


if __name__ == "__main__":
    unittest.main()
