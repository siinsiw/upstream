"""Publisher gate: who counts as the usual publisher, what each signature status leads to, what is remembered.
No PowerShell runs here: the signer lookup is patched."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from upstream import trust
from upstream.errors import InstallError
from upstream.inventory import InstalledApp


class FakeSettings:
    def __init__(self, **data):
        self.data = {"publisher_policy": "ask", "signers": {}, "signers_approved": {}, **data}
        self.saved = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        self.saved += 1


def make_app(**over):
    fields = dict(reg_key=r"HKLM64\Demo", name="Demo App", version="1.0", publisher="Demo Software Inc.",
                  scope="machine", arch="x64")
    fields.update(over)
    return InstalledApp(**fields)


class TrustTestCase(unittest.TestCase):
    def setUp(self):
        trust._cache.clear()
        trust._seen.clear()
        patcher = mock.patch.object(trust, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def evaluate(self, status, signer, settings=None, installed="", app=None):
        """evaluate() with the installed copy's signer fixed (no file is inspected)."""
        with mock.patch.object(trust, "_installed_signer", return_value=installed):
            return trust.evaluate(settings if settings is not None else FakeSettings(), app or make_app(), status, signer)


class SamePublisherTest(TrustTestCase):
    SAME = [("Microsoft Corporation", "Microsoft Corp."), ("Google LLC", "google inc"), ("Google LLC", "Google, LLC"),
            ("Dropbox, Inc.", "Dropbox"), ("Notepad++", "Notepad++"), ("Spotify AB", "SPOTIFY"),
            ("Igor Pavlov", "Pavlov Igor"), ("Valve Corp.", "Valve"), ("Mozilla Corporation", "MOZILLA CORP")]
    # A longer or shorter name is a different certificate holder, whichever way round.
    SPOOFS = [("Microsoft Corporation", "Microsoft Corporation Fake Ltd"), ("Google LLC", "Google Updates Ltd"),
              ("Mozilla Corporation", "Mozilla Foundation"), ("Notepad++ Team", "Notepad++"),
              ("Adobe Inc.", "Adobe Systems Incorporated")]
    DIFFERENT = [("Adobe Inc.", "Mozilla Corporation"), ("Microsoft Corporation", "Micro Star International"),
                 ("Demo", "Cleaner Pro Ltd"), ("", "Demo"), ("Inc.", "Demo Inc."), ("Demo", ""), ("Inc.", "LLC")]

    def test_certificate_names_match_when_only_case_punctuation_order_and_legal_forms_differ(self):
        for a, b in self.SAME:
            for x, y in ((a, b), (b, a)):
                with self.subTest(a=x, b=y):
                    self.assertTrue(trust._same_publisher(x, y))
                    self.assertTrue(trust._same_publisher(x, y, strong=False))

    def test_certificate_names_must_be_equal_not_just_contained(self):
        for a, b in self.SPOOFS:
            for x, y in ((a, b), (b, a)):
                with self.subTest(a=x, b=y):
                    self.assertFalse(trust._same_publisher(x, y))

    def test_registry_text_only_has_to_be_contained_in_the_signer_name(self):
        for text, signer in [("Notepad++", "Notepad++ Team"), ("Google LLC", "Google Updates Ltd"),
                             ("Demo Software Inc.", "Demo Software Europe GmbH")]:
            with self.subTest(text=text, signer=signer):
                self.assertTrue(trust._same_publisher(text, signer, strong=False))
        for text, signer in [("Notepad++ Team", "Notepad++"), ("Google Updates Ltd", "Google LLC"),
                             ("Mozilla Foundation", "Mozilla Corporation")]:
            with self.subTest(text=text, signer=signer):
                self.assertFalse(trust._same_publisher(text, signer, strong=False))

    def test_different_names_do_not_match(self):
        for a, b in self.DIFFERENT:
            for strong in (True, False):
                with self.subTest(a=a, b=b, strong=strong):
                    self.assertFalse(trust._same_publisher(a, b, strong))


