"""App details: the registry facts the scan reads, details.py (size, signer, Repair, Modify) and the dialog.

Nothing here starts a repair, a modify window or PowerShell: installer._shell_run, details._open and
installer.signature are replaced, registry values come from fake keys, folders are temp trees."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6                                                          # noqa: E402
from PySide6.QtCore import QEvent                                         # noqa: E402
from PySide6.QtGui import QGuiApplication, QPalette                       # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit        # noqa: E402

from upstream import details, i18n, installer, inventory                  # noqa: E402
from upstream.context import CancelToken                                  # noqa: E402
from upstream.errors import Cancelled, InstallError, StoppedWaiting       # noqa: E402
from upstream.inventory import InstalledApp                               # noqa: E402
from upstream.scanner import AppStatus, Update                            # noqa: E402
from upstream.ui import details as ui                                     # noqa: E402
from upstream.ui import theme                                             # noqa: E402
from upstream.ui.locale import apply_language                             # noqa: E402

app = QApplication.instance() or QApplication([])

CODE = "{D916DC3C-6452-4692-92B9-DAECC5122C66}"


def make_app(**over) -> InstalledApp:
    fields = dict(reg_key=f"HKLM64\\{CODE}", name="Filmju", version="1.0.1", publisher="Filmju Ltd", scope="machine",
                  arch="x86", product_code=CODE, is_msi=True, install_kind="msi",
                  uninstall_string=f"MsiExec.exe /X{CODE}", modify_path=f"MsiExec.exe /I{CODE}")
    fields.update(over)
    return InstalledApp(**fields)


def wait_for(predicate, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --- the registry facts (inventory) ------------------------------------------------

class FakeKey:
    def __init__(self, values: dict, last_write: int = 0):
        self.values, self.last_write = values, last_write


def fake_query(key, name):
    if name not in key.values:
        raise FileNotFoundError(name)
    return key.values[name], 1


def filetime(moment: datetime) -> int:
    return int((moment.timestamp() + 11644473600) * 1e7)   # 100 ns steps since 1601


class RegistryFactsTest(unittest.TestCase):
    def setUp(self):
        for target, fake in (("QueryValueEx", fake_query), ("QueryInfoKey", lambda key: (0, 0, key.last_write))):
            patch = mock.patch.object(inventory.winreg, target, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def test_estimated_size(self):
        cases = [({"EstimatedSize": 2048}, 2048), ({"EstimatedSize": "512"}, 512), ({"EstimatedSize": " 7 "}, 7),
                 ({"EstimatedSize": 0}, 0), ({"EstimatedSize": 0xFFFFFFFF}, 0), ({"EstimatedSize": "lots"}, 0),
                 ({"EstimatedSize": b"\x01\x02"}, 0), ({"EstimatedSize": -5}, 0), ({}, 0)]
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(inventory._size_kb(FakeKey(values)), expected)

    def test_install_date_from_the_value(self):
        for raw, expected in [("20250628", "20250628"), ("2025-06-28", "20250628"), ("2025/6/8", "20250608"),
                              ("2025.06.08", "20250608"), (20250628, "20250628")]:
            with self.subTest(raw=raw):
                self.assertEqual(inventory._install_date(FakeKey({"InstallDate": raw})), expected)

    def test_install_date_falls_back_to_when_the_key_was_written(self):
        written = filetime(datetime(2024, 3, 5, 12, 0))
        for raw in ("", "20251345", "28/06/2025", "19800101", "20990101", "soon", None):
            with self.subTest(raw=raw):
                values = {} if raw is None else {"InstallDate": raw}
                self.assertEqual(inventory._install_date(FakeKey(values, written)), "20240305")

    def test_install_date_is_empty_without_any_usable_time(self):
        self.assertEqual(inventory._install_date(FakeKey({}, 0)), "")
        self.assertEqual(inventory._install_date(FakeKey({"InstallDate": "x"}, 2 ** 63)), "")

    def test_a_scanned_entry_carries_the_new_fields(self):
        key = FakeKey({"DisplayName": "Filmju", "DisplayVersion": "1.0.1", "Publisher": "Filmju Ltd",
                       "EstimatedSize": 10698, "InstallDate": "20250628", "WindowsInstaller": 1,
                       "ModifyPath": f"MsiExec.exe /I{CODE}", "NoModify": 1, "NoRepair": 0,
                       "UninstallString": f"MsiExec.exe /X{CODE}"})
        found = inventory._read_entry(key, f"HKLM64\\{CODE}", "machine", "x64")
        self.assertEqual((found.size_kb, found.install_date), (10698, "20250628"))
        self.assertEqual(found.modify_path, f"MsiExec.exe /I{CODE}")
        self.assertTrue(found.no_modify)
        self.assertFalse(found.no_repair)
        self.assertEqual(found.product_code, CODE)

    def test_defaults_when_the_key_says_nothing(self):
        found = inventory._read_entry(FakeKey({"DisplayName": "Plain"}, filetime(datetime(2023, 1, 2, 9, 0))),
                                      "HKCU\\Plain", "user", "")
        self.assertEqual((found.size_kb, found.modify_path, found.no_modify, found.no_repair), (0, "", False, False))
        self.assertEqual(found.install_date, "20230102")


class RealRegistryTest(unittest.TestCase):
    def test_every_installed_app_has_sane_values(self):
        apps = inventory.read_installed_apps()
        for a in apps:
            self.assertIsInstance(a.size_kb, int)
            self.assertGreaterEqual(a.size_kb, 0)
            if a.install_date:
                self.assertRegex(a.install_date, r"^\d{8}$")
                day = date(int(a.install_date[:4]), int(a.install_date[4:6]), int(a.install_date[6:]))
                self.assertLessEqual(day, date.today())
            self.assertIsInstance(a.modify_path, str)


# --- details.py ------------------------------------------------------------------------

class HelpersTest(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(details.format_size(0), "0 B")
        self.assertEqual(details.format_size(512), "512 B")
        self.assertEqual(details.format_size(2048), "2.0 KB")
        self.assertEqual(details.format_size(10953728), "10.4 MB")
        self.assertEqual(details.format_size(5 * 1024 ** 3), "5.0 GB")
        self.assertEqual(details.format_size(5000 * 1024 ** 3), "5000.0 GB")

    def test_format_date(self):
        self.assertEqual(details.format_date("20250628"), "2025-06-28")
        for bad in ("", "2025-06-28", "2025062", "abc", None):
            self.assertEqual(details.format_date(bad), "")

    def test_family_labels(self):
        self.assertEqual(details.family_label("msi"), "Windows Installer (MSI)")
        self.assertEqual(details.family_label("nullsoft"), "NSIS")
        self.assertEqual(details.family_label("exe"), "")
        self.assertEqual(details.family_label(""), "")


class WebLinksTest(unittest.TestCase):
    def test_keeps_web_addresses_only_without_duplicates(self):
        a = make_app(urls=["https://example.com/", "mailto:help@example.com", "C:\\Docs\\readme.txt", "Acme Support",
                           "http://example.com", "https://example.com/about", "ftp://example.com/x", "",
                           "https://exa mple.com", "https://"])
        self.assertEqual(details.web_links(a), ["https://example.com/", "https://example.com/about"])

    def test_repository_and_release_page_come_first(self):
        a = make_app(urls=["https://github.com/owner/project/", "https://example.com"])
        links = details.web_links(a, "owner/project", "https://github.com/owner/project/releases/tag/v2")
        self.assertEqual(links, ["https://github.com/owner/project", "https://github.com/owner/project/releases/tag/v2",
                                 "https://example.com"])

    def test_is_capped(self):
        a = make_app(urls=[f"https://example.com/{i}" for i in range(20)])
        self.assertEqual(len(details.web_links(a)), details.MAX_LINKS)

    def test_a_broken_address_is_skipped(self):
        self.assertEqual(details.web_links(make_app(urls=["https://[bad", "https://ok.example"])), ["https://ok.example"])


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def file(self, relative: str, size: int = 10) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path


class FolderTest(TempTree):
    def test_install_folder_prefers_the_recorded_location(self):
        self.file("Acme/app.exe")
        found = details.install_folder(make_app(install_location=str(self.root / "Acme")))
        self.assertEqual(found, str(self.root / "Acme"))

    def test_install_folder_from_the_icon_or_the_apps_own_uninstaller(self):
        icon = self.file("FromIcon/app.exe")
        self.assertEqual(details.install_folder(make_app(icon_path=str(icon))), str(icon.parent))
        unins = self.file("Inno/unins000.exe")
        self.assertEqual(details.install_folder(make_app(uninstall_string=f'"{unins}" /SILENT')), str(unins.parent))

    def test_a_cache_uninstaller_says_nothing_about_the_app(self):
        setup = self.file("Package Cache/{abc}/vc_redist.x64.exe")
        self.assertEqual(details.install_folder(make_app(uninstall_string=f'"{setup}" /uninstall')), "")

    def test_a_bundle_exe_in_a_cache_is_not_the_apps_folder(self):
        for relative in ("Package Cache/{B6CE88EB-2CE3-4D91-8EFC-425AE1F48CAF}/python.exe", "Pkg/{b6ce88eb-2ce3-4d91-8efc-425ae1f48caf}/x.exe",
                         "Local/Package Cache/anything/x.exe"):
            with self.subTest(relative=relative):
                self.assertEqual(details.install_folder(make_app(icon_path=str(self.file(relative)))), "")
        recorded = self.file("Package Cache/Acme/app.exe").parent    # a recorded InstallLocation is taken as it is
        self.assertEqual(details.install_folder(make_app(install_location=str(recorded))), str(recorded))

    def test_folders_that_hold_many_apps_or_do_not_exist_are_not_offered(self):
        for location in (os.environ.get("ProgramFiles", r"C:\Program Files"), "C:\\", str(self.root / "missing"),
                         "relative\\path", ""):
            with self.subTest(location=location):
                self.assertEqual(details.install_folder(make_app(install_location=location)), "")

    def test_folder_size_adds_up_nested_files(self):
        self.file("a.bin", 100)
        self.file("sub/b.bin", 250)
        self.file("sub/deeper/c.bin", 1000)
        self.assertEqual(details.folder_size(str(self.root)), (1350, True))

    def test_folder_size_does_not_follow_junctions(self):
        self.file("app/a.bin", 100)
        self.file("outside/big.bin", 5000)
        link = self.root / "app" / "link"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.root / "outside")],
                              capture_output=True, creationflags=installer.CREATE_NO_WINDOW)
        if made.returncode:
            self.skipTest("could not create a junction")
        self.assertEqual(details.folder_size(str(self.root / "app")), (100, True))

    def test_folder_size_caps_report_a_lower_bound(self):
        for i in range(10):
            self.file(f"f{i}.bin", 10)
        total, complete = details.folder_size(str(self.root), max_entries=4)
        self.assertFalse(complete)
        self.assertLess(total, 100)
        self.assertEqual(details.folder_size(str(self.root), seconds=-1)[1], False)
        token = CancelToken()
        token.cancel()
        self.assertEqual(details.folder_size(str(self.root), cancel=token), (0, False))

    def test_folder_size_of_a_missing_folder_is_zero(self):
        self.assertEqual(details.folder_size(str(self.root / "gone")), (0, True))

    def test_gather_uses_the_registry_size_and_formats_the_date(self):
        self.file("app/a.bin", 100)
        a = make_app(install_location=str(self.root / "app"), size_kb=2048, install_date="20250628",
                     urls=["https://example.com"], uninstall_string="x.exe /u")
        with mock.patch.object(inventory, "_msi_state", return_value=5):
            facts = details.gather(a, measure=True)
        self.assertEqual((facts.size_bytes, facts.size_approx), (2048 * 1024, False))
        self.assertEqual((facts.install_date, facts.install_location), ("2025-06-28", str(self.root / "app")))
        self.assertEqual((facts.name, facts.version, facts.scope, facts.arch), ("Filmju", "1.0.1", "machine", "x86"))
        self.assertEqual((facts.can_repair, facts.can_modify), (True, True))
        self.assertEqual((facts.urls, facts.uninstall_string, facts.installer_kind), (["https://example.com"], "x.exe /u", "msi"))

    def test_gather_measures_the_folder_only_when_asked(self):
        self.file("app/a.bin", 300)
        a = make_app(install_location=str(self.root / "app"), install_date="garbage")
        self.assertEqual(details.gather(a).size_bytes, 0)
        measured = details.gather(a, measure=True)
        self.assertEqual((measured.size_bytes, measured.size_approx, measured.install_date), (300, False, ""))


class SignerTest(TempTree):
    def setUp(self):
        super().setUp()
        details._signed.clear()
        self.addCleanup(details._signed.clear)

    def app_with(self, *names: str) -> InstalledApp:
        files = [self.file(f"Acme/{n}") for n in names]
        return make_app(reg_key="HKCU\\Acme", name="Acme", product_code=None, is_msi=False, install_kind="",
                        install_location=str(self.root / "Acme"), icon_path=str(files[0]),
                        uninstall_string=f'"{files[-1]}" /S')

    def run_signer(self, app_, answers):
        with mock.patch.object(installer, "signature", side_effect=answers) as probe:
            return details.signer_of(app_), probe

    def test_the_first_valid_signature_decides(self):
        result, probe = self.run_signer(self.app_with("acme.exe", "unins000.exe"), [("Valid", "Acme Inc")])
        self.assertEqual(result, ("Valid", "Acme Inc"))
        self.assertEqual(probe.call_count, 1)

    def test_a_signed_file_beats_an_unsigned_one(self):
        result, _ = self.run_signer(self.app_with("acme.exe", "unins000.exe"), [("NotSigned", ""), ("Valid", "Acme Inc")])
        self.assertEqual(result, ("Valid", "Acme Inc"))

    def test_nothing_signed(self):
        result, _ = self.run_signer(self.app_with("acme.exe", "unins000.exe"), [("NotSigned", ""), ("NotSigned", "")])
        self.assertEqual(result, ("NotSigned", ""))

    def test_a_signature_windows_does_not_trust_keeps_its_signer(self):
        result, _ = self.run_signer(self.app_with("acme.exe"), [("NotTrusted", "Shady Ltd")])
        self.assertEqual(result, ("NotTrusted", "Shady Ltd"))

    def test_powershell_failing_is_unknown_and_not_remembered(self):
        a = self.app_with("acme.exe")
        result, _ = self.run_signer(a, [("Unknown", "")])
        self.assertEqual(result, ("Unknown", ""))
        again, probe = self.run_signer(a, [("Valid", "Acme Inc")])
        self.assertEqual(again, ("Valid", "Acme Inc"))
        self.assertEqual(probe.call_count, 1)

    def test_an_exception_from_the_probe_is_unknown(self):
        with self.assertLogs("upstream.details", "WARNING"):
            result, _ = self.run_signer(self.app_with("acme.exe"), [OSError("no powershell")])
        self.assertEqual(result, ("Unknown", ""))

    def test_answers_are_cached_per_app_and_version(self):
        a = self.app_with("acme.exe")
        self.run_signer(a, [("Valid", "Acme Inc")])
        result, probe = self.run_signer(a, [])
        self.assertEqual(result, ("Valid", "Acme Inc"))
        probe.assert_not_called()

    def test_nothing_to_check(self):
        store = InstalledApp("WINGET\\Microsoft.WindowsTerminal", "Windows Terminal", "1.0", "Microsoft", "user", "x64")
        result, probe = self.run_signer(store, [])
        self.assertEqual(result, ("NoFile", ""))
        probe.assert_not_called()

    def test_files_under_the_windows_folder_say_nothing_about_the_app(self):
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        a = make_app(icon_path=os.path.join(windows, "System32", "msiexec.exe"), uninstall_string=f"MsiExec.exe /X{CODE}")
        result, probe = self.run_signer(a, [])
        self.assertEqual(result, ("NoFile", ""))
        probe.assert_not_called()


class RepairTest(TempTree):
    def setUp(self):
        super().setUp()
        state = mock.patch.object(inventory, "_msi_state", return_value=5)
        state.start()
        self.addCleanup(state.stop)

    def test_which_apps_can_be_repaired(self):
        self.assertTrue(details.can_repair(make_app()))
        self.assertIsNone(details.repair_blocker(make_app()))
        # The product code can come from the msiexec command when the key is not a GUID.
        self.assertTrue(details.can_repair(make_app(product_code=None, is_msi=True, reg_key="HKLM64\\Filmju")))
        for why, a in {
            "vendor said no": make_app(no_repair=True),
            "inno": make_app(product_code=None, is_msi=False, install_kind="inno", uninstall_string='"C:\\A\\unins000.exe"',
                             modify_path=""),
            "store": InstalledApp("WINGET\\X.Y", "XY", "1", "X", "user", "x64"),
            "no command": make_app(product_code=None, uninstall_string="", modify_path=""),
            "not msiexec": make_app(product_code=None, uninstall_string=f'"C:\\A\\setup.exe" /x {CODE}', modify_path=""),
        }.items():
            with self.subTest(why):
                self.assertFalse(details.can_repair(a))
                template, values = details.repair_blocker(a)
                self.assertIn(template, i18n.FA)

    def test_windows_installer_not_knowing_the_product_blocks_repair(self):
        for state, expected in ((-1, False), (1, False), (2, False), (3, True), (4, True), (5, True), (None, True)):
            with self.subTest(state=state), mock.patch.object(inventory, "_msi_state", return_value=state):
                self.assertEqual(details.can_repair(make_app()), expected)

    def run_repair(self, a, exit_code=0, **kwargs):
        steps = []
        with mock.patch.object(installer, "_shell_run", return_value=exit_code) as run:
            result = details.repair(a, lambda template, **values: steps.append((template, values)), **kwargs)
        return result, run, steps

    def test_repair_runs_msiexec_with_its_progress_window(self):
        result, run, steps = self.run_repair(make_app())
        self.assertEqual(result, (True, "{name} was repaired.", {"name": "Filmju"}))
        (file, params), kwargs = run.call_args
        self.assertTrue(file.lower().endswith("msiexec.exe"))
        self.assertEqual(params, f"/fa {CODE} /passive /norestart")
        self.assertNotIn("/qn", params)
        self.assertEqual(steps, [("Repairing {name}…", {"name": "Filmju"})])

    def test_exit_codes(self):
        cases = {
            3010: (True, "{name} was repaired. Restart Windows to finish.", {"name": "Filmju"}),
            1641: (True, "{name} was repaired. Restart Windows to finish.", {"name": "Filmju"}),
            1602: (False, "The repair was cancelled.", {}),
            1223: (False, "The repair was cancelled.", {}),
            1618: (False, "Another installation is running. Try again when it has finished.", {}),
            1603: (False, "Windows Installer hit a fatal error during installation (code {code})", {"code": 1603}),
            9999: (False, "Installer exited with code {code}", {"code": 9999}),
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self.run_repair(make_app(), code)[0], expected)

    def test_every_repair_message_is_translated(self):
        for code in (0, 3010, 1602, 1618, 1603, 9999):
            _, template, _ = self.run_repair(make_app(), code)[0]
            self.assertIn(template, i18n.FA)
        self.assertTrue(set(details.NEUTRAL) <= set(i18n.FA))

    def test_declined_permission_and_stop_waiting_come_back_as_messages(self):
        for error in (Cancelled("Administrator permission (UAC) was declined."),
                      StoppedWaiting("Stopped waiting for the installer. It may still be running."),
                      InstallError("Couldn't start the installer (Windows error {code}).", code=5)):
            with self.subTest(error=error.template), mock.patch.object(installer, "_shell_run", side_effect=error):
                ok, template, values = details.repair(make_app(), lambda *a, **k: None)
            self.assertFalse(ok)
            self.assertEqual((template, values), (error.template, error.values))
        self.assertTrue({"Administrator permission (UAC) was declined.",
                         "Stopped waiting for the installer. It may still be running."} <= set(details.NEUTRAL))

    def test_the_cancel_token_and_time_limit_are_passed_on(self):
        token = CancelToken()
        _, run, _ = self.run_repair(make_app(), cancel=token)
        self.assertIs(run.call_args.kwargs["cancel"], token)
        self.assertEqual(run.call_args.kwargs["timeout_min"], installer.timeout_minutes())

    def test_an_unexpected_error_is_reported_not_raised(self):
        with mock.patch.object(installer, "_shell_run", side_effect=RuntimeError("boom")), self.assertLogs("upstream.details", "ERROR"):
            self.assertEqual(details.repair(make_app(), lambda *a, **k: None), (False, "{error}", {"error": "boom"}))

    def test_nothing_starts_for_an_app_that_cannot_be_repaired(self):
        result, run, steps = self.run_repair(make_app(no_repair=True))
        self.assertFalse(result[0])
        self.assertEqual(result[1], "The installer of {name} says it can't be repaired.")
        run.assert_not_called()
        self.assertEqual(steps, [])


class ModifyTest(TempTree):
    def setUp(self):
        super().setUp()
        state = mock.patch.object(inventory, "_msi_state", return_value=5)
        state.start()
        self.addCleanup(state.stop)

    def vendor(self, args: str = "/modify", **over) -> InstalledApp:
        program = self.file("Vendor/setup.exe")
        return make_app(product_code=None, is_msi=False, install_kind="inno", reg_key="HKCU\\Vendor",
                        modify_path=f'"{program}" {args}'.strip(), **over)

    def test_an_msi_product_uses_msiexec_maintenance(self):
        command = details._modify_command(make_app())
        self.assertTrue(command.program.lower().endswith("msiexec.exe"))
        self.assertEqual(command.params, f"/i {CODE}")
        self.assertIsNone(command.blocked)

    def test_msiexec_switches_from_the_registry_are_never_reused(self):
        # Some MSI entries put the *uninstall* switch in ModifyPath.
        for raw in (f"MsiExec.exe /X{CODE}", f"msiexec /x {CODE} /qn", f"MsiExec.exe /I{CODE} REMOVE=ALL"):
            with self.subTest(raw=raw):
                command = details._modify_command(make_app(modify_path=raw))
                self.assertEqual(command.params, f"/i {CODE}")

    def test_the_vendor_program_runs_with_its_arguments(self):
        a = self.vendor("/modify EXCEL /dll OSETUP.DLL")
        command = details._modify_command(a)
        self.assertEqual(command.program, str(self.root / "Vendor" / "setup.exe"))
        self.assertEqual(command.params, "/modify EXCEL /dll OSETUP.DLL")
        self.assertTrue(details.can_modify(a))

    def test_an_unquoted_program_path_with_spaces(self):
        program = self.file("Two Words/setup.exe")
        a = make_app(product_code=None, is_msi=False, modify_path=f"{program} /modify")
        command = details._modify_command(a)
        self.assertEqual((command.program, command.params), (str(program), "/modify"))

    def test_when_modify_is_not_offered(self):
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        cases = {
            "vendor said no": self.vendor(no_modify=True),
            "store row": InstalledApp("WINGET\\X.Y", "XY", "1", "X", "user", "x64"),
            "no command and not msi": make_app(product_code=None, is_msi=False, modify_path="", uninstall_string="x.exe"),
            "msi not known to windows": None,
            "program missing": make_app(product_code=None, modify_path='"C:\\Nowhere\\setup.exe" /modify'),
            "network path": make_app(product_code=None, modify_path='"\\\\server\\share\\setup.exe" /modify'),
            "inside windows": make_app(product_code=None, modify_path=f'"{windows}\\System32\\cmd.exe" /c start x'),
            "not a program": make_app(product_code=None, modify_path=f'"{self.file("Vendor/readme.txt")}"'),
            "bare name": make_app(product_code=None, modify_path="rundll32.exe shell32.dll,Control_RunDLL"),
            "control character": self.vendor("/modify\r\ncalc.exe"),
        }
        for why, a in cases.items():
            with self.subTest(why):
                if a is None:
                    with mock.patch.object(inventory, "_msi_state", return_value=-1):
                        a = make_app()
                        self.assertFalse(details.can_modify(a))
                    continue
                self.assertFalse(details.can_modify(a))
                template, _ = details.modify_blocker(a)
                self.assertIn(template, i18n.FA)

    def test_modify_opens_the_program_and_returns_at_once(self):
        a = self.vendor("/modify")
        with mock.patch.object(details, "_open", return_value=0) as launch:
            result = details.modify(a)
        self.assertEqual(result, (True, "Opened the Modify window of {name}. Follow the steps there.", {"name": "Filmju"}))
        program = str(self.root / "Vendor" / "setup.exe")
        launch.assert_called_once_with(program, "/modify", str(self.root / "Vendor"))

    def test_modify_for_an_msi_product(self):
        with mock.patch.object(details, "_open", return_value=0) as launch:
            ok, *_ = details.modify(make_app())
        self.assertTrue(ok)
        file, params, _folder = launch.call_args.args
        self.assertTrue(file.lower().endswith("msiexec.exe"))
        self.assertEqual(params, f"/i {CODE}")

    def test_modify_errors(self):
        a = self.vendor()
        with mock.patch.object(details, "_open", return_value=1223):
            self.assertEqual(details.modify(a), (False, "Administrator permission (UAC) was declined.", {}))
        with mock.patch.object(details, "_open", return_value=2), self.assertLogs("upstream.details", "WARNING"):
            self.assertEqual(details.modify(a), (False, "Couldn't open the Modify window (Windows error {code}).", {"code": 2}))

    def test_nothing_is_launched_when_modify_is_blocked(self):
        with mock.patch.object(details, "_open") as launch:
            ok, template, values = details.modify(self.vendor(no_modify=True))
        self.assertFalse(ok)
        self.assertEqual(template, "{name} doesn't offer a Modify option.")
        launch.assert_not_called()

    def test_every_message_is_translated(self):
        a = self.vendor()
        for code in (0, 1223, 2):
            with mock.patch.object(details, "_open", return_value=code), self.assertLogs("upstream.details", "INFO"):
                self.assertIn(details.modify(a)[1], i18n.FA)

    def test_open_reports_the_windows_error(self):
        fake = SimpleNamespace(ShellExecuteExW=lambda info: 0)
        with mock.patch.object(installer, "_shell32", fake), mock.patch.object(details.ctypes, "get_last_error", return_value=1223):
            self.assertEqual(details._open("C:\\x.exe", "", ""), 1223)
        seen = {}

        def accept(info):
            seen.update(file=info._obj.lpFile, params=info._obj.lpParameters, mask=info._obj.fMask)
            return 1

        with mock.patch.object(installer, "_shell32", SimpleNamespace(ShellExecuteExW=accept)):
            self.assertEqual(details._open("C:\\x.exe", "/m", "C:\\"), 0)
        self.assertEqual((seen["file"], seen["params"]), ("C:\\x.exe", "/m"))
        self.assertFalse(seen["mask"] & 0x40)    # no SEE_MASK_NOCLOSEPROCESS: nothing is waited for


# --- the dialog --------------------------------------------------------------------------

class DeletedBeforeTheTimerTest(unittest.TestCase):
    """The first-paint timer is bound to the dialog: a dialog that is deleted before it fires starts nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(theme, "DATA_DIR", Path(self._tmp.name)),
                   mock.patch.object(inventory, "_msi_state", return_value=5),
                   mock.patch.object(details, "installer_family", return_value="msi"),
                   mock.patch.object(details, "signer_of", return_value=("Valid", "Filmju Ltd"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(app.setStyleSheet, "")
        self.thread = mock.patch.object(ui.threading, "Thread")
        self.starts = self.thread.start()
        self.addCleanup(self.thread.stop)

    def dialog(self) -> ui.DetailsDialog:
        return ui.DetailsDialog(AppStatus(make_app(size_kb=10698, install_date="20250628")))

    def test_a_dialog_that_stays_starts_its_work_after_the_first_event_loop_turn(self):
        dialog = self.dialog()
        self.addCleanup(dialog.close)
        self.starts.assert_not_called()
        app.processEvents()
        self.assertEqual(self.starts.call_count, 1)

    def test_a_dialog_deleted_first_starts_no_thread(self):
        dialog = self.dialog()
        shiboken6.delete(dialog)
        app.processEvents()
        app.processEvents()
        self.starts.assert_not_called()


class DialogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(theme, "DATA_DIR", Path(self._tmp.name)),     # theme glyphs are cached there
                   mock.patch.object(inventory, "_msi_state", return_value=5),
                   mock.patch.object(details, "signer_of", return_value=("Valid", "Filmju Ltd")),
                   mock.patch.object(details, "installer_family", return_value="msi"),
                   mock.patch.object(ui.QDesktopServices, "openUrl", return_value=True),
                   mock.patch.object(ui.QTimer, "singleShot")]    # a dialog starts its work only when a test says so
        self.signer, self.family = patches[2].start(), patches[3].start()
        self.opened, self.timer = patches[4].start(), patches[5].start()
        for patch in (patches[0], patches[1]):
            patch.start()
        for patch in patches:
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._reset_app)
        self.dialogs: list[ui.DetailsDialog] = []

    @staticmethod
    def _reset_app():
        apply_language(app, "en")
        app.setStyleSheet("")
        app.setPalette(QPalette())
        theme._current = theme.THEMES["paper"]

    def make(self, a: InstalledApp | None = None, update: Update | None = None, **kwargs) -> ui.DetailsDialog:
        status = AppStatus(a or make_app(size_kb=10698, install_date="20250628"), repo="owner/project",
                           winget_id="Filmju.Filmju", update=update)
        dialog = ui.DetailsDialog(status, **kwargs)
        self.addCleanup(dialog.close)
        self.dialogs.append(dialog)
        return dialog

    @staticmethod
    def finish(dialog: ui.DetailsDialog):
        done = []
        dialog.ready.connect(lambda: done.append(1))
        dialog.start_background()
        assert wait_for(lambda: bool(done)), "the background work did not finish"

    @staticmethod
    def texts(dialog: ui.DetailsDialog) -> dict[str, str]:
        return {key: cell.full_text for key, cell in dialog._cells.items()}

    def test_it_opens_from_what_the_scan_holds_without_touching_the_slow_parts(self):
        a = make_app(size_kb=0, install_date="")
        with tempfile.TemporaryDirectory() as folder:
            a.install_location = folder
            dialog = self.make(a)
            self.signer.assert_not_called()
            self.family.assert_not_called()
            shown = self.texts(dialog)
        self.assertEqual(shown["signer"], "Checking…")
        self.assertEqual(shown["size"], "Calculating…")
        self.assertEqual((shown["arch"], shown["scope"], shown["date"]), ("32-bit", "All users", "Unknown"))
        self.assertEqual(dialog.windowTitle(), "Details — Filmju")
        self.assertEqual(dialog.subtitle.full_text, "Version 1.0.1 · Filmju Ltd")

    def test_background_results_fill_in(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "a.bin").write_bytes(b"x" * 3000)
            dialog = self.make(make_app(size_kb=0, install_location=folder, install_kind=""))
            self.assertEqual(self.texts(dialog)["family"], "Checking…")
            self.finish(dialog)
            shown = self.texts(dialog)
        self.assertEqual((shown["family"], shown["size"], shown["signer"]),
                         ("Windows Installer (MSI)", "2.9 KB", "Signed by Filmju Ltd"))
        self.signer.assert_called_once()

    def test_the_background_work_is_scheduled_after_the_first_paint(self):
        dialog = self.make()
        self.timer.assert_called_once_with(0, dialog, dialog.start_background)

    def test_the_background_work_starts_once(self):
        dialog = self.make()
        self.finish(dialog)
        dialog.start_background()
        app.processEvents()
        self.assertEqual(self.signer.call_count, 1)

    def test_a_walk_cut_short_says_at_least(self):
        with mock.patch.object(details, "folder_size", return_value=(5 * 1024 ** 2, False)), \
                tempfile.TemporaryDirectory() as folder:
            dialog = self.make(make_app(size_kb=0, install_location=folder))
            self.finish(dialog)
        self.assertEqual(self.texts(dialog)["size"], i18n.tr("At least {size}", size="5.0 MB"))

    def test_signature_states(self):
        cases = {
            ("NotSigned", ""): ("Not signed", ""),
            ("HashMismatch", ""): ("The signature is broken: the file was changed after it was signed", "danger"),
            ("NotTrusted", "Shady Ltd"): ("Signed by {signer}, but not trusted by Windows", "danger"),
            ("UnknownError", ""): ("Windows doesn't trust the signature", "danger"),
            ("NoFile", ""): ("No program file to check", "muted"),
            ("Unknown", ""): ("Couldn't check the signature", "muted"),
            ("Valid", ""): ("Couldn't check the signature", "muted"),
        }
        for answer, (template, tone) in cases.items():
            with self.subTest(answer=answer):
                self.signer.return_value = answer
                dialog = self.make()
                self.finish(dialog)
                cell = dialog._cells["signer"]
                self.assertEqual(cell.full_text, i18n.tr(template, signer=answer[1]) if "{signer}" in template else template)
                self.assertEqual(cell.property("tone") or "", tone)

    def test_a_crash_in_the_background_work_still_finishes(self):
        self.signer.side_effect = RuntimeError("boom")
        dialog = self.make()
        with self.assertLogs("upstream.ui.details", "ERROR"):
            self.finish(dialog)
        self.assertEqual(self.texts(dialog)["signer"], "Couldn't check the signature")

    def test_update_information(self):
        notes = "## 2.0\n- fixed CVE-2026-1\n" * 3
        upd = Update("github", "2.0.0", "owner/project", notes=notes, page_url="https://github.com/owner/project/releases/tag/v2")
        dialog = self.make(update=upd)
        shown = self.texts(dialog)
        self.assertEqual((shown["installed"], shown["latest"]), ("1.0.1", "2.0.0"))
        self.assertEqual(shown["source"], "GitHub — owner/project")
        self.assertNotIn("repo", shown)            # the update's own source already says it
        self.assertEqual(shown["winget"], "Filmju.Filmju")
        self.assertEqual(dialog._cells["latest"].property("tone"), "accent")
        box = dialog.scroll.widget().findChildren(QPlainTextEdit)[0]
        self.assertEqual(box.toPlainText(), notes.strip())
        self.assertTrue(box.isReadOnly())

    def test_without_an_update(self):
        shown = self.texts(self.make())
        self.assertEqual(shown["latest"], "Up to date")
        self.assertEqual((shown["repo"], shown["winget"]), ("owner/project", "Filmju.Filmju"))
        bare = ui.DetailsDialog(AppStatus(make_app()))
        self.addCleanup(bare.close)
        self.assertEqual(self.texts(bare)["latest"], "No source found")
        noted = AppStatus(make_app(), repo="owner/project")
        noted.set_note("Held at {version}", version="1.0.1")
        held = ui.DetailsDialog(noted)
        self.addCleanup(held.close)
        self.assertEqual(self.texts(held)["latest"], "Held at 1.0.1")

    def test_long_notes_are_cut(self):
        dialog = self.make(update=Update("winget", "2", "Filmju.Filmju", notes="x" * 10_000))
        box = dialog.scroll.widget().findChildren(QPlainTextEdit)[0]
        self.assertEqual(len(box.toPlainText()), ui.NOTES_LIMIT)

    def test_links_open_web_addresses_only(self):
        dialog = self.make(make_app(urls=["https://example.com", "mailto:x@example.com"]))
        labels = [w for w in dialog.scroll.widget().findChildren(QLabel) if w.textFormat().name == "RichText"]
        self.assertEqual([w.toolTip() for w in labels], ["https://github.com/owner/project", "https://example.com"])
        labels[1].linkActivated.emit("https://example.com")
        self.opened.assert_called_once()
        self.opened.reset_mock()
        for bad in ("javascript:alert(1)", "file:///C:/Windows/System32/calc.exe", "ms-settings:", ""):
            dialog._open_link(bad)
        self.opened.assert_not_called()

    def test_location_and_uninstall_command(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = self.make(make_app(install_location=folder, uninstall_string='"C:\\A\\unins000.exe" /SILENT'))
            edits = {e.text() for e in dialog.scroll.widget().findChildren(ui.QLineEdit)}
            self.assertEqual(edits, {folder, '"C:\\A\\unins000.exe" /SILENT'})
            self.assertTrue(all(e.isReadOnly() for e in dialog.scroll.widget().findChildren(ui.QLineEdit)))
            dialog._open_folder()
            self.assertEqual(self.opened.call_args.args[0].toLocalFile().replace("/", "\\"), folder)
        plain = self.make(make_app(install_location="", uninstall_string="", quiet_uninstall_string=""))
        self.assertEqual(plain.scroll.widget().findChildren(ui.QLineEdit), [])

    def test_copy_details_and_copy_command(self):
        upd = Update("github", "2.0.0", "owner/project", notes="Fixed a thing", page_url="https://example.com/r")
        dialog = self.make(update=upd)
        self.finish(dialog)
        dialog._copy_details()
        text = QGuiApplication.clipboard().text()
        for needle in ("Filmju 1.0.1", "Filmju Ltd", "Architecture: 32-bit", "Installer: Windows Installer (MSI)",
                       "Installed on: 2025-06-28", "Size on disk: 10.4 MB", "Signature: Signed by Filmju Ltd",
                       "Latest version: 2.0.0", "Release notes:", "Fixed a thing", "https://example.com/r",
                       f"MsiExec.exe /X{CODE}"):
            self.assertIn(needle, text)
        self.assertEqual(dialog.status_label.text(), "Copied to the clipboard.")
        QGuiApplication.clipboard().setText("")
        dialog._copy_command("x.exe /u")
        self.assertEqual(QGuiApplication.clipboard().text(), "x.exe /u")

    # -- repair and modify buttons -----------------------------------------------------------

    def test_buttons_follow_what_the_app_can_do(self):
        ok = self.make()
        self.assertTrue(ok.repair_btn.isEnabled() and ok.modify_btn.isEnabled())
        self.assertEqual((ok.repair_btn.toolTip(), ok.modify_btn.toolTip()), ("", ""))
        inno = self.make(make_app(product_code=None, is_msi=False, install_kind="inno", modify_path="",
                                  uninstall_string="x.exe"))
        self.assertFalse(inno.repair_btn.isEnabled() or inno.modify_btn.isEnabled())
        self.assertEqual(inno.repair_btn.toolTip(), i18n.tr("Only apps installed with Windows Installer (MSI) can be repaired from here."))
        self.assertEqual(inno.modify_btn.toolTip(), i18n.tr("{name} doesn't offer a Modify option.", name="Filmju"))
        self.assertTrue(inno.copy_btn.isEnabled() and inno.close_btn.isEnabled())

    def test_repair_asks_first(self):
        dialog = self.make()
        with mock.patch.object(dialog, "_confirm_repair", return_value=False) as ask, \
                mock.patch.object(details, "repair") as repair:
            dialog._on_repair()
        ask.assert_called_once()
        repair.assert_not_called()
        self.assertEqual(dialog._busy, "")

    def test_repair_runs_on_a_thread_and_reports(self):
        started, release = threading.Event(), threading.Event()
        seen = {}

        def slow(a, progress, cancel=None):
            seen.update(thread=threading.current_thread(), cancel=cancel)
            progress("Repairing {name}…", name=a.name)
            started.set()
            release.wait(10)
            return True, "{name} was repaired.", {"name": a.name}

        dialog = self.make()
        dialog.show()
        with mock.patch.object(dialog, "_confirm_repair", return_value=True), mock.patch.object(details, "repair", slow):
            dialog._on_repair()
            self.assertTrue(started.wait(10))
            wait_for(lambda: dialog.status_label.text() == "Repairing Filmju…")
            self.assertEqual(dialog._busy, "repair")
            self.assertFalse(dialog.repair_btn.isEnabled() or dialog.modify_btn.isEnabled())
            self.assertTrue(dialog.bar.isVisibleTo(dialog) and dialog.stop_btn.isVisibleTo(dialog))
            dialog._on_repair()                                  # a second click is ignored
            release.set()
            self.assertTrue(wait_for(lambda: not dialog._busy))
        self.assertIsNot(seen["thread"], threading.main_thread())
        self.assertIsInstance(seen["cancel"], CancelToken)
        self.assertEqual(dialog.status_label.text(), "Filmju was repaired.")
        self.assertEqual(dialog.status_label.property("tone"), "accent")
        self.assertTrue(dialog.repair_btn.isEnabled() and dialog.modify_btn.isEnabled())
        self.assertFalse(dialog.bar.isVisibleTo(dialog))

    def test_a_failed_repair_is_an_error_and_a_cancelled_one_is_not(self):
        dialog = self.make()
        for template, values, tone in (("Installer exited with code {code}", {"code": 1603}, "danger"),
                                       ("The repair was cancelled.", {}, "muted"),
                                       ("Stopped waiting for the installer. It may still be running.", {}, "muted")):
            with self.subTest(template=template):
                dialog._on_action_done("repair", False, template, values)
                self.assertEqual(dialog.status_label.property("tone"), tone)
                self.assertEqual(dialog.status_label.text(), i18n.render(template, values))

    def test_stop_waiting_cancels_the_token_and_closing_does_too(self):
        tokens = []

        def wait(a, progress, cancel=None):
            tokens.append(cancel)
            cancel.wait(10)
            return False, "Stopped waiting for the installer. It may still be running.", {}

        dialog = self.make()
        dialog.show()
        with mock.patch.object(dialog, "_confirm_repair", return_value=True), mock.patch.object(details, "repair", wait):
            dialog._on_repair()
            self.assertTrue(wait_for(lambda: bool(tokens)))
            dialog.stop_btn.click()
            self.assertTrue(tokens[0].is_set)
            self.assertTrue(wait_for(lambda: not dialog._busy))
        self.assertEqual(dialog.status_label.property("tone"), "muted")

        again = self.make()
        again.show()
        with mock.patch.object(again, "_confirm_repair", return_value=True), mock.patch.object(details, "repair", wait):
            again._on_repair()
            self.assertTrue(wait_for(lambda: len(tokens) == 2))
            again.close()
            self.assertTrue(tokens[1].is_set)
            self.assertTrue(again._stop.is_set)

    def test_a_repair_that_raises_is_reported(self):
        dialog = self.make()
        with mock.patch.object(dialog, "_confirm_repair", return_value=True), \
                mock.patch.object(details, "repair", side_effect=RuntimeError("boom")), \
                self.assertLogs("upstream.ui.details", "ERROR"):
            dialog._on_repair()
            self.assertTrue(wait_for(lambda: not dialog._busy))
        self.assertEqual(dialog.status_label.text(), "boom")
        self.assertEqual(dialog.status_label.property("tone"), "danger")

    def test_modify_runs_on_a_thread(self):
        seen = {}

        def fake(a):
            seen["thread"] = threading.current_thread()
            return True, "Opened the Modify window of {name}. Follow the steps there.", {"name": a.name}

        dialog = self.make()
        with mock.patch.object(details, "modify", fake):
            dialog._on_modify()
            self.assertTrue(wait_for(lambda: not dialog._busy and bool(seen)))
        self.assertIsNot(seen["thread"], threading.main_thread())
        self.assertEqual(dialog.status_label.text(), "Opened the Modify window of Filmju. Follow the steps there.")

    def test_nothing_runs_while_updates_are_running(self):
        dialog = self.make(updates_running=lambda: True)
        with mock.patch.object(details, "repair") as repair, mock.patch.object(details, "modify") as modify, \
                mock.patch.object(dialog, "_confirm_repair", return_value=True) as ask:
            dialog._on_repair()
            dialog._on_modify()
        repair.assert_not_called()
        modify.assert_not_called()
        ask.assert_not_called()
        self.assertEqual(dialog.status_label.text(), "Updates are running right now. Wait until they finish, then try again.")

    def test_a_blocked_action_is_not_run_even_if_the_slot_is_called(self):
        dialog = self.make(make_app(no_repair=True, no_modify=True))
        with mock.patch.object(details, "repair") as repair, mock.patch.object(details, "modify") as modify:
            dialog._on_repair()
            dialog._on_modify()
        repair.assert_not_called()
        modify.assert_not_called()

    def test_the_confirmation_names_the_app_and_defaults_to_cancel(self):
        dialog = self.make()
        shown = {}

        def exec_(box):
            shown.update(title=box.windowTitle(), text=box.text(), default=box.defaultButton().text(),
                         buttons=[b.text() for b in box.buttons()])
            box.defaultButton().click()
            return 0

        with mock.patch.object(ui.QMessageBox, "exec", exec_):
            self.assertFalse(dialog._confirm_repair())
        self.assertEqual(shown["title"], "Repair Filmju")
        self.assertIn("Filmju", shown["text"])
        self.assertEqual(shown["default"], "Cancel")
        self.assertEqual(shown["buttons"], ["Repair", "Cancel"])

    # -- language, direction and theme ---------------------------------------------------------

    def test_persian_rendering_and_live_switch(self):
        fa = i18n.FA
        dialog = self.make(update=Update("github", "2.0.0", "owner/project", notes="n"))
        self.finish(dialog)
        apply_language(app, "fa")
        dialog.retranslate()
        self.assertEqual(dialog.windowTitle(), i18n.tr("Details — {name}", name="Filmju"))
        self.assertEqual((dialog.repair_btn.text(), dialog.modify_btn.text(), dialog.copy_btn.text(), dialog.close_btn.text()),
                         (fa["Repair"], fa["Modify"], fa["Copy details"], fa["Close"]))
        shown = self.texts(dialog)
        self.assertEqual(shown["arch"], fa["32-bit"])
        self.assertEqual(shown["scope"], fa["All users"])
        self.assertEqual(shown["date"], i18n.num("2025-06-28"))
        self.assertEqual(shown["size"], i18n.num("10.4 MB"))
        self.assertTrue(shown["signer"].startswith(fa["Signed by {signer}"].split("{")[0]))
        headings = [w.text() for w in dialog.scroll.widget().findChildren(QLabel) if w.objectName() == "section"]
        self.assertEqual(headings, [fa["General"], fa["Location"], fa["Updates"], fa["Links"], fa["Uninstall command"]])
        apply_language(app, "en")
        dialog.retranslate()
        self.assertEqual(dialog.repair_btn.text(), "Repair")
        self.assertEqual(self.texts(dialog)["arch"], "32-bit")

    def test_a_language_change_event_retranslates(self):
        dialog = self.make()
        apply_language(app, "fa")
        QApplication.sendEvent(dialog, QEvent(QEvent.LanguageChange))
        self.assertEqual(dialog.repair_btn.text(), i18n.FA["Repair"])

    def test_the_status_line_is_translated_when_the_language_changes(self):
        dialog = self.make()
        dialog._on_action_done("repair", True, "{name} was repaired.", {"name": "Filmju"})
        apply_language(app, "fa")
        dialog.retranslate()
        self.assertEqual(dialog.status_label.text(), i18n.render("{name} was repaired.", {"name": "Filmju"}))
        self.assertNotEqual(dialog.status_label.text(), "Filmju was repaired.")

    def test_rtl_puts_labels_on_the_right(self):
        for lang, label_is_left in (("en", True), ("fa", False)):
            with self.subTest(lang=lang):
                apply_language(app, lang)
                dialog = self.make()
                dialog.show()
                app.processEvents()
                label = next(w for w in dialog.scroll.widget().findChildren(QLabel) if w.text() == i18n.tr("Architecture"))
                cell = dialog._cells["arch"]
                left_of = label.mapTo(dialog, label.rect().topLeft()).x() < cell.mapTo(dialog, cell.rect().topLeft()).x()
                self.assertEqual(left_of, label_is_left)
                foot = [b.mapTo(dialog, b.rect().topLeft()).x() for b in (dialog.repair_btn, dialog.close_btn)]
                self.assertEqual(foot[0] < foot[1], label_is_left)     # Close sits at the far end of the footer
                dialog.close()

    def test_every_theme_renders(self):
        for key in theme.THEME_ORDER:
            with self.subTest(theme=key):
                theme.apply_theme(app, key)
                dialog = self.make(update=Update("github", "2.0.0", "owner/project", notes="n"))
                dialog.show()
                app.processEvents()
                shot = dialog.grab()
                self.assertFalse(shot.isNull())
                self.assertGreater(shot.width(), 500)
                dialog.close()

    def test_a_theme_switch_reaches_an_open_dialog(self):
        theme.apply_theme(app, "paper")
        dialog = self.make(update=Update("github", "2.0.0", "owner/project"))
        dialog.show()
        app.processEvents()
        rule = dialog.scroll.widget().findChildren(ui._Rule)[0]
        light = rule.grab().toImage().pixelColor(0, 0).name()
        theme.apply_theme(app, "graphite")
        app.processEvents()
        dark = rule.grab().toImage().pixelColor(0, 0).name()
        self.assertEqual((light, dark), (theme.THEMES["paper"]["line"].lower(), theme.THEMES["graphite"]["line"].lower()))
        self.assertEqual(dialog._cells["latest"].palette().color(QPalette.WindowText).name(),
                         theme.THEMES["graphite"]["accent"].lower())

    def test_the_dialog_fits_the_screen(self):
        dialog = self.make(update=Update("github", "2.0.0", "owner/project", notes="n" * 3000))
        screen = dialog.screen().availableGeometry().height()
        self.assertLessEqual(dialog.height(), screen)
        self.assertGreaterEqual(dialog.width(), ui.WIDTH)

    def test_the_status_strip_gives_its_height_back(self):
        dialog = self.make()
        dialog.show()
        app.processEvents()
        before = dialog.height()
        dialog._say_status("Copied to the clipboard.", tone="accent")
        app.processEvents()
        self.assertGreater(dialog.height(), before)
        dialog._say_status("")
        app.processEvents()
        self.assertEqual(dialog.height(), before)

    def test_the_status_strip_never_shrinks_a_dialog_the_user_made_taller(self):
        dialog = self.make()
        dialog.show()
        dialog.resize(dialog.width(), dialog.screen().availableGeometry().height() + 100)
        app.processEvents()
        tall = dialog.height()
        dialog._say_status("Copied to the clipboard.", tone="accent")
        app.processEvents()
        self.assertGreaterEqual(dialog.height(), tall)

    def test_an_icon_can_be_supplied(self):
        pixmap = theme.letter_avatar("Q", 44)
        dialog = self.make(icon=pixmap)
        self.assertIs(dialog._icon, pixmap)
        self.assertFalse(dialog.icon_label.pixmap().isNull())
        self.assertIsNot(self.make()._icon, pixmap)     # without one it draws the app's letter


if __name__ == "__main__":
    unittest.main()
