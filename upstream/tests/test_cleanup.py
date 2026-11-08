"""Leftover cleanup: backup, removal and undo. Files and folders move inside a temp tree; the registry is only
touched under a throwaway HKCU\\Software\\UpstreamTest_<random> key that each test deletes again. Services, restore
points and everything outside the temp tree are replaced by fakes that fail the test when they are reached."""
from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import winreg
from pathlib import Path
from unittest import mock

from upstream import cleanup, guard, leftovers
from upstream.inventory import InstalledApp
from upstream.leftovers import Leftover


def item(kind: str, location: str, confidence: str = leftovers.CERTAIN, size: int | None = None) -> Leftover:
    return Leftover(kind, str(location), confidence, leftovers.R_NAMED, {}, size)


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


def run(items, **kw):
    calls = []
    kw.setdefault("apps", [])
    kw.setdefault("restore_point", False)
    result = cleanup.run(items, "Test App", lambda template, percent=0, **v: calls.append((template, percent, v)), **kw)
    return result, calls


def reasons(result) -> list[str]:
    return [template for _, template, _ in result.failed]


class TempTree(unittest.TestCase):
    """A temp folder that stands in for the user's disk: the guard allows it, the backups go next to it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upstream_cleanup_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = self.tmp / "world"
        self.world.mkdir()
        real, norm_tmp = guard.protected_path, guard.normalize(str(self.tmp))
        self.real_guard = real
        self.enter(mock.patch.object(cleanup, "QUARANTINE_DIR", self.tmp / "quarantine"))
        self.enter(mock.patch.object(guard, "protected_path",
                                     lambda path: None if guard.inside(guard.normalize(path), norm_tmp) else real(path)))
        self.enter(mock.patch.object(cleanup, "is_admin", lambda: False))

    def enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def manifest(self, backup_id: str) -> dict:
        return json.loads((self.tmp / "quarantine" / backup_id / "manifest.json").read_text("utf-8"))


class FilesAndFolders(TempTree):
    def build(self):
        app = self.world / "App"
        write(app / "a.txt", "alpha")
        write(app / "sub" / "b.txt", "beta")
        write(self.world / "loose.txt", "loose")
        write(self.world / "Menu" / "App.lnk", "shortcut")
        return [item("folder", app), item("file", self.world / "loose.txt"),
                item("shortcut", self.world / "Menu" / "App.lnk", leftovers.LIKELY)]

    def test_backup_remove_list_and_undo(self):
        items = self.build()
        result, calls = run(items)
        self.assertEqual((result.removed, result.failed, result.already_gone), (3, [], 0))
        self.assertEqual(len(result.done), 3)
        self.assertTrue(result.backup_id)
        self.assertFalse((self.world / "App").exists() or (self.world / "loose.txt").exists())
        self.assertTrue((self.world / "Menu").is_dir(), "only the shortcut goes, not its folder")

        data = self.manifest(result.backup_id)
        self.assertEqual((data["app"], data["state"], len(data["items"])), ("Test App", "complete", 3))
        folder_entry = next(e for e in data["items"] if e["kind"] == "folder")
        backed = self.tmp / "quarantine" / result.backup_id / folder_entry["backup"]
        self.assertEqual((backed / "sub" / "b.txt").read_text("utf-8"), "beta")
        self.assertEqual(Path(folder_entry["location"]).name, "App")

        listed = cleanup.backups()
        self.assertEqual([(b.id, b.app_name, b.items) for b in listed], [(result.backup_id, "Test App", 3)])
        self.assertGreater(listed[0].size_bytes, 0)
        self.assertTrue(all(t == cleanup.P_REMOVE for t, _, _ in calls))
        self.assertEqual([c[1] for c in calls], sorted(c[1] for c in calls))
        self.assertTrue(all("item" in c[2] for c in calls))

        undone = cleanup.restore(result.backup_id)
        self.assertEqual((undone.restored, undone.failed, undone.remaining), (3, [], 0))
        self.assertEqual((self.world / "App" / "sub" / "b.txt").read_text("utf-8"), "beta")
        self.assertEqual((self.world / "loose.txt").read_text("utf-8"), "loose")
        self.assertEqual((self.world / "Menu" / "App.lnk").read_text("utf-8"), "shortcut")
        self.assertEqual(cleanup.backups(), [])
        self.assertFalse((self.tmp / "quarantine" / result.backup_id).exists(), "a fully restored backup is dropped")

    def test_only_the_given_items_are_touched(self):
        items = self.build()
        run(items[:1])
        self.assertTrue((self.world / "loose.txt").exists())
        self.assertTrue((self.world / "Menu" / "App.lnk").exists())

    def test_restore_never_overwrites(self):
        result, _ = run(self.build())
        write(self.world / "loose.txt", "new file made later")
        write(self.world / "App" / "a.txt", "reinstalled alpha")      # the folder exists again, with one of its files
        undone = cleanup.restore(result.backup_id)
        self.assertEqual((self.world / "loose.txt").read_text("utf-8"), "new file made later")
        self.assertEqual((self.world / "App" / "a.txt").read_text("utf-8"), "reinstalled alpha")
        self.assertEqual((self.world / "App" / "sub" / "b.txt").read_text("utf-8"), "beta", "what is missing comes back")
        self.assertEqual((self.world / "Menu" / "App.lnk").read_text("utf-8"), "shortcut")
        self.assertEqual(undone.restored, 1)
        self.assertEqual(sorted(t for _, t, _ in undone.failed), sorted([cleanup.R_EXISTS, cleanup.R_SOME_EXIST]))
        self.assertEqual(undone.remaining, 2)
        self.assertEqual([b.items for b in cleanup.backups()], [2], "what was skipped stays restorable")

    def test_parent_and_child_in_one_run(self):
        app = self.world / "App"
        write(app / "inner" / "x.txt")
        result, _ = run([item("file", app / "inner" / "x.txt"), item("folder", app), item("folder", app)])
        self.assertEqual((result.removed, result.already_gone, result.failed), (1, 1, []))
        undone = cleanup.restore(result.backup_id)
        self.assertEqual((undone.restored, undone.failed), (1, []))
        self.assertTrue((app / "inner" / "x.txt").is_file())

    def test_gone_and_changed_items(self):
        write(self.world / "f.txt", "12345")
        result, _ = run([item("file", self.world / "missing.txt"), item("file", self.world / "f.txt", size=3)])
        self.assertEqual((result.removed, result.already_gone), (0, 1))
        self.assertEqual(reasons(result), [cleanup.T_CHANGED])
        self.assertTrue((self.world / "f.txt").exists())
        self.assertIsNone(result.backup_id, "nothing was backed up")
        self.assertFalse((self.tmp / "quarantine").exists() and any((self.tmp / "quarantine").iterdir()))
        wrong = item("folder", self.world / "f.txt")
        self.assertEqual(reasons(run([wrong])[0]), [cleanup.T_CHANGED], "a file where a folder was listed")

    def test_unknown_kind_and_protected_item(self):
        write(self.world / "p.txt")
        with mock.patch.object(cleanup, "_move", side_effect=AssertionError("must not move")):
            result, _ = run([item("folder", self.world / "p.txt", leftovers.PROTECTED), item("socket", "x")])
        self.assertEqual(result.removed, 0)
        self.assertEqual(reasons(result), [leftovers.R_NAMED, cleanup.T_UNKNOWN_KIND])
        self.assertTrue((self.world / "p.txt").exists())

    def test_ownership_check_against_installed_apps(self):
        other = self.world / "Other"
        write(other / "keep.txt")
        write(self.world / "Mine" / "x.txt")
        app = InstalledApp("HKLM64\\Other", "Other App", "1.0", "Vendor", "user", "x64", install_location=str(other))
        inside, parent, mine = item("folder", other / "sub"), item("folder", self.world), item("folder", self.world / "Mine")
        (other / "sub").mkdir()
        result, _ = run([inside, parent, mine], apps=[app])
        self.assertEqual(result.removed, 1)
        self.assertEqual(len(result.failed), 2)
        self.assertTrue(all(t == leftovers.R_INSIDE_OTHER and v == {"other": "Other App"} for _, t, v in result.failed))
        self.assertTrue((other / "keep.txt").exists() and (other / "sub").is_dir())

    def test_cancel_stops_before_the_next_item(self):
        files = [write(self.world / f"f{i}.txt") for i in range(4)]
        stop = threading.Event()
        seen = []

        def progress(template, percent=0, **values):
            seen.append(values.get("item"))
            if len(seen) == 2:
                stop.set()

        result = cleanup.run([item("file", f) for f in files], "Test App", progress, stop, apps=[], restore_point=False)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.removed, 2, "the item being removed when the stop came is finished, the rest is not started")
        self.assertEqual(sum(f.exists() for f in files), 2)

    def test_backups_survive_a_crash_mid_run(self):
        """The manifest names every item before it moves, so a half-finished run can still be undone."""
        write(self.world / "one.txt", "1")
        write(self.world / "two.txt", "2")
        real = cleanup._move
        calls = []

        def crash_on_second(src, dst, is_dir):
            calls.append(src)
            if len(calls) == 2:
                raise KeyboardInterrupt
            real(src, dst, is_dir)

        with mock.patch.object(cleanup, "_move", crash_on_second):
            with self.assertRaises(KeyboardInterrupt):
                run([item("file", self.world / "one.txt"), item("file", self.world / "two.txt")])
        (backup,) = cleanup.backups()
        self.assertEqual(backup.items, 2)
        undone = cleanup.restore(backup.id)
        self.assertEqual((undone.restored, undone.failed), (2, []))
        self.assertEqual((self.world / "one.txt").read_text("utf-8"), "1")
        self.assertEqual((self.world / "two.txt").read_text("utf-8"), "2")

    def test_long_paths(self):
        deep = self.world
        while len(str(deep)) < 300:
            deep = deep / ("d" * 40)
        os.makedirs(cleanup._long(str(deep)))
        with open(cleanup._long(str(deep / "f.txt")), "w") as fh:
            fh.write("deep")
        top = self.world / ("d" * 40)
        result, _ = run([item("folder", top)])
        self.assertEqual((result.removed, result.failed), (1, []))
        self.assertFalse(os.path.exists(cleanup._long(str(top))))
        undone = cleanup.restore(result.backup_id)
        self.assertEqual(undone.restored, 1)
        with open(cleanup._long(str(deep / "f.txt"))) as fh:
            self.assertEqual(fh.read(), "deep")


class RefusedByTheGuard(unittest.TestCase):
    """The real guard, no temp-tree exemption: nothing may be moved, exported or deleted."""

    def test_protected_places_are_refused_before_anything_happens(self):
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        places = [item("folder", windows), item("folder", os.path.join(windows, "System32")),
                  item("folder", os.environ.get("ProgramFiles", r"C:\Program Files")),
                  item("folder", os.environ.get("USERPROFILE", "C:\\Users")), item("folder", "C:\\"),
                  item("folder", os.environ.get("TEMP", tempfile.gettempdir())),
                  item("file", "relative\\path.txt"), item("shortcut", "\\\\server\\share\\a.lnk"),
                  item("regkey", "HKCU\\Software"), item("regkey", "HKLM\\SOFTWARE\\Microsoft\\Windows"),
                  item("regkey", "HKCU\\Software\\Classes\\.txt"), item("regkey", "HKLM\\SYSTEM\\CurrentControlSet"),
                  item("regvalue", "HKCU\\Software\\Vendor|Setting"), item("service", "HKCU\\Software\\Vendor"),
                  item("regkey", "HKLM\\SYSTEM\\CurrentControlSet\\Services\\Foo")]
        boom = AssertionError("an action was reached for a protected item")
        with mock.patch.object(cleanup, "_move", side_effect=boom), mock.patch.object(cleanup, "_reg_export", side_effect=boom), \
                mock.patch.object(cleanup, "_delete_key_tree", side_effect=boom), mock.patch.object(cleanup, "_delete_service", side_effect=boom), \
                mock.patch.object(cleanup, "is_admin", lambda: True), mock.patch.object(cleanup._Session, "save", side_effect=boom):
            result, _ = run(places)
        self.assertEqual(result.removed, 0)
        self.assertEqual(len(result.failed), len(places))
        known = set(guard.REASONS) | set(cleanup.REASONS)
        self.assertTrue(all(template in known for _, template, _ in result.failed), reasons(result))
        self.assertIsNone(result.backup_id)


class Links(TempTree):
    def junction(self, link: Path, target: Path):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode:
            self.skipTest("cannot create a junction here")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))

    def test_a_link_is_never_followed(self):
        target = self.tmp / "target"
        write(target / "precious.txt", "keep me")
        link = self.world / "link"
        self.junction(link, target)
        with mock.patch.object(cleanup, "_move", side_effect=AssertionError("must not move")):
            result, _ = run([item("folder", link), item("file", link / "precious.txt")])
        self.assertEqual(reasons(result), [leftovers.R_LINK, leftovers.R_LINK])
        self.assertEqual((target / "precious.txt").read_text("utf-8"), "keep me")

    def test_a_link_inside_a_folder_is_moved_not_followed(self):
        target = self.tmp / "target"
        write(target / "precious.txt", "keep me")
        app = self.world / "App"
        write(app / "a.txt")
        self.junction(app / "link", target)
        result, _ = run([item("folder", app)])
        self.assertEqual((result.removed, result.failed), (1, []))
        self.assertEqual((target / "precious.txt").read_text("utf-8"), "keep me")
        cleanup.restore(result.backup_id)
        self.assertTrue((app / "a.txt").exists() and (target / "precious.txt").exists())

    def test_copying_across_drives_refuses_a_folder_with_a_link(self):
        target = self.tmp / "target"
        write(target / "precious.txt", "keep me")
        app = self.world / "App"
        write(app / "a.txt")
        self.junction(app / "link", target)
        with mock.patch.object(cleanup, "_rename", side_effect=OSError(errno.EXDEV, "other drive")):
            result, _ = run([item("folder", app)])
        self.assertEqual(reasons(result), [cleanup.T_LINK_INSIDE])
        self.assertTrue((app / "a.txt").exists())
        self.assertTrue((target / "precious.txt").exists())
        self.assertIsNone(result.backup_id)


class AcrossDrives(TempTree):
    """No second drive is needed: the rename is made to fail the way Windows fails it between drives."""

    def setUp(self):
        super().setUp()
        self.enter(mock.patch.object(cleanup, "_rename", side_effect=OSError(errno.EXDEV, "other drive")))

    def test_copy_then_delete(self):
        app = self.world / "App"
        write(app / "a.txt", "alpha")
        write(app / "sub" / "b.txt", "beta")
        write(self.world / "f.txt", "file")
        result, _ = run([item("folder", app), item("file", self.world / "f.txt")])
        self.assertEqual((result.removed, result.failed), (2, []))
        self.assertFalse(app.exists() or (self.world / "f.txt").exists())
        folder = next(e for e in self.manifest(result.backup_id)["items"] if e["kind"] == "folder")
        self.assertEqual((self.tmp / "quarantine" / result.backup_id / folder["backup"] / "sub" / "b.txt").read_text("utf-8"), "beta")
        with mock.patch.object(cleanup, "_rename", side_effect=OSError(errno.EXDEV, "other drive")):
            undone = cleanup.restore(result.backup_id)
        self.assertEqual((undone.restored, undone.failed), (2, []))
        self.assertEqual((app / "sub" / "b.txt").read_text("utf-8"), "beta")
        self.assertEqual((self.world / "f.txt").read_text("utf-8"), "file")

    def test_a_locked_file_keeps_its_original_and_leaves_no_copy(self):
        locked = write(self.world / "locked.txt", "busy")
        with open(locked, "rb"):         # Python opens without share-delete: Windows refuses to delete it
            result, _ = run([item("file", locked)])
        self.assertEqual(reasons(result), [cleanup.T_IN_USE])
        self.assertEqual(locked.read_text("utf-8"), "busy")
        self.assertIsNone(result.backup_id)

    def test_a_folder_with_a_locked_file_is_not_lost(self):
        app = self.world / "App"
        locked = write(app / "locked.txt", "busy")
        write(app / "free.txt", "free")
        with open(locked, "rb"):
            result, _ = run([item("folder", app)])
        self.assertEqual(result.removed, 0)
        self.assertIn(reasons(result)[0], (cleanup.T_PARTIAL, cleanup.T_IN_USE))
        self.assertEqual(locked.read_text("utf-8"), "busy")
        if reasons(result)[0] == cleanup.T_PARTIAL:      # some files went: the backup holds everything and can be undone
            self.assertEqual(cleanup.backups()[0].items, 1)
            with mock.patch.object(cleanup, "_rename", side_effect=OSError(errno.EXDEV, "other drive")):
                undone = cleanup.restore(result.backup_id)
            self.assertEqual((app / "free.txt").read_text("utf-8"), "free")

    def test_not_enough_room_for_the_copy(self):
        write(self.world / "big.txt", "x" * 10)
        with mock.patch.object(cleanup.shutil, "disk_usage", return_value=mock.Mock(free=1000)):
            result, _ = run([item("file", self.world / "big.txt")])
        self.assertEqual(reasons(result), [cleanup.T_NO_SPACE])
        self.assertTrue((self.world / "big.txt").exists())


class Denied(TempTree):
    def test_a_folder_with_an_open_file_is_not_moved(self):
        app = self.world / "App"
        held = write(app / "held.txt", "busy")
        with open(held, "rb"):
            result, _ = run([item("folder", app)])
        self.assertEqual(result.removed, 0)
        self.assertIn(reasons(result)[0], (cleanup.T_IN_USE, cleanup.T_DENIED))
        self.assertEqual(held.read_text("utf-8"), "busy")

    def test_machine_wide_folders_name_the_missing_rights(self):
        machine = Leftover("folder", os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Vendor", "App"),
                           leftovers.CERTAIN, leftovers.R_NAMED)
        self.assertTrue(cleanup.needs_admin(machine))
        self.assertFalse(cleanup.needs_admin(item("folder", self.world)))
        e = PermissionError(errno.EACCES, "denied", None, 5)
        self.assertEqual(cleanup._map_oserror(e, False, True).template, cleanup.T_ADMIN)
        self.assertEqual(cleanup._map_oserror(e, False, False).template, cleanup.T_DENIED)
        self.assertEqual(cleanup._map_oserror(e, True, True).template, cleanup.T_DENIED)
        busy = PermissionError(errno.EACCES, "busy", None, 32)
        self.assertEqual(cleanup._map_oserror(busy, False, True).template, cleanup.T_IN_USE)


class Registry(TempTree):
    def setUp(self):
        super().setUp()
        self.sub = f"Software\\UpstreamTest_{secrets.token_hex(4)}"
        self.addCleanup(self.delete_tree, self.sub)
        self.location = "HKCU\\" + self.sub

    @staticmethod
    def delete_tree(sub: str):
        def walk(path: str):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                    names = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
            except OSError:
                return
            for name in names:
                walk(path + "\\" + name)
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, path, winreg.KEY_WOW64_64KEY)
        walk(sub)

    def populate(self):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, "Name", 0, winreg.REG_SZ, "Test ünïcode")
            winreg.SetValueEx(key, "Count", 0, winreg.REG_DWORD, 7)
            winreg.SetValueEx(key, "Blob", 0, winreg.REG_BINARY, b"\x00\x01\xff")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.sub + "\\Child\\Deep", 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, "List", 0, winreg.REG_MULTI_SZ, ["a", "b"])
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, "%TEMP%\\x")

    def snapshot(self, sub: str) -> dict:
        found = {}

        def walk(path: str):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                subs, values, _ = winreg.QueryInfoKey(key)
                found[path[len(sub):]] = {n: winreg.EnumValue(key, i)[1:] for i, n in
                                          [(i, winreg.EnumValue(key, i)[0]) for i in range(values)]}
                for i in range(subs):
                    walk(path + "\\" + winreg.EnumKey(key, i))
        walk(sub)
        return found

    def exists(self, sub: str) -> bool:
        return cleanup.key_exists("HKCU\\" + sub)

    def test_key_backup_remove_and_undo(self):
        self.populate()
        before = self.snapshot(self.sub)
        self.assertEqual(len(before), 3)
        result, _ = run([item("regkey", self.location)])
        self.assertEqual((result.removed, result.failed), (1, []))
        self.assertFalse(self.exists(self.sub))
        entry = self.manifest(result.backup_id)["items"][0]
        reg = self.tmp / "quarantine" / result.backup_id / entry["reg"]
        self.assertGreater(reg.stat().st_size, 0)
        self.assertIn("Child\\Deep", reg.read_text("utf-16"))
        self.assertEqual(cleanup.backups()[0].items, 1)

        undone = cleanup.restore(result.backup_id)
        self.assertEqual((undone.restored, undone.failed), (1, []))
        self.assertEqual(self.snapshot(self.sub), before)
        self.assertEqual(cleanup.backups(), [])

    def test_restore_leaves_an_existing_key_alone(self):
        self.populate()
        result, _ = run([item("regkey", self.location)])
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, "Name", 0, winreg.REG_SZ, "made later")
        undone = cleanup.restore(result.backup_id)
        self.assertEqual((undone.restored, [t for _, t, _ in undone.failed]), (0, [cleanup.R_EXISTS]))
        self.assertEqual(self.snapshot(self.sub)[""]["Name"][0], "made later")

    def test_a_tampered_backup_cannot_write_elsewhere(self):
        self.populate()
        result, _ = run([item("regkey", self.location)])
        entry = self.manifest(result.backup_id)["items"][0]
        reg = self.tmp / "quarantine" / result.backup_id / entry["reg"]
        evil = "HKEY_CURRENT_USER\\Software\\UpstreamTestElsewhere"
        reg.write_text(reg.read_text("utf-16") + f"\n[{evil}]\n\"x\"=\"y\"\n", "utf-16")
        with mock.patch.object(cleanup, "_reg_import", side_effect=AssertionError("must not import")):
            undone = cleanup.restore(result.backup_id)
        self.assertEqual([t for _, t, _ in undone.failed], [cleanup.R_UNREADABLE])
        self.assertFalse(self.exists("Software\\UpstreamTestElsewhere"))

    def test_value_backup_remove_and_undo(self):
        self.populate()
        key_text = self.location
        allow = mock.patch.object(guard, "protected_value", lambda key, name: None if key == key_text else "protected")
        with allow:
            result, _ = run([item("regvalue", f"{key_text}|Count"), item("regvalue", f"{key_text}|Blob"),
                             item("regvalue", f"{key_text}|Nope")])
            self.assertEqual((result.removed, result.already_gone, result.failed), (2, 1, []))
            names = self.snapshot(self.sub)[""]
            self.assertEqual(sorted(names), ["Name"])
            entries = self.manifest(result.backup_id)["items"]
            self.assertEqual({e["value"]["name"]: e["value"] for e in entries}["Blob"]["bytes"], "0001ff")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, "Count", 0, winreg.REG_DWORD, 99)     # made again later
            undone = cleanup.restore(result.backup_id)
        self.assertEqual(undone.restored, 1)
        self.assertEqual([t for _, t, _ in undone.failed], [cleanup.R_EXISTS])
        values = self.snapshot(self.sub)[""]
        self.assertEqual(values["Blob"], (b"\x00\x01\xff", winreg.REG_BINARY))
        self.assertEqual(values["Count"][0], 99, "an existing value is not overwritten")

    def test_only_startup_values_are_removable(self):
        self.populate()
        with mock.patch.object(cleanup, "_open", side_effect=AssertionError("must not open")):
            result, _ = run([item("regvalue", f"{self.location}|Count")])
        self.assertEqual(reasons(result), [guard.R_VALUE])
        self.assertEqual(self.snapshot(self.sub)[""]["Count"][0], 7)

    def test_machine_keys_need_administrator_rights(self):
        boom = AssertionError("must not reach the registry")
        with mock.patch.object(cleanup, "_reg_export", side_effect=boom), mock.patch.object(cleanup, "_delete_key_tree", side_effect=boom), \
                mock.patch.object(cleanup, "_delete_service", side_effect=boom):
            result, _ = run([item("regkey", "HKLM\\SOFTWARE\\UpstreamTestNobody"),
                             item("service", "HKLM\\SYSTEM\\CurrentControlSet\\Services\\UpstreamTestSvc"),
                             item("regvalue", "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run|UpstreamTest")])
        self.assertEqual(reasons(result), [cleanup.T_ADMIN] * 3)

    def test_service_is_exported_deleted_through_windows_and_not_restored(self):
        name = "UpstreamTestSvc"
        location = f"HKLM\\SYSTEM\\CurrentControlSet\\Services\\{name}"

        def fake_export(loc, dest):
            self.assertEqual(loc, location)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("Windows Registry Editor Version 5.00", "utf-16")

        with mock.patch.object(cleanup, "is_admin", lambda: True), \
                mock.patch.object(cleanup, "key_exists", lambda loc: True), \
                mock.patch.object(cleanup, "_reg_export", fake_export), \
                mock.patch.object(cleanup, "_delete_service", return_value=0) as delete:
            result, _ = run([item("service", location)])
            self.assertEqual((result.removed, result.failed), (1, []))
            self.assertTrue(result.restart_needed, "the key is still there, so Windows removes it at the next restart")
            delete.assert_called_once_with(name)
            denied = run([item("service", location)])[0]
        self.assertEqual(denied.removed, 1)
        with mock.patch.object(cleanup, "is_admin", lambda: True), mock.patch.object(cleanup, "key_exists", lambda loc: True), \
                mock.patch.object(cleanup, "_reg_export", fake_export), mock.patch.object(cleanup, "_delete_service", return_value=5):
            refused = run([item("service", location)])[0]
        self.assertEqual(reasons(refused), [cleanup.T_ADMIN])
        self.assertIsNone(refused.backup_id, "nothing was removed, so no backup is kept")
        undone = cleanup.restore(result.backup_id)
        self.assertEqual((undone.restored, [t for _, t, _ in undone.failed]), (0, [cleanup.R_SERVICE]))

    def test_a_service_windows_does_not_know_loses_its_leftover_key(self):
        """Error 1060 (no such service) used to count as removed while the key stayed and a restart was promised."""
        location = "HKLM\\SYSTEM\\CurrentControlSet\\Services\\UpstreamTestSvc"
        present = [True]
        deleted = []

        def fake_export(loc, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("Windows Registry Editor Version 5.00", "utf-16")

        def fake_tree_delete(hive, sub, depth=0, gone=None):
            deleted.append((hive, sub))
            present[0] = False

        with mock.patch.object(cleanup, "is_admin", lambda: True), mock.patch.object(cleanup, "key_exists", lambda loc: present[0]), \
                mock.patch.object(cleanup, "_reg_export", fake_export), mock.patch.object(cleanup, "_delete_service", return_value=1060), \
                mock.patch.object(cleanup, "_delete_key_tree", fake_tree_delete):
            result, _ = run([item("service", location)])
        self.assertEqual((result.removed, result.failed), (1, []))
        self.assertEqual(deleted, [(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Services\\UpstreamTestSvc")])
        self.assertFalse(result.restart_needed, "the key is gone, so there is nothing to finish at the next restart")

    def make_tree(self):
        """The key with two values, a child `a`, and `b\\c`; returns what snapshot() sees of it."""
        for child, name in (("", "Name"), ("\\a", "A"), ("\\b\\c", "C")):
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.sub + child, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, "value of " + name)
        return self.snapshot(self.sub)

    def test_a_delete_that_fails_half_way_keeps_the_backup_and_undo_completes_the_tree(self):
        before = self.make_tree()
        real, calls = winreg.DeleteKeyEx, []

        def flaky(*args):
            calls.append(args[1])
            if len(calls) == 3:     # `a` and `b\c` are gone, `b` is refused
                raise PermissionError(errno.EACCES, "denied", None, 5)
            return real(*args)

        with mock.patch.object(cleanup.winreg, "DeleteKeyEx", flaky):
            result, _ = run([item("regkey", self.location)])
        self.assertEqual((result.removed, reasons(result)), (0, [cleanup.T_KEY_PARTIAL]))
        self.assertEqual(result.failed[0][2], {"error": "denied"})
        self.assertTrue(result.backup_id, "part of the tree is gone: the export must stay")
        self.assertTrue(self.exists(self.sub) and self.exists(self.sub + "\\b"))
        self.assertFalse(self.exists(self.sub + "\\a") or self.exists(self.sub + "\\b\\c"))
        entry = self.manifest(result.backup_id)["items"][0]
        self.assertEqual(entry["status"], "partial")
        self.assertGreater((self.tmp / "quarantine" / result.backup_id / entry["reg"]).stat().st_size, 0)
        self.assertEqual([(b.id, b.items) for b in cleanup.backups()], [(result.backup_id, 1)])

        undone = cleanup.restore(result.backup_id)     # the key exists, but its entry is partial: the export is merged in
        self.assertEqual((undone.restored, undone.failed, undone.remaining), (1, [], 0))
        self.assertEqual(self.snapshot(self.sub), before)
        self.assertEqual(cleanup.backups(), [])

    def test_a_delete_that_fails_before_anything_changed_leaves_no_backup(self):
        self.make_tree()

        def refuse(*args):
            raise PermissionError(errno.EACCES, "denied", None, 5)

        with mock.patch.object(cleanup.winreg, "DeleteKeyEx", refuse):
            result, _ = run([item("regkey", self.location)])
        self.assertEqual(reasons(result), [cleanup.T_DENIED])
        self.assertIsNone(result.backup_id)

    def set_value(self, name: str, data: str, sub: str | None = None):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub or self.sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)

    def installed_app(self, folder: Path) -> InstalledApp:
        return InstalledApp("HKLM64\\Other", "Other App", "1.0", "Vendor", "user", "x64", install_location=str(folder))

    def test_a_startup_entry_that_runs_an_installed_app_now_is_left(self):
        """The app was installed again while the review was open: its Run value is the new copy's."""
        other = self.world / "Other"
        other.mkdir()
        self.set_value("Updater", f'"{other}\\app.exe" /min')
        key_text = self.location
        with mock.patch.object(guard, "protected_value", lambda key, name: None if key == key_text else "protected"):
            result, _ = run([item("regvalue", f"{key_text}|Updater")], apps=[self.installed_app(other)])
        self.assertEqual(reasons(result), [leftovers.R_INSIDE_OTHER])
        self.assertEqual(result.failed[0][2], {"other": "Other App"})
        self.assertEqual(self.snapshot(self.sub)[""]["Updater"][0], f'"{other}\\app.exe" /min')
        self.assertIsNone(result.backup_id)

    def test_a_key_that_points_into_an_installed_app_now_is_left(self):
        other = self.world / "Other"
        other.mkdir()
        self.set_value("InstallDir", str(other))
        result, _ = run([item("regkey", self.location)], apps=[self.installed_app(other)])
        self.assertEqual(reasons(result), [leftovers.R_KEY_OTHER])
        self.assertTrue(self.exists(self.sub))

    def test_a_startup_entry_that_changed_since_the_review_is_left(self):
        self.set_value("Updater", r"C:\Old\updater.exe")
        key_text = self.location
        picked = [item("regvalue", f"{key_text}|Updater")]
        with mock.patch.object(guard, "protected_value", lambda key, name: None if key == key_text else "protected"):
            baseline = cleanup.snapshot(picked)
            self.assertEqual(len(baseline), 1)
            self.set_value("Updater", r"C:\New\updater.exe")
            result, _ = run(picked, baseline=baseline)
            self.assertEqual(reasons(result), [cleanup.T_CHANGED])
            self.assertEqual(self.snapshot(self.sub)[""]["Updater"][0], r"C:\New\updater.exe")
            self.assertIsNone(result.backup_id)
            again, _ = run(picked, baseline=cleanup.snapshot(picked))     # a review made now matches
        self.assertEqual((again.removed, again.failed), (1, []))

    def test_a_key_written_to_since_the_review_is_left(self):
        self.populate()
        picked = [item("regkey", self.location)]
        baseline = cleanup.snapshot(picked)
        self.assertEqual(len(baseline), 1)
        self.set_value("Later", "written after the review", self.sub + "\\Child\\Deep")
        result, _ = run(picked, baseline=baseline)
        self.assertEqual(reasons(result), [cleanup.T_CHANGED])
        self.assertTrue(self.exists(self.sub))
        self.assertIsNone(result.backup_id)
        self.assertEqual(run(picked, baseline=cleanup.snapshot(picked))[0].removed, 1)

    def test_a_service_whose_image_path_changed_is_left(self):
        location = "HKLM\\SYSTEM\\CurrentControlSet\\Services\\UpstreamTestSvc"
        picked = [item("service", location)]
        states = {"now": "old image"}
        boom = AssertionError("must not be reached")
        with mock.patch.object(cleanup, "_state", lambda it: states["now"]), mock.patch.object(cleanup, "is_admin", lambda: True), \
                mock.patch.object(cleanup, "key_exists", lambda loc: True), mock.patch.object(cleanup, "_reg_export", side_effect=boom), \
                mock.patch.object(cleanup, "_delete_service", side_effect=boom):
            baseline = cleanup.snapshot(picked)
            states["now"] = "another program"
            result, _ = run(picked, baseline=baseline)
        self.assertEqual(reasons(result), [cleanup.T_CHANGED])
        self.assertIsNone(result.backup_id)

    def test_the_state_of_a_service_is_its_image_path(self):
        service = Leftover("service", self.location, leftovers.CERTAIN, leftovers.R_NAMED)
        self.set_value("ImagePath", r"C:\Old\svc.exe")
        old = cleanup._state(service)
        self.assertIn("svc.exe", old)
        self.set_value("ImagePath", r"C:\New\svc.exe")
        self.assertNotEqual(cleanup._state(service), old)
        self.assertIsNone(cleanup._state(Leftover("service", self.location + "\\Nope", leftovers.CERTAIN, leftovers.R_NAMED)))

    def test_registry_items_are_left_when_the_installed_apps_cannot_be_read(self):
        self.populate()
        key_text = self.location
        picked = [item("regkey", key_text), item("regvalue", f"{key_text}|Count")]
        with mock.patch.object(guard, "protected_value", lambda key, name: None if key == key_text else "protected"), \
                mock.patch("upstream.inventory.read_installed_apps", side_effect=OSError("registry unreadable")), \
                self.assertLogs("upstream.cleanup", "ERROR"):
            result, _ = run(picked, apps=None)
        self.assertEqual(reasons(result), [cleanup.T_NO_OWNERS] * 2)
        self.assertTrue(self.exists(self.sub))
        self.assertIsNone(result.backup_id)

    def test_a_tampered_backup_cannot_import_a_file_from_elsewhere(self):
        """A valid export outside the backup folder, named by the manifest, is not imported."""
        self.populate()
        result, _ = run([item("regkey", self.location)])
        folder = self.tmp / "quarantine" / result.backup_id
        outside = self.tmp / "outside.reg"
        shutil.copy(folder / self.manifest(result.backup_id)["items"][0]["reg"], outside)
        for named in ("..\\..\\outside.reg", str(outside)):
            with self.subTest(named=named):
                edit_manifest(self.tmp, result.backup_id, reg=named)
                with mock.patch.object(cleanup, "_reg_import", side_effect=AssertionError("must not import")):
                    undone = cleanup.restore(result.backup_id)
                self.assertEqual([t for _, t, _ in undone.failed], [cleanup.R_UNREADABLE])
                self.assertFalse(self.exists(self.sub))