class PolicyTest(TrustTestCase):
    def test_off_allows_everything_without_looking_anything_up(self):
        settings = FakeSettings(publisher_policy="off", signers={"demo app": "Someone Else"})
        with mock.patch.object(trust, "_installed_signer", side_effect=AssertionError("looked up")):
            for status, signer in [("Valid", "Mallory Ltd"), ("NotSigned", ""), ("HashMismatch", "X")]:
                self.assertEqual(trust.evaluate(settings, make_app(), status, signer).action, "allow")

    def test_same_publisher_allows(self):
        for status_signer in [("Valid", "Demo Software, Inc"), ("Valid", "DEMO SOFTWARE")]:
            self.assertEqual(self.evaluate(*status_signer, installed="Demo Software Inc").action, "allow")

    def test_different_signer_asks_and_names_everyone(self):
        d = self.evaluate("Valid", "Mallory Ltd", installed="Demo Software Inc")
        self.assertEqual(d.action, "ask")
        self.assertEqual(d.values, {"name": "Demo App", "usual": "Demo Software Inc", "signer": "Mallory Ltd"})
        self.assertIn("{signer}", d.template)
        self.assertTrue(d.detail)

    def test_a_lookalike_signer_is_not_the_installed_publisher(self):
        for installed, signer in [("Microsoft Corporation", "Microsoft Corporation Fake Ltd"),
                                  ("Google LLC", "Google Updates Ltd"), ("Mozilla Corporation", "Mozilla Foundation")]:
            for policy, action in (("ask", "ask"), ("block", "block")):
                d = self.evaluate("Valid", signer, FakeSettings(publisher_policy=policy), installed=installed)
                with self.subTest(installed=installed, signer=signer, policy=policy):
                    self.assertEqual((d.action, d.values["usual"], d.values["signer"]), (action, installed, signer))

    def test_spelling_variants_of_the_installed_publisher_are_allowed(self):
        for installed, signer in [("Google LLC", "Google, LLC"), ("Microsoft Corporation", "Microsoft Corp."),
                                  ("Notepad++", "Notepad++")]:
            with self.subTest(installed=installed, signer=signer):
                self.assertEqual(self.evaluate("Valid", signer, installed=installed).action, "allow")

    def test_block_policy_blocks_a_different_signer(self):
        d = self.evaluate("Valid", "Mallory Ltd", FakeSettings(publisher_policy="block"), installed="Demo Software Inc")
        self.assertEqual(d.action, "block")
        self.assertEqual(d.values["signer"], "Mallory Ltd")

    def test_unknown_policy_value_behaves_like_ask(self):
        d = self.evaluate("Valid", "Mallory Ltd", FakeSettings(publisher_policy="maybe"), installed="Demo Software Inc")
        self.assertEqual(d.action, "ask")

    def test_missing_settings_means_ask(self):
        with mock.patch.object(trust, "_installed_signer", return_value="Demo Software Inc"):
            d = trust.evaluate(None, make_app(), "Valid", "Mallory Ltd")
        self.assertEqual(d.action, "ask")

    def test_every_template_formats_with_its_values(self):
        cases = [("Valid", "Mallory Ltd"), ("NotSigned", ""), ("NotTrusted", "Mallory Ltd"), ("Unknown", ""),
                 ("Incompatible", "")]
        for policy in ("ask", "block"):
            for status, signer in cases:
                d = self.evaluate(status, signer, FakeSettings(publisher_policy=policy), installed="Demo Software Inc")
                with self.subTest(policy=policy, status=status):
                    self.assertEqual(d.action, policy)
                    self.assertIn("Demo App", InstallError(d.template, **d.values).args[0])
                    d.detail.format(**d.values)


