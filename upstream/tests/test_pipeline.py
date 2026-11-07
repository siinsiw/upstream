"""The update pipeline with every external step replaced by a fake: order of steps, cancel, ask."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from upstream import updater
from upstream.context import UpdateContext
from upstream.exitcodes import Outcome
from upstream.inventory import InstalledApp
from upstream.scanner import AppStatus, Update


def make_status(source="github"):
    app = InstalledApp(reg_key=r"HKLM64\X", name="Demo", version="1.0", publisher="Demo Inc", scope="machine", arch="x64")
    upd = Update(source, "2.0", "o/r", download_url="https://example.invalid/demo.exe", file_name="demo.exe")
    return AppStatus(app, update=upd)


class PipelineTest(unittest.TestCase):
    def run_direct(self, **over):
        status = make_status()
        calls = []
        patches = {
            "net.preflight": lambda ctx: calls.append("preflight"),
            "net.download": lambda *a, **k: (calls.append("download"), Path("demo.exe"))[1],
            "installer.signature": lambda p: ("Valid", "Demo Inc"),
            "installer.detect": lambda p, h=None: "inno",
            "running.close_for_update": lambda ctx, app, progress: calls.append("close"),
            "installer.install": lambda *a, **k: (calls.append("install"), 0)[1],
            "verify.check": lambda app, before, outcome, ver: mock.Mock(
                kind="installed", ok=True, template="Installed {version}", values={"version": "2.0"}, version="2.0"),
        }
        patches.update(over)
        stack = [mock.patch.object(getattr(updater, name.split(".")[0]), name.split(".")[1], fn)
                 for name, fn in patches.items()]
        for p in stack:
            p.start()
        try:
            with mock.patch.object(Path, "unlink"):
                result = updater.Updater().run(status, lambda *a, **k: None, UpdateContext())
        finally:
            for p in stack:
                p.stop()
        return result, calls

    def test_steps_run_in_order(self):
        (ok, template, values, version), calls = self.run_direct()
        self.assertTrue(ok)
        self.assertEqual(calls, ["preflight", "download", "close", "install"])
        self.assertEqual(version, "2.0")

    def test_failed_exit_code_is_reported(self):
        (ok, template, values, version), _ = self.run_direct(
            **{"exitcodes.classify": lambda code, kind: Outcome("failed", "Installer exited with code {code}", {"code": code}),
               "installer.install": lambda *a, **k: 5})
        self.assertFalse(ok)
        self.assertEqual(values, {"code": 5})

    def test_cancelled_outcome_stops_without_trying_next_source(self):
        status = make_status()
        status.fallbacks = [Update("winget", "2.0", "Demo.Demo")]
        tried = []
        with mock.patch.object(updater.net, "preflight"), \
             mock.patch.object(updater.Updater, "_direct", lambda self, *a: (Outcome("cancelled", "Cancelled."), "")), \
             mock.patch.object(updater.Updater, "_winget", lambda self, *a: tried.append("winget")):
            ok, template, values, _ = updater.Updater().run(status, lambda *a, **k: None, UpdateContext())
        self.assertFalse(ok)
        self.assertEqual(tried, [])


if __name__ == "__main__":
    unittest.main()