def edit_manifest(tmp: Path, backup_id: str, index: int = 0, **changes) -> None:
    path = tmp / "quarantine" / backup_id / "manifest.json"
    data = json.loads(path.read_text("utf-8"))
    data["items"][index].update(changes)
    path.write_text(json.dumps(data), "utf-8")


class ReadingRegFiles(TempTree):
    """reg.exe reads a key header after spaces or a tab and ignores a comment behind it, so all of those count."""
    KEY = "HKCU\\Software\\UpstreamTest_abcd"
    HOME = "HKEY_CURRENT_USER\\Software\\UpstreamTest_abcd"
    ELSEWHERE = "HKEY_CURRENT_USER\\Software\\UpstreamTestElsewhere"

    def check(self, *lines: str) -> bool:
        file = self.tmp / "t.reg"
        file.write_text("\r\n".join(["Windows Registry Editor Version 5.00", "", f"[{self.HOME}]", '"a"="b"', *lines]), "utf-16")
        return cleanup._reg_file_inside(file, self.KEY)

    def test_headers_inside_the_key(self):
        self.assertTrue(self.check())
        self.assertTrue(self.check("", f"[{self.HOME}\\Child]", '"x"=dword:00000001'))
        self.assertTrue(self.check(f"  [{self.HOME}\\Child]"), "an indented header of a sub-key is fine")

    def test_a_header_elsewhere_in_any_spelling_refuses_the_file(self):
        spellings = [f"[{self.ELSEWHERE}]", f" [{self.ELSEWHERE}]", f"\t[{self.ELSEWHERE}]", f"  \t [{self.ELSEWHERE}]  ",
                     f"[{self.ELSEWHERE}] ; note", f"[{self.ELSEWHERE}]\t;", f"[-{self.ELSEWHERE}]",
                     f"[{self.HOME}xyz]", "[HKEY_LOCAL_MACHINE\\SOFTWARE\\UpstreamTestElsewhere]"]
        for line in spellings:
            with self.subTest(line=line):
                self.assertFalse(self.check("", line, '"x"="y"'))

    def test_a_header_with_anything_after_the_bracket_refuses_the_file(self):
        self.assertFalse(self.check(f"[{self.HOME}\\Child] ; harmless"))
        self.assertFalse(self.check(f"[{self.HOME}] trailing"))

    def test_nothing_inside_means_refused(self):
        file = self.tmp / "empty.reg"
        file.write_text("Windows Registry Editor Version 5.00\r\n", "utf-16")
        self.assertFalse(cleanup._reg_file_inside(file, self.KEY))
        self.assertFalse(cleanup._reg_file_inside(self.tmp / "missing.reg", self.KEY))