class NoBaselineTest(TrustTestCase):
    """Nothing signed to compare with: unsigned is normal, but a signature Windows rejects is not."""
    BAD = [("NotTrusted", "Mallory Ltd"), ("UnknownError", ""), ("Incompatible", ""), ("HashMismatch", "Mallory Ltd"),
           ("NotSupportedFileFormat", ""), ("SomethingNew", "")]

    def test_only_not_signed_and_unknown_pass_silently(self):
        for publisher in ("Demo Software Inc.", ""):
            for status in ("NotSigned", "Unknown", ""):
                for policy in ("ask", "block"):
                    d = self.evaluate(status, "", FakeSettings(publisher_policy=policy),
                                      app=make_app(publisher=publisher))
                    with self.subTest(publisher=publisher, status=status, policy=policy):
                        self.assertEqual(d.action, "allow")

    def test_a_failed_signature_lookup_is_logged_not_silent(self):
        self.assertEqual(self.evaluate("Unknown", "").action, "allow")
        self.log.warning.assert_called_once()

    def test_any_other_bad_status_asks_or_blocks(self):
        for publisher in ("Demo Software Inc.", ""):
            for status, signer in self.BAD:
                for policy in ("ask", "block"):
                    d = self.evaluate(status, signer, FakeSettings(publisher_policy=policy),
                                      app=make_app(publisher=publisher))
                    with self.subTest(publisher=publisher, status=status, policy=policy):
                        self.assertEqual(d.action, policy)
                        self.assertEqual(d.values["name"], "Demo App")
                        self.assertIn("couldn't verify", d.template)
                        self.assertEqual(bool(d.detail), policy == "ask")
                        self.assertIn("Demo App", InstallError(d.template, **d.values).args[0])
                        d.detail.format(**d.values)

    def test_the_registry_text_is_named_when_there_is_one(self):
        d = self.evaluate("NotTrusted", "Mallory Ltd")
        self.assertEqual(d.values, {"name": "Demo App", "usual": "Demo Software Inc."})
        d = self.evaluate("NotTrusted", "Mallory Ltd", app=make_app(publisher=""))
        self.assertEqual(d.values, {"name": "Demo App"})

    def test_valid_signatures_are_unaffected(self):
        self.assertEqual(self.evaluate("Valid", "Anyone Ltd", app=make_app(publisher="")).action, "allow")
        self.assertEqual(self.evaluate("Valid", "", app=make_app(publisher="")).action, "allow")
        self.assertEqual(self.evaluate("Valid", "Demo Software Europe GmbH").action, "allow")
        self.assertEqual(self.evaluate("Valid", "Mallory Ltd").action, "ask")


class UnsignedTest(TrustTestCase):
    def test_unsigned_with_a_signed_baseline_asks_or_blocks(self):
        ask = self.evaluate("NotSigned", "", installed="Demo Software Inc")
        self.assertEqual(ask.action, "ask")
        self.assertEqual(ask.values, {"name": "Demo App", "usual": "Demo Software Inc"})
        self.assertIn("isn't digitally signed", ask.template)
        block = self.evaluate("NotSigned", "", FakeSettings(publisher_policy="block"), installed="Demo Software Inc")
        self.assertEqual(block.action, "block")

    def test_untrusted_or_unknown_status_gets_its_own_wording(self):
        unsigned = self.evaluate("NotSigned", "", installed="Demo Software Inc")
        for status, signer in [("NotTrusted", "Demo Software Inc"), ("UnknownError", ""), ("Unknown", ""),
                               ("Incompatible", ""), ("Valid", "")]:
            d = self.evaluate(status, signer, installed="Demo Software Inc")
            with self.subTest(status=status, signer=signer):
                self.assertEqual(d.action, "ask")
                self.assertNotEqual(d.template, unsigned.template)
                self.assertIn("couldn't verify", d.template)

    def test_unsigned_without_any_signed_baseline_is_allowed_and_logged(self):
        self.assertEqual(self.evaluate("NotSigned", "", installed="").action, "allow")
        self.log.info.assert_called_once()
        self.assertEqual(self.evaluate("NotSigned", "", installed="", app=make_app(publisher="")).action, "allow")

    def test_unsigned_is_allowed_when_only_the_registry_names_a_publisher(self):
        for policy in ("ask", "block"):
            d = self.evaluate("NotSigned", "", FakeSettings(publisher_policy=policy), installed="")
            self.assertEqual(d.action, "allow")

    def test_signed_file_for_an_app_with_no_baseline_is_allowed(self):
        app = make_app(publisher="")
        self.assertEqual(self.evaluate("Valid", "Anyone Ltd", installed="", app=app).action, "allow")


