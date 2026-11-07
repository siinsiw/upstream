"""In-place install detection: installer framework of the installed copy, and the plan that keeps an update
in that copy. Nothing here writes the registry or runs an installer; MSI packages are small databases built
in a temp folder."""
from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from upstream import inplace, installer, inventory
from upstream.i18n import FA
from upstream.inventory import InstalledApp

OLD = "{AAAAAAAA-0000-4000-8000-000000000001}"
NEW = "{BBBBBBBB-0000-4000-8000-000000000002}"
UPGRADE = "{CCCCCCCC-0000-4000-8000-000000000003}"
OTHER_UPGRADE = "{DDDDDDDD-0000-4000-8000-000000000004}"


def make_app(kind="", **over) -> InstalledApp:
    fields = dict(reg_key=r"HKLM64\Demo", name="Demo", version="1.0", publisher="Demo Inc", scope="machine",
                  arch="x64", install_kind=kind)
    fields.update(over)
    return InstalledApp(**fields)


def touch(path: Path, data: bytes = b"MZ" + bytes(64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_msi(path: Path, product: str, upgrade: str = "", allusers: str = "", upgrade_rows=()) -> Path:
    """A minimal .msi: a Property table, and an Upgrade table when rows are given [(code, attributes)]."""
    lib = ctypes.WinDLL("msi")
    handle = ctypes.POINTER(wintypes.DWORD)
    lib.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, handle]
    lib.MsiDatabaseOpenViewW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, handle]
    lib.MsiViewExecute.argtypes = [wintypes.DWORD, wintypes.DWORD]
    lib.MsiDatabaseCommit.argtypes = [wintypes.DWORD]
    lib.MsiCloseHandle.argtypes = [wintypes.DWORD]
    db = wintypes.DWORD()
    assert lib.MsiOpenDatabaseW(str(path), 4, ctypes.byref(db)) == 0     # MSIDBOPEN_CREATEDIRECT

    def run(sql: str):
        view = wintypes.DWORD()
        assert lib.MsiDatabaseOpenViewW(db, sql, ctypes.byref(view)) == 0, sql
        assert lib.MsiViewExecute(view, 0) == 0, sql
        lib.MsiCloseHandle(view)

    try:
        run("CREATE TABLE `Property` (`Property` CHAR(72) NOT NULL, `Value` LONGCHAR NOT NULL PRIMARY KEY `Property`)")
        props = {"ProductCode": product, "UpgradeCode": upgrade, "ALLUSERS": allusers, "ProductVersion": "2.0.0"}
        for name, value in props.items():
            if value:
                run(f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('{name}', '{value}')")
        if upgrade_rows:
            run("CREATE TABLE `Upgrade` (`UpgradeCode` CHAR(38) NOT NULL, `VersionMin` CHAR(20), `VersionMax` CHAR(20), "
                "`Language` CHAR(255), `Attributes` LONG NOT NULL, `Remove` CHAR(255), `ActionProperty` CHAR(72) NOT NULL "
                "PRIMARY KEY `UpgradeCode`, `VersionMin`, `VersionMax`, `Language`, `Attributes`)")
            for code, attrs in upgrade_rows:
                run("INSERT INTO `Upgrade` (`UpgradeCode`, `VersionMin`, `VersionMax`, `Language`, `Attributes`, "
                    f"`Remove`, `ActionProperty`) VALUES ('{code}', '', '9.9.9', '', {attrs}, '', 'OLDER')")
        assert lib.MsiDatabaseCommit(db) == 0
    finally:
        lib.MsiCloseHandle(db)
    return path


class FakeKey:
    def __init__(self, values: dict):
        self.values = values


def fake_query(key, name):
    if name in key.values:
        return key.values[name], 1
    raise FileNotFoundError(name)


class RegistryEvidenceTest(unittest.TestCase):
    """inventory._read_entry fills install_kind from the Uninstall key alone."""

    def kind_of(self, subkey: str, **values) -> str:
        values.setdefault("DisplayName", "Demo")
        with mock.patch.object(inventory.winreg, "QueryValueEx", fake_query):
            app = inventory._read_entry(FakeKey(values), f"HKLM64\\{subkey}", "machine", "x64")
        return app.install_kind

    def test_inno(self):
        self.assertEqual(self.kind_of("Audacity_is1"), "inno")
        self.assertEqual(self.kind_of(f"{NEW}_is1"), "inno")
        self.assertEqual(self.kind_of("Demo", UninstallString='"C:\\Apps\\Demo\\unins000.exe"'), "inno")
        self.assertEqual(self.kind_of("Demo", **{"Inno Setup: Setup Version": "6.2.2"}), "inno")

    def test_msi(self):
        flagged = dict(WindowsInstaller=1, UninstallString=f"MsiExec.exe /X{NEW}")
        self.assertEqual(self.kind_of(NEW, **flagged), "msi")
        self.assertEqual(self.kind_of(NEW, UninstallString=f"MsiExec.exe /I{NEW}"), "msi")

    def test_guid_key_with_another_uninstaller_is_not_guessed(self):
        self.assertEqual(self.kind_of(NEW, UninstallString='"C:\\Apps\\Demo\\uninstall.exe"'), "")
        self.assertEqual(self.kind_of(NEW), "")

    def test_burn_bundle(self):
        cache = f'"C:\\ProgramData\\Package Cache\\{NEW}\\setup.exe" /uninstall'
        self.assertEqual(self.kind_of(NEW, BundleProviderKey="Demo.Runtime", UninstallString=cache), "burn")
        self.assertEqual(self.kind_of("Demo", BundleCachePath="C:\\x\\setup.exe"), "burn")

    def test_installshield(self):
        self.assertEqual(self.kind_of(f"InstallShield_{NEW}"), "installshield")
        path = f'"C:\\Program Files (x86)\\InstallShield Installation Information\\{NEW}\\setup.exe" -runfromtemp -removeonly'
        self.assertEqual(self.kind_of("Demo", UninstallString=path), "installshield")

    def test_squirrel(self):
        self.assertEqual(self.kind_of("Discord", UninstallString="C:\\Users\\a\\AppData\\Local\\Discord\\Update.exe --uninstall"),
                         "squirrel")

    def test_unknown_when_the_key_is_silent(self):
        self.assertEqual(self.kind_of("BleachBit", UninstallString='"C:\\Program Files\\BleachBit\\uninstall.exe" /allusers'), "")
        self.assertEqual(self.kind_of("Chrome", UninstallString='"C:\\Chrome\\setup.exe" --uninstall'), "")

    def test_other_fields_are_unchanged(self):
        with mock.patch.object(inventory.winreg, "QueryValueEx", fake_query):
            app = inventory._read_entry(FakeKey({"DisplayName": "Demo", "WindowsInstaller": 1}), f"HKLM64\\{NEW}",
                                        "machine", "x64")
        self.assertTrue(app.is_msi)
        self.assertEqual(app.product_code, NEW)


class InstalledKindTest(unittest.TestCase):
    def setUp(self):
        inplace._sniffed.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def app_with(self, exe: Path, **over) -> InstalledApp:
        return make_app(uninstall_string=f'"{exe}" /S', **over)

    def test_registry_evidence_wins(self):
        self.assertEqual(inplace.installed_kind(make_app("inno")), "inno")

    def test_store_rows_are_msix(self):
        self.assertEqual(inplace.installed_kind(make_app(reg_key=r"WINGET\Microsoft.WindowsTerminal")), "msix")

    def test_nsis_uninstaller_is_recognised_from_the_file(self):
        exe = touch(self.root / "Demo" / "uninst.exe", b"MZ" + bytes(100) + b"Nullsoft.NSIS" + bytes(50))
        self.assertEqual(inplace.installed_kind(self.app_with(exe)), "nullsoft")

    def test_other_frameworks_are_recognised_too(self):
        exe = touch(self.root / "Package Cache" / "setup.exe", b"MZ" + bytes(100) + b".wixburn" + bytes(50))
        self.assertEqual(inplace.installed_kind(self.app_with(exe)), "burn")

    def test_generic_exe_is_unknown(self):
        exe = touch(self.root / "uninstall.exe")
        self.assertEqual(inplace.installed_kind(self.app_with(exe)), "")

    def test_only_uninstaller_looking_files_are_read(self):
        exe = touch(self.root / "demo.exe", b"MZ" + bytes(100) + b"Nullsoft.NSIS")
        with mock.patch.object(inplace.installer, "detect") as detect:
            self.assertEqual(inplace.installed_kind(self.app_with(exe)), "")
        detect.assert_not_called()

    def test_missing_or_broken_files_are_unknown(self):
        self.assertEqual(inplace.installed_kind(make_app()), "")
        self.assertEqual(inplace.installed_kind(self.app_with(self.root / "uninst.exe")), "")
        text = touch(self.root / "unins000.exe", b"not a program")
        self.assertEqual(inplace.installed_kind(self.app_with(text)), "")

    def test_msiexec_is_not_an_uninstaller(self):
        app = make_app(uninstall_string=f"MsiExec.exe /X{NEW}")
        self.assertEqual(inplace.installed_kind(app), "")

    def test_result_is_cached_until_the_file_changes(self):
        exe = touch(self.root / "uninst.exe", b"MZ" + bytes(100) + b"Nullsoft.NSIS")
        app = self.app_with(exe)
        with mock.patch.object(inplace.installer, "detect", wraps=installer.detect) as detect:
            self.assertEqual(inplace.installed_kind(app), "nullsoft")
            self.assertEqual(inplace.installed_kind(app), "nullsoft")
            self.assertEqual(detect.call_count, 1)
            touch(exe, b"MZ" + bytes(300) + b"Inno Setup")
            self.assertEqual(inplace.installed_kind(app), "inno")
            self.assertEqual(detect.call_count, 2)


class MismatchTest(unittest.TestCase):
    def outcome(self, installed: str, new: str):
        plan = inplace.plan(make_app(installed), new, Path("setup.exe"))
        return "refuse" if plan.refuse else "warn" if plan.warnings else "none", plan

    def test_table(self):
        cases = [
            ("inno", "nullsoft", "refuse"), ("nullsoft", "inno", "refuse"), ("msi", "inno", "refuse"),
            ("inno", "msi", "refuse"), ("burn", "msi", "refuse"), ("installshield", "burn", "refuse"),
            ("msix", "inno", "refuse"), ("inno", "msix", "refuse"), ("squirrel", "nullsoft", "refuse"),
            ("msi", "burn", "warn"), ("msi", "installshield", "warn"), ("msi", "advancedinstaller", "warn"),
            ("inno", "exe", "none"), ("msix", "exe", "none"), ("", "inno", "none"), ("", "", "none"),
            ("inno", "", "none"), ("burn", "burn", "none"), ("squirrel", "squirrel", "none"),
            ("installshield", "installshield", "none"),
        ]
        for installed, new, expected in cases:
            with self.subTest(installed=installed, new=new):
                self.assertEqual(self.outcome(installed, new)[0], expected)

    def test_refusal_names_both_frameworks_and_is_translated(self):
        _, plan = self.outcome("inno", "nullsoft")
        template, values = plan.refuse
        self.assertEqual(values, {"name": "Demo", "installed": "Inno Setup", "new": "NSIS"})
        self.assertIn("Inno Setup", template.format(**values))
        self.assertIn(template, FA)

    def test_a_refusal_changes_nothing_else(self):
        _, plan = self.outcome("msi", "inno")
        self.assertEqual(plan.extra_args, "")


class FolderPlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.folder = self.root / "My Apps" / "Demo"
        self.uninstaller = touch(self.folder / "unins000.exe")
        # Temp folders count as protected for the real guard; the app folder here is not meant to be one.
        patch = mock.patch.object(inplace.guard, "protected_path", lambda p: None)
        patch.start()
        self.addCleanup(patch.stop)

    def app(self, kind: str, **over) -> InstalledApp:
        fields = dict(install_location=str(self.folder), uninstall_string=f'"{self.uninstaller}"')
        fields.update(over)
        return make_app(kind, **fields)

    def plan(self, kind: str, **over):
        return inplace.plan(self.app(kind, **over), kind, self.root / "setup.exe")

    def test_inno_gets_a_quoted_dir(self):
        plan = self.plan("inno")
        self.assertEqual(plan.extra_args, f'/DIR="{self.folder}"')
        self.assertIsNone(plan.refuse)
        self.assertEqual(plan.warnings, [])

    def test_nsis_gets_an_unquoted_d_switch(self):
        plan = self.plan("nullsoft")
        self.assertEqual(plan.extra_args, f"/D={self.folder}")
        self.assertNotIn('"', plan.extra_args)
        self.assertIn(" ", plan.extra_args.split("=", 1)[1])      # the folder has a space in it

    def test_nsis_switch_ends_the_command_line(self):
        plan = self.plan("nullsoft")
        with mock.patch.object(installer, "_shell_run", return_value=0) as run:
            installer.install(self.root / "setup.exe", "nullsoft", "machine", plan)
        self.assertTrue(run.call_args.args[1].endswith(plan.extra_args))

    def test_trailing_backslash_is_dropped(self):
        self.assertEqual(self.plan("inno", install_location=f"{self.folder}\\").extra_args, f'/DIR="{self.folder}"')

    def test_folder_comes_from_the_uninstaller_when_the_key_has_none(self):
        self.assertEqual(self.plan("inno", install_location="").extra_args, f'/DIR="{self.folder}"')

    def test_icon_inside_the_folder_is_enough(self):
        elsewhere = touch(self.root / "elsewhere" / "unins000.exe")
        icon = touch(self.folder / "demo.exe")
        plan = self.plan("inno", uninstall_string=f'"{elsewhere}"', icon_path=str(icon))
        self.assertEqual(plan.extra_args, f'/DIR="{self.folder}"')

    def test_no_switch_when_nothing_proves_the_folder_belongs_to_the_app(self):
        elsewhere = touch(self.root / "elsewhere" / "unins000.exe")
        self.assertEqual(self.plan("inno", uninstall_string=f'"{elsewhere}"').extra_args, "")

    def test_no_switch_for_a_missing_folder(self):
        self.assertEqual(self.plan("inno", install_location=str(self.root / "gone")).extra_args, "")

    def test_no_switch_without_a_known_folder(self):
        self.assertEqual(self.plan("inno", install_location="", uninstall_string="").extra_args, "")

    def test_other_frameworks_get_no_folder_switch(self):
        for kind in ("burn", "installshield", "advancedinstaller", "squirrel", "msix"):
            with self.subTest(kind=kind):
                self.assertEqual(self.plan(kind).extra_args, "")

    def test_installer_that_is_generic_is_left_alone(self):
        plan = inplace.plan(self.app("inno"), "exe", self.root / "setup.exe")
        self.assertEqual((plan.extra_args, plan.refuse, plan.warnings), ("", None, []))


class ProtectedFolderTest(unittest.TestCase):
    """The real guard, unpatched: a container folder is never handed to an installer as the app folder."""

    def test_containers_are_never_used(self):
        drive = os.path.splitdrive(os.environ["SystemRoot"])[0] + "\\"
        for folder in (os.environ["ProgramFiles"], os.environ["SystemRoot"], drive, os.environ["USERPROFILE"]):
            with self.subTest(folder=folder):
                self.assertTrue(os.path.isdir(folder))
                app = make_app("inno", install_location=folder)
                self.assertEqual(inplace.plan(app, "inno", Path("setup.exe")).extra_args, "")


class MsiPlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.installed = {}     # upgrade code -> product codes Windows Installer has under it

    def plan(self, package: Path, app: InstalledApp | None = None):
        app = app or make_app("msi", reg_key=f"HKLM64\\{OLD}", product_code=OLD, is_msi=True)
        with mock.patch.object(inplace, "_related_products", lambda code: self.installed.get(code, set())):
            return inplace.plan(app, "msi", package)

    def package(self, name="new.msi", **kwargs) -> Path:
        return make_msi(self.root / name, **kwargs)

    def test_same_upgrade_code_is_an_in_place_upgrade(self):
        self.installed[UPGRADE] = {OLD}
        plan = self.plan(self.package(product=NEW, upgrade=UPGRADE, allusers="1"))
        self.assertEqual((plan.extra_args, plan.refuse, plan.warnings), ("", None, []))

    def test_other_upgrade_code_would_make_a_second_copy(self):
        self.installed[OTHER_UPGRADE] = {"{EEEEEEEE-0000-4000-8000-000000000005}"}
        plan = self.plan(self.package(product=NEW, upgrade=UPGRADE, allusers="1"))
        template, values = plan.refuse
        self.assertEqual(values, {"name": "Demo"})
        self.assertIn(template, FA)

    def test_upgrade_code_that_installed_nothing_is_a_second_copy(self):
        self.assertIsNotNone(self.plan(self.package(product=NEW, upgrade=UPGRADE, allusers="1")).refuse)

    def test_package_without_any_upgrade_information_is_a_second_copy(self):
        self.assertIsNotNone(self.plan(self.package(product=NEW, allusers="1")).refuse)

    def test_same_product_code_is_a_small_update(self):
        plan = self.plan(self.package(product=OLD.lower(), upgrade=UPGRADE, allusers="1"))
        self.assertEqual(plan.extra_args, "REINSTALL=ALL REINSTALLMODE=vomus")
        self.assertIsNone(plan.refuse)

    def test_upgrade_table_can_replace_an_old_upgrade_code(self):
        self.installed[OTHER_UPGRADE] = {OLD}
        pkg = self.package(product=NEW, upgrade=UPGRADE, allusers="1", upgrade_rows=[(OTHER_UPGRADE, 256)])
        self.assertIsNone(self.plan(pkg).refuse)

    def test_detect_only_rows_do_not_replace_anything(self):
        self.installed[OTHER_UPGRADE] = {OLD}
        pkg = self.package(product=NEW, upgrade=UPGRADE, allusers="1", upgrade_rows=[(OTHER_UPGRADE, 2)])
        self.assertIsNotNone(self.plan(pkg).refuse)

    def test_unanswered_lookup_does_not_refuse(self):
        pkg = self.package(product=NEW, upgrade=UPGRADE, allusers="1")
        with mock.patch.object(inplace, "_related_products", lambda code: None):
            plan = inplace.plan(make_app("msi", product_code=OLD), "msi", pkg)
        self.assertEqual((plan.extra_args, plan.refuse), ("", None))

    def test_unreadable_package_means_no_plan(self):
        junk = touch(self.root / "junk.msi", b"this is not an installer database")
        for path in (junk, self.root / "missing.msi"):
            with self.subTest(path=path.name):
                plan = self.plan(path)
                self.assertEqual((plan.extra_args, plan.refuse, plan.warnings), ("", None, []))

    def test_unknown_installed_product_means_no_plan(self):
        plan = self.plan(self.package(product=NEW, upgrade=UPGRADE), make_app("msi"))
        self.assertEqual((plan.extra_args, plan.refuse), ("", None))

    def test_scope_mismatch_is_only_a_warning(self):
        self.installed[UPGRADE] = {OLD}
        cases = [("machine", "", True), ("machine", "1", False), ("machine", "2", False),
                 ("user", "1", True), ("user", "", False), ("user", "2", False)]
        for scope, allusers, warns in cases:
            with self.subTest(scope=scope, allusers=allusers):
                app = make_app("msi", scope=scope, product_code=OLD)
                plan = self.plan(self.package(f"{scope}{allusers}.msi", product=NEW, upgrade=UPGRADE, allusers=allusers), app)
                self.assertIsNone(plan.refuse)
                self.assertEqual(len(plan.warnings), 1 if warns else 0)
                for template, values in plan.warnings:
                    self.assertEqual(values, {"name": "Demo"})
                    self.assertIn(template, FA)

    def test_product_code_sources(self):
        self.assertEqual(inplace._product_code(make_app(product_code=OLD)), OLD)
        self.assertEqual(inplace._product_code(make_app(reg_key=f"HKLM32\\{OLD.lower()}")), OLD)
        self.assertEqual(inplace._product_code(make_app(uninstall_string=f"MsiExec.exe /X{OLD}")), OLD)
        self.assertEqual(inplace._product_code(make_app(uninstall_string=f'"C:\\x\\uninstall.exe" {OLD}')), "")


class WindowsInstallerTest(unittest.TestCase):
    """msi.dll itself, read-only."""

    def test_unused_upgrade_code_has_no_products(self):
        self.assertEqual(inplace._related_products(UPGRADE), set())

    def test_malformed_upgrade_code_is_not_an_answer(self):
        self.assertIsNone(inplace._related_products("not a guid"))

    def test_package_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_msi(Path(tmp) / "a.msi", product=NEW, upgrade=UPGRADE, allusers="1",
                            upgrade_rows=[(OTHER_UPGRADE, 256), ("{FFFFFFFF-0000-4000-8000-000000000006}", 2)])
            package = inplace._read_package(path)
        self.assertEqual(package, {"product": NEW, "upgrade": UPGRADE, "allusers": "1", "replaces": [OTHER_UPGRADE]})


class NeverRaisesTest(unittest.TestCase):
    def test_unexpected_errors_fall_back_to_an_empty_plan(self):
        with mock.patch.object(inplace, "installed_kind", side_effect=RuntimeError("boom")), \
             self.assertLogs("upstream.inplace", "ERROR"):
            plan = inplace.plan(make_app("inno"), "inno", Path("setup.exe"))
        self.assertEqual((plan.extra_args, plan.refuse, plan.warnings), ("", None, []))


if __name__ == "__main__":
    unittest.main()