class Confinement(TempTree):
    """The manifest sits in a folder the user can write to: what it names must lie inside its own backup."""

    def test_inside_folder(self):
        folder = self.tmp / "quarantine" / "20260101-000000-abcdef"
        folder.mkdir(parents=True)
        self.assertEqual(cleanup._inside_folder(folder, "files/C/x.txt"), folder / "files/C/x.txt")
        self.assertEqual(cleanup._inside_folder(folder, "registry\\1.reg"), folder / "registry\\1.reg")
        for bad in ("..", "../x", "files/../../x", "..\\..\\world", str(self.tmp), "C:\\Windows", "", " ", ".", None, 5, ["a"]):
            with self.subTest(bad=bad):
                self.assertIsNone(cleanup._inside_folder(folder, bad))

    def test_a_manifest_cannot_make_a_restore_move_something_else(self):
        write(self.world / "loose.txt", "loose")
        secret = write(self.tmp / "secret.txt", "not part of any backup")
        result, _ = run([item("file", self.world / "loose.txt")])
        for named in ("..\\..\\secret.txt", str(secret)):
            with self.subTest(named=named):
                edit_manifest(self.tmp, result.backup_id, backup=named)
                with mock.patch.object(cleanup, "_move", side_effect=AssertionError("must not move")):
                    undone = cleanup.restore(result.backup_id)
                self.assertEqual([t for _, t, _ in undone.failed], [cleanup.R_UNREADABLE])
                self.assertEqual(secret.read_text("utf-8"), "not part of any backup")
                self.assertFalse((self.world / "loose.txt").exists())
        edit_manifest(self.tmp, result.backup_id, backup=cleanup._backup_dest(Path("."), str(self.world / "loose.txt")).as_posix())
        self.assertEqual(cleanup.restore(result.backup_id).restored, 1, "the real entry still restores")

    def test_a_link_inside_the_backup_cannot_lead_out_of_it(self):
        target = self.tmp / "target"
        write(target / "precious.txt", "keep me")
        write(self.world / "loose.txt", "loose")
        result, _ = run([item("file", self.world / "loose.txt")])
        folder = self.tmp / "quarantine" / result.backup_id
        link = folder / "files" / "evil"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode:
            self.skipTest("cannot create a junction here")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        edit_manifest(self.tmp, result.backup_id, backup="files/evil/precious.txt")
        with mock.patch.object(cleanup, "_move", side_effect=AssertionError("must not move")):
            undone = cleanup.restore(result.backup_id)
        self.assertEqual([t for _, t, _ in undone.failed], [cleanup.R_UNREADABLE])
        self.assertTrue((target / "precious.txt").exists())