class BaselineOrderTest(TrustTestCase):
    def test_remembered_signer_beats_the_installed_files_and_the_registry(self):
        settings = FakeSettings(signers={"demo app": "Remembered Corp"})
        with mock.patch.object(trust, "_installed_signer", side_effect=AssertionError("not needed")):
            self.assertEqual(trust.evaluate(settings, make_app(), "Valid", "Remembered Corp.").action, "allow")
            d = trust.evaluate(settings, make_app(), "Valid", "Demo Software Inc")
        self.assertEqual(d.action, "ask")
        self.assertEqual(d.values["usual"], "Remembered Corp")

    def test_the_remembered_signer_must_match_exactly(self):
        settings = FakeSettings(signers={"demo app": "Microsoft Corporation"})
        with mock.patch.object(trust, "_installed_signer", side_effect=AssertionError("not needed")):
            self.assertEqual(trust.evaluate(settings, make_app(), "Valid", "Microsoft Corp").action, "allow")
            d = trust.evaluate(settings, make_app(), "Valid", "Microsoft Corporation Fake Ltd")
        self.assertEqual((d.action, d.values["usual"]), ("ask", "Microsoft Corporation"))

    def test_installed_signature_beats_the_registry_text(self):
        d = self.evaluate("Valid", "Demo Software Inc", installed="Real Vendor GmbH")
        self.assertEqual((d.action, d.values["usual"]), ("ask", "Real Vendor GmbH"))

    def test_registry_text_alone_asks_but_never_blocks(self):
        for policy in ("ask", "block"):
            d = self.evaluate("Valid", "Mallory Ltd", FakeSettings(publisher_policy=policy), installed="")
            with self.subTest(policy=policy):
                self.assertEqual(d.action, "ask")
                self.assertEqual(d.values["usual"], "Demo Software Inc.")

    def test_registry_text_alone_accepts_the_matching_signer(self):
        self.assertEqual(self.evaluate("Valid", "Demo Software Inc", installed="").action, "allow")


class RememberTest(TrustTestCase):
    def test_a_valid_signer_is_stored_under_the_app_ident(self):
        settings, app = FakeSettings(), make_app(name="Demo App (64-bit)")
        self.evaluate("Valid", "Demo Software Inc", settings, app=app)
        trust.remember(settings, app, "Demo Software Inc")
        self.assertEqual(settings.data["signers"], {"demo app": "Demo Software Inc"})
        self.assertEqual(settings.saved, 1)

    def test_other_statuses_are_not_stored(self):
        for status in ("NotTrusted", "UnknownError", "NotSigned", "Unknown"):
            settings = FakeSettings()
            self.evaluate(status, "Demo Software Inc", settings, installed="Demo Software Inc")
            trust.remember(settings, make_app(), "Demo Software Inc")
            with self.subTest(status=status):
                self.assertEqual(settings.data["signers"], {})
                self.assertEqual(settings.saved, 0)

    def test_without_an_earlier_evaluate_nothing_is_stored(self):
        settings = FakeSettings()
        trust.remember(settings, make_app(), "Demo Software Inc")
        self.assertEqual(settings.data["signers"], {})

    def test_explicit_status_is_trusted_over_nothing(self):
        settings = FakeSettings()
        trust.remember(settings, make_app(), "Demo Software Inc", "Valid")
        self.assertEqual(settings.data["signers"], {"demo app": "Demo Software Inc"})
        trust.remember(settings, make_app(), "Other Inc", "NotTrusted")
        self.assertEqual(settings.data["signers"], {"demo app": "Demo Software Inc"})

    def test_nothing_is_learned_while_the_check_is_off(self):
        settings = FakeSettings(publisher_policy="off")
        trust.evaluate(settings, make_app(), "Valid", "Demo Software Inc")
        trust.remember(settings, make_app(), "Demo Software Inc")
        self.assertEqual(settings.data["signers"], {})

    def test_an_unchanged_signer_does_not_rewrite_the_settings(self):
        settings = FakeSettings(signers={"demo app": "Demo Software Inc"})
        trust.remember(settings, make_app(), "Demo Software Inc", "Valid")
        self.assertEqual(settings.saved, 0)

    def test_a_new_signer_replaces_the_old_one_and_keeps_other_apps(self):
        settings = FakeSettings(signers={"demo app": "Old Name Inc", "other": "Other Inc"})
        trust.remember(settings, make_app(), "New Name Inc", "Valid")
        self.assertEqual(settings.data["signers"], {"demo app": "New Name Inc", "other": "Other Inc"})

    def test_missing_settings_and_empty_signer_are_ignored(self):
        trust.remember(None, make_app(), "Demo Software Inc", "Valid")
        settings = FakeSettings()
        trust.remember(settings, make_app(), "", "Valid")
        self.assertEqual(settings.saved, 0)

    def test_a_failing_save_does_not_raise(self):
        settings = FakeSettings()
        settings.save = mock.Mock(side_effect=OSError("disk full"))
        trust.remember(settings, make_app(), "Demo Software Inc", "Valid")
        self.log.warning.assert_called_once()

    def test_what_is_learned_is_used_next_time(self):
        settings = FakeSettings()
        trust.remember(settings, make_app(), "Demo Software Inc", "Valid")
        d = self.evaluate("Valid", "Mallory Ltd", settings, installed="")
        self.assertEqual((d.action, d.values["usual"]), ("ask", "Demo Software Inc"))