class ManifestWrites(TempTree):
    def test_the_manifest_reaches_the_disk_before_it_replaces_the_old_one(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            order.append("fsync")
            real_fsync(fd)

        def replace(src, dst):
            order.append("replace")
            real_replace(src, dst)

        target = self.tmp / "manifest.json"
        with mock.patch.object(cleanup.os, "fsync", fsync), mock.patch.object(cleanup.os, "replace", replace):
            cleanup._write_json(target, {"app": "ünï", "items": []})
        self.assertEqual(order, ["fsync", "replace"])
        self.assertEqual(json.loads(target.read_text("utf-8")), {"app": "ünï", "items": []})
        self.assertFalse(target.with_suffix(".tmp").exists())


class OwnershipFailsClosed(TempTree):
    def test_files_are_left_when_the_installed_apps_cannot_be_read(self):
        write(self.world / "a.txt")
        write(self.world / "App" / "b.txt")
        picked = [item("file", self.world / "a.txt"), item("folder", self.world / "App")]
        with mock.patch("upstream.inventory.read_installed_apps", side_effect=OSError("registry unreadable")), \
                mock.patch.object(cleanup, "_move", side_effect=AssertionError("must not move")), self.assertLogs("upstream.cleanup", "ERROR"):
            result, _ = run(picked, apps=None)
        self.assertEqual((result.removed, reasons(result)), (0, [cleanup.T_NO_OWNERS] * 2))
        self.assertTrue((self.world / "a.txt").exists() and (self.world / "App" / "b.txt").exists())
        self.assertIsNone(result.backup_id)

    def test_a_protected_item_still_names_its_own_reason(self):
        with mock.patch("upstream.inventory.read_installed_apps", side_effect=OSError("x")), self.assertLogs("upstream.cleanup", "ERROR"):
            result, _ = run([item("folder", os.environ.get("SystemRoot", r"C:\Windows"))], apps=None)
        self.assertNotIn(cleanup.T_NO_OWNERS, reasons(result))


class BackupList(TempTree):
    def fake_backup(self, name: str, days_old: float, state: str = "removed") -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - days_old * 86400))
        backup_id = f"{stamp}-{secrets.token_hex(3)}"
        folder = self.tmp / "quarantine" / backup_id
        write(folder / "files" / "C" / "x.txt", "kept")
        data = {"version": 1, "id": backup_id, "app": name, "ts": time.time() - days_old * 86400, "state": "complete",
                "items": [{"n": 1, "kind": "file", "location": str(self.world / "x.txt"), "backup": "files/C/x.txt",
                           "status": state, "bytes": 4}]}
        (folder / "manifest.json").write_text(json.dumps(data), "utf-8")
        return backup_id

    def test_listing_newest_first_and_ignoring_junk(self):
        old, new = self.fake_backup("Old", 40), self.fake_backup("New", 1)
        self.fake_backup("Restored", 2, "restored")
        (self.tmp / "quarantine" / "not-a-backup").mkdir()
        broken = self.tmp / "quarantine" / "20200101-000000-abcdef"
        broken.mkdir()
        (broken / "manifest.json").write_text("{not json", "utf-8")
        self.assertEqual([(b.id, b.app_name, b.items, b.size_bytes) for b in cleanup.backups()],
                         [(new, "New", 1, 4), (old, "Old", 1, 4)])

    def test_purge_and_delete_stay_inside_the_backup_folder(self):
        old, new = self.fake_backup("Old", 40), self.fake_backup("New", 1)
        outside = write(self.tmp / "keep.txt")
        self.assertEqual(cleanup.purge_old(0), 0)
        self.assertEqual(cleanup.purge_old(30), 1)
        self.assertEqual([b.id for b in cleanup.backups()], [new])
        for bad in ("..", "..\\world", "", "20200101-000000-zzzzzz", str(self.world)):
            cleanup.delete_backup(bad)
        self.assertTrue(outside.exists() and self.world.exists() and (self.tmp / "quarantine" / new).exists())
        cleanup.delete_backup(new)
        self.assertEqual(cleanup.backups(), [])
        self.assertEqual(cleanup.restore(old).failed[0][1], cleanup.R_UNREADABLE, "a deleted backup cannot be restored")

    def test_read_only_files_do_not_stop_a_delete(self):
        backup_id = self.fake_backup("RO", 1)
        os.chmod(self.tmp / "quarantine" / backup_id / "files" / "C" / "x.txt", 0o444)
        cleanup.delete_backup(backup_id)
        self.assertFalse((self.tmp / "quarantine" / backup_id).exists())

    def test_delete_says_whether_the_backup_is_gone(self):
        backup_id = self.fake_backup("Gone", 1)
        stuck = self.fake_backup("Stuck", 1)
        self.assertTrue(cleanup.delete_backup(backup_id))
        self.assertTrue(cleanup.delete_backup(backup_id), "already gone is gone")
        for bad in ("", "..", "not-an-id", None):
            self.assertFalse(cleanup.delete_backup(bad))
        with mock.patch.object(cleanup, "_remove_tree", side_effect=PermissionError(errno.EACCES, "denied", None, 5)), \
                self.assertLogs("upstream.cleanup", "WARNING"):
            self.assertFalse(cleanup.delete_backup(stuck), "a backup an elevated Upstream made can't be deleted by a standard one")
        self.assertEqual([b.id for b in cleanup.backups()], [stuck])
        self.assertTrue(cleanup.delete_backup(stuck))

    def holding_the_lock(self):
        """Another thread holds the cleanup lock, the way a running cleanup or undo does."""
        held, release = threading.Event(), threading.Event()

        def hold():
            with cleanup._lock:
                held.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(5))
        return release, thread

    def test_nothing_is_deleted_while_a_cleanup_or_an_undo_runs(self):
        old, new = self.fake_backup("Old", 40), self.fake_backup("New", 1)
        release, thread = self.holding_the_lock()
        try:
            self.assertFalse(cleanup.delete_backup(new))
            self.assertEqual(cleanup.purge_old(30), 0)
            self.assertEqual(len(list((self.tmp / "quarantine").iterdir())), 2)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(cleanup.purge_old(30), 1)
        self.assertTrue(cleanup.delete_backup(new))

    def test_an_undo_reads_the_manifest_only_once_it_has_the_lock(self):
        """A delete that finished while the undo waited must not be undone from a stale copy."""
        backup_id = self.fake_backup("Waiting", 1)
        release, thread = self.holding_the_lock()
        out = []
        worker = threading.Thread(target=lambda: out.append(cleanup.restore(backup_id)))
        try:
            worker.start()
            time.sleep(0.3)      # the undo is now waiting for the lock
            shutil.rmtree(self.tmp / "quarantine" / backup_id)
        finally:
            release.set()
            thread.join(5)
        worker.join(5)
        self.assertEqual([(w, t) for w, t, _ in out[0].failed], [(backup_id, cleanup.R_UNREADABLE)])

    def test_a_service_export_keeps_its_backup_after_everything_else_is_back(self):
        backup_id = self.fake_backup("Svc", 1)
        folder = self.tmp / "quarantine" / backup_id
        write(folder / "registry" / "2.reg", "export of the service key")
        path = folder / "manifest.json"
        data = json.loads(path.read_text("utf-8"))
        data["items"].append({"n": 2, "kind": "service", "location": "HKLM\\SYSTEM\\CurrentControlSet\\Services\\UpstreamTestSvc",
                              "reg": "registry/2.reg", "status": "removed", "bytes": 20})
        path.write_text(json.dumps(data), "utf-8")

        undone = cleanup.restore(backup_id)
        self.assertEqual((undone.restored, undone.remaining), (1, 0))
        self.assertEqual([t for _, t, _ in undone.failed], [cleanup.R_SERVICE])
        self.assertEqual((self.world / "x.txt").read_text("utf-8"), "kept")
        self.assertTrue(folder.is_dir(), "the service can only be reinstalled by hand: its export stays")
        self.assertEqual(json.loads(path.read_text("utf-8"))["items"][1]["status"], "manual")
        self.assertEqual([(b.id, b.items) for b in cleanup.backups()], [(backup_id, 1)])

        again = cleanup.restore(backup_id)
        self.assertEqual((again.restored, again.remaining, [t for _, t, _ in again.failed]), (0, 0, [cleanup.R_SERVICE]))
        self.assertTrue(folder.is_dir())
        self.assertTrue(cleanup.delete_backup(backup_id))
        self.assertEqual(cleanup.backups(), [])