class ApprovedSignerTest(TrustTestCase):
    """The user said yes to a warning: that signer is accepted for this app, the remembered publisher stays."""

    def test_the_signer_is_added_and_the_remembered_publisher_is_never_replaced(self):
        settings = FakeSettings(signers={"demo app": "Real Vendor GmbH", "other": "Other Inc"})
        self.evaluate("Valid", "New Owner Ltd", settings, installed="")
        trust.remember(settings, make_app(), "New Owner Ltd", approved=True)
        self.assertEqual(settings.data["signers_approved"], {"demo app": ["New Owner Ltd"]})
        self.assertEqual(settings.data["signers"], {"demo app": "Real Vendor GmbH", "other": "Other Inc"})
        self.assertEqual(settings.saved, 1)

    def test_nothing_is_pinned_as_the_publisher_when_none_was_remembered_either(self):
        settings = FakeSettings()
        trust.remember(settings, make_app(), "New Owner Ltd", "Valid", approved=True)
        self.assertEqual(settings.data["signers"], {})
        self.assertEqual(settings.data["signers_approved"], {"demo app": ["New Owner Ltd"]})

    def test_no_duplicates_and_other_apps_are_kept(self):
        settings = FakeSettings(signers_approved={"demo app": ["New Owner Ltd"], "other": ["Else Inc"]})
        for spelling in ("New Owner Ltd", "NEW OWNER, Ltd.", "New Owner Inc"):
            trust.remember(settings, make_app(), spelling, "Valid", approved=True)
        self.assertEqual(settings.data["signers_approved"], {"demo app": ["New Owner Ltd"], "other": ["Else Inc"]})
        self.assertEqual(settings.saved, 0)
        trust.remember(settings, make_app(), "Third Party GmbH", "Valid", approved=True)
        self.assertEqual(settings.data["signers_approved"]["demo app"], ["New Owner Ltd", "Third Party GmbH"])
        self.assertEqual(settings.data["signers_approved"]["other"], ["Else Inc"])

    def test_old_or_damaged_settings_are_tolerated(self):
        for broken in (None, "text", ["a"], 5, {"demo app": "New Owner Ltd"}, {"demo app": None}, {"demo app": 7}):
            settings = FakeSettings(signers_approved=broken)
            trust.remember(settings, make_app(), "New Owner Ltd", "Valid", approved=True)
            with self.subTest(broken=broken):
                self.assertEqual(settings.data["signers_approved"]["demo app"], ["New Owner Ltd"])
        settings = FakeSettings()
        del settings.data["signers_approved"]
        self.assertEqual(self.evaluate("Valid", "Mallory Ltd", settings, installed="Real Vendor GmbH").action, "ask")
        trust.remember(settings, make_app(), "New Owner Ltd", "Valid", approved=True)
        self.assertEqual(settings.data["signers_approved"], {"demo app": ["New Owner Ltd"]})

    def test_only_a_valid_signature_is_accepted_and_never_while_the_check_is_off(self):
        settings = FakeSettings()
        trust.remember(settings, make_app(), "New Owner Ltd", "NotTrusted", approved=True)
        trust.remember(settings, make_app(), "New Owner Ltd", approved=True)
        trust.remember(settings, make_app(), "", "Valid", approved=True)
        trust.remember(None, make_app(), "New Owner Ltd", "Valid", approved=True)
        off = FakeSettings(publisher_policy="off")
        trust.remember(off, make_app(), "New Owner Ltd", "Valid", approved=True)
        for each in (settings, off):
            self.assertEqual(each.data["signers_approved"], {})
            self.assertEqual(each.saved, 0)

    def test_the_status_evaluate_saw_is_used_when_none_is_given(self):
        settings = FakeSettings()
        self.evaluate("Valid", "New Owner Ltd", settings, installed="Real Vendor GmbH")
        trust.remember(settings, make_app(), "New Owner Ltd", approved=True)
        self.assertEqual(settings.data["signers_approved"], {"demo app": ["New Owner Ltd"]})

    def test_a_failing_save_does_not_raise(self):
        settings = FakeSettings()
        settings.save = mock.Mock(side_effect=OSError("disk full"))
        trust.remember(settings, make_app(), "New Owner Ltd", "Valid", approved=True)
        self.log.warning.assert_called_once()

    def test_an_approved_signer_matches_the_strong_baseline_but_only_with_a_valid_signature(self):
        settings = FakeSettings(signers={"demo app": "Real Vendor GmbH"},
                                signers_approved={"demo app": ["New Owner Ltd"]})
        for signer in ("New Owner Ltd", "new owner, inc.", "New Owner"):
            with self.subTest(signer=signer):
                self.assertEqual(self.evaluate("Valid", signer, settings).action, "allow")
        for status in ("NotTrusted", "UnknownError", "NotSigned"):
            with self.subTest(status=status):
                self.assertEqual(self.evaluate(status, "New Owner Ltd", settings).action, "ask")

    def test_an_approved_signer_is_only_good_for_its_own_app_and_by_exact_name(self):
        settings = FakeSettings(signers={"demo app": "Real Vendor GmbH", "other": "Real Vendor GmbH"},
                                signers_approved={"other": ["New Owner Ltd"]})
        self.assertEqual(self.evaluate("Valid", "New Owner Ltd", settings).action, "ask")
        other = make_app(name="Other", reg_key=r"HKLM64\Other")
        self.assertEqual(self.evaluate("Valid", "New Owner Ltd", settings, app=other).action, "allow")
        settings.data["signers_approved"]["demo app"] = ["New Owner Ltd"]
        self.assertEqual(self.evaluate("Valid", "New Owner Fake Ltd", settings).action, "ask")
        self.assertEqual(self.evaluate("Valid", "New Owner Ltd", settings).action, "allow")

    def test_an_approved_signer_also_stops_registry_text_questions(self):
        settings = FakeSettings(signers_approved={"demo app": ["New Owner Ltd"]})
        self.assertEqual(self.evaluate("Valid", "New Owner Ltd", settings, installed="").action, "allow")
        self.assertEqual(self.evaluate("Valid", "Mallory Ltd", settings, installed="").action, "ask")

    def test_the_real_publisher_is_not_questioned_after_the_user_approved_another_signer(self):
        settings, app = FakeSettings(signers={"demo app": "Real Vendor GmbH"}), make_app()
        self.assertEqual(self.evaluate("Valid", "New Owner Ltd", settings, app=app).action, "ask")
        trust.remember(settings, app, "New Owner Ltd", approved=True)
        self.assertEqual(settings.data["signers"], {"demo app": "Real Vendor GmbH"})
        self.assertEqual(self.evaluate("Valid", "Real Vendor GmbH", settings, app=app).action, "allow")
        self.assertEqual(self.evaluate("Valid", "New Owner Ltd", settings, app=app).action, "allow")
        self.assertEqual(self.evaluate("Valid", "Mallory Ltd", settings, app=app).action, "ask")

    def test_a_silent_install_by_an_approved_signer_does_not_become_the_remembered_publisher(self):
        settings = FakeSettings(signers={"demo app": "Real Vendor GmbH"},
                                signers_approved={"demo app": ["New Owner Ltd"]})
        self.evaluate("Valid", "New Owner Ltd", settings)
        trust.remember(settings, make_app(), "New Owner Ltd")
        self.assertEqual(settings.data["signers"], {"demo app": "Real Vendor GmbH"})
        self.assertEqual(settings.saved, 0)

    def test_the_default_still_replaces_the_remembered_publisher(self):
        settings = FakeSettings(signers={"demo app": "Old Name Inc"}, signers_approved={"demo app": ["Someone Else"]})
        trust.remember(settings, make_app(), "New Name Inc", "Valid", approved=False)
        self.assertEqual(settings.data["signers"], {"demo app": "New Name Inc"})
        self.assertEqual(settings.data["signers_approved"], {"demo app": ["Someone Else"]})


class InstalledSignerTest(TrustTestCase):
    """_installed_signer finds the installed files and asks installer.signature about them (patched)."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="upstream-trust-"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path in sorted(self.dir.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.dir.rmdir()

    def touch(self, name):
        path = self.dir / name
        path.write_bytes(b"MZ")
        return str(path)

    def lookup(self, answers, app):
        """Run _installed_signer with installer.signature answering from `answers` (file name -> (status, CN))."""
        seen = []

        def fake(path):
            seen.append(path.name)
            return answers.get(path.name, ("NotSigned", ""))

        with mock.patch.object(trust.installer, "signature", fake):
            return trust._installed_signer(app), seen

    def test_main_exe_is_found_by_the_app_name_and_helpers_are_skipped(self):
        self.touch("unins000.exe")
        self.touch("crashhandler.exe")
        self.touch("Demo App.exe")
        app = make_app(install_location=str(self.dir))
        signer, seen = self.lookup({"Demo App.exe": ("Valid", "Demo Software Inc")}, app)
        self.assertEqual((signer, seen), ("Demo Software Inc", ["Demo App.exe"]))

    def test_icon_comes_first_then_the_uninstaller_and_the_first_valid_signature_wins(self):
        icon, uninstall = self.touch("demo-main.exe"), self.touch("remove.exe")
        app = make_app(icon_path=icon, uninstall_string=f'"{uninstall}" /S')
        signer, seen = self.lookup({"demo-main.exe": ("NotSigned", ""), "remove.exe": ("Valid", "Demo Software Inc")}, app)
        self.assertEqual((signer, seen), ("Demo Software Inc", ["demo-main.exe", "remove.exe"]))

    def test_unquoted_uninstall_string_with_spaces_is_understood(self):
        folder = self.dir / "Program Files X"
        folder.mkdir()
        exe = folder / "uninst.exe"
        exe.write_bytes(b"MZ")
        self.assertEqual(trust._program(f"{exe} /S /norestart"), str(exe))
        self.assertEqual(trust._program(f'"{exe}" /S'), str(exe))
        self.assertEqual(trust._program("msiexec.exe /x {GUID}"), "")
        self.assertEqual(trust._program(""), "")

    def test_files_under_the_windows_folder_are_never_inspected(self):
        windows = os.environ["SystemRoot"]
        msiexec = os.path.join(windows, "System32", "msiexec.exe")
        self.assertFalse(trust._signable(msiexec))
        app = make_app(icon_path=msiexec, uninstall_string=f"{msiexec} /x {{GUID}}")
        self.assertEqual(self.lookup({}, app), ("", []))

    def test_only_exe_and_dll_files_that_exist_are_candidates(self):
        self.assertFalse(trust._signable(str(self.dir / "missing.exe")))
        (self.dir / "demo.ico").write_bytes(b"x")
        self.assertFalse(trust._signable(str(self.dir / "demo.ico")))
        self.assertFalse(trust._signable("demo.exe"))
        self.assertTrue(trust._signable(self.touch("demo.dll")))

    def test_unsigned_install_gives_no_baseline_and_is_cached(self):
        app = make_app(icon_path=self.touch("demo.exe"))
        self.assertEqual(self.lookup({}, app), ("", ["demo.exe"]))
        self.assertEqual(self.lookup({}, app), ("", []))
        newer = make_app(icon_path=app.icon_path, version="2.0")
        self.assertEqual(self.lookup({}, newer), ("", ["demo.exe"]))

    def test_a_signed_result_is_cached_too(self):
        app = make_app(icon_path=self.touch("demo.exe"))
        self.assertEqual(self.lookup({"demo.exe": ("Valid", "Demo Software Inc")}, app)[0], "Demo Software Inc")
        self.assertEqual(self.lookup({}, app), ("Demo Software Inc", []))

    def test_a_powershell_failure_is_not_remembered(self):
        app = make_app(icon_path=self.touch("demo.exe"))
        self.assertEqual(self.lookup({"demo.exe": ("Unknown", "")}, app), ("", ["demo.exe"]))
        self.assertEqual(self.lookup({"demo.exe": ("Valid", "Demo Software Inc")}, app)[0], "Demo Software Inc")

    def test_an_unexpected_error_in_the_lookup_is_survived_and_not_remembered(self):
        app = make_app(icon_path=self.touch("demo.exe"))
        with mock.patch.object(trust.installer, "signature", side_effect=AttributeError("no output")):
            self.assertEqual(trust._installed_signer(app), "")
        self.assertEqual(self.lookup({"demo.exe": ("Valid", "Demo Software Inc")}, app)[0], "Demo Software Inc")

    def test_an_app_without_any_files_to_inspect_runs_nothing(self):
        self.assertEqual(self.lookup({}, make_app()), ("", []))

    def test_evaluate_uses_the_lookup_end_to_end(self):
        app = make_app(icon_path=self.touch("demo.exe"))
        with mock.patch.object(trust.installer, "signature", lambda p: ("Valid", "Real Vendor GmbH")):
            d = trust.evaluate(FakeSettings(), app, "Valid", "Mallory Ltd")
        self.assertEqual((d.action, d.values["usual"]), ("ask", "Real Vendor GmbH"))


if __name__ == "__main__":
    unittest.main()