class RestorePoint(TempTree):
    def test_setting_and_override(self):
        write(self.world / "a.txt")
        write(self.world / "b.txt")
        write(self.world / "c.txt")

        class Settings:
            def __init__(self, on):
                self.on = on

            def get(self, key, default=None):
                return self.on if key == "cleanup_restore_point" else default

        self.addCleanup(cleanup.configure, None)
        with mock.patch.object(cleanup, "_create_restore_point", return_value=True) as make:
            cleanup.configure(Settings(False))
            self.assertIsNone(run([item("file", self.world / "a.txt")])[0].restore_point)
            make.assert_not_called()
            cleanup.configure(Settings(True))
            made, calls = run([item("file", self.world / "b.txt")], restore_point=None)
            self.assertIs(made.restore_point, True)
            self.assertEqual(calls[0][0], cleanup.P_POINT)
            self.assertIs(self.manifest(made.backup_id)["restore_point"], True)
        with mock.patch.object(cleanup, "_create_restore_point", return_value=False):
            failed = run([item("file", self.world / "c.txt")], restore_point=True)[0]
        self.assertEqual((failed.restore_point, failed.removed), (False, 1), "no restore point is not an error")

    def test_without_administrator_rights_nothing_is_started(self):
        with mock.patch.object(cleanup.subprocess, "Popen", side_effect=AssertionError("must not start PowerShell")):
            self.assertFalse(cleanup._create_restore_point("App", None))

    def test_nothing_to_remove_makes_no_restore_point(self):
        with mock.patch.object(cleanup, "_create_restore_point", side_effect=AssertionError("no point for nothing")):
            self.assertEqual(run([], restore_point=True)[0].removed, 0)
            self.assertIsNone(run([item("folder", self.world, leftovers.PROTECTED)], restore_point=True)[0].restore_point)


class Helpers(unittest.TestCase):
    def test_item_labels(self):
        self.assertEqual(cleanup.item_label(item("folder", "C:\\Users\\a\\AppData\\Roaming\\Foo\\")), "Foo")
        self.assertEqual(cleanup.item_label(item("regkey", "HKCU\\Software\\Vendor\\Foo")), "Foo")
        self.assertEqual(cleanup.item_label(item("regvalue", "HKCU\\Software\\Run|Updater")), "Updater")
        self.assertEqual(cleanup.item_label(item("service", "HKLM\\SYSTEM\\CurrentControlSet\\Services\\Svc")), "Svc")

    def test_long_prefix(self):
        self.assertEqual(cleanup._long("C:\\short"), "C:\\short")
        self.assertTrue(cleanup._long("C:\\" + "a" * 300).startswith("\\\\?\\C:\\"))

    def test_every_template_has_a_persian_entry(self):
        from upstream.i18n import FA
        self.assertEqual([t for t in cleanup.REASONS if t not in FA], [])


if __name__ == "__main__":
    unittest.main()
