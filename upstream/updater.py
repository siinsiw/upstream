"""Runs one update: the chosen source first, then the fallbacks in order.

Messages travel as (English template, values) pairs and are translated by the
UI when shown, so switching language mid-install re-renders them correctly."""
from __future__ import annotations

import logging

from . import installer, winget
from .i18n import SOURCE_LABELS, english
from .inventory import find_current
from .scanner import AppStatus, Update
from .versioning import is_newer

log = logging.getLogger("upstream.update")

KIND_LABELS = {
    "msi": "MSI", "msix": "MSIX", "inno": "Inno Setup", "nullsoft": "NSIS", "burn": "WiX Burn",
    "installshield": "InstallShield", "advancedinstaller": "Advanced Installer", "squirrel": "Squirrel",
    "exe": "unknown (runs its normal setup window)",
}

Message = tuple[str, dict]


class Updater:
    def run(self, status: AppStatus, progress) -> tuple[bool, str, dict, str]:
        """progress(template, percent, **values), percent -1 meaning busy.
        Returns (ok, message template, message values, installed version)."""
        app = status.app
        attempts = [u for u in [status.update, *status.fallbacks] if u and (u.download_url or u.source == "winget")]
        if not attempts:
            return False, "No source for an automatic download", {}, app.version
        error: Message = ("Install failed", {})
        for index, upd in enumerate(attempts):
            try:
                if upd.source == "winget":
                    ok, error = self._winget(upd, progress)
                else:
                    ok, error = self._direct(app, upd, progress)
            except installer.Cancelled as e:
                return False, e.template, e.values, app.version
            except installer.InstallError as e:
                ok, error = False, (e.template, e.values)
            except Exception as e:
                log.exception("update of %s via %s failed", app.name, upd.source)
                ok, error = False, ("{error}", {"error": str(e)})
            if ok:
                current = find_current(app)
                new_version = current.version if current else upd.version
                if current and not is_newer(new_version, app.version):
                    return True, "Installer finished, but Windows still reports {version}", {"version": new_version}, new_version
                return True, "Installed {version}", {"version": new_version}, new_version
            log.warning("%s: %s failed: %s", app.name, upd.source, english(*error))
            if index + 1 < len(attempts):
                progress("{source} failed, trying the next source…", -1,
                         source=SOURCE_LABELS.get(upd.source, upd.source))
        return False, error[0], error[1], app.version

    def _direct(self, app, upd: Update, progress) -> tuple[bool, Message]:
        progress("Downloading", 0)

        def on_bytes(done, total):
            progress("Downloading", int(done * 100 / total) if total else -1)

        path = installer.download(upd.download_url, upd.file_name, on_bytes, upd.sha256)
        log.info("%s: downloaded %s (sha256 %s)", app.name, path.name,
                 "verified" if upd.sha256 else "not published")

        progress("Checking signature", -1)
        status, signer = installer.signature(path)
        log.info("%s: Authenticode %s %s", app.name, status, signer)
        if status == "HashMismatch":
            path.unlink(missing_ok=True)
            return False, ("The file's digital signature is broken (tampered); nothing was installed.", {})

        kind = installer.detect(path, upd.installer_type)
        log.info("%s: installer type %s", app.name, KIND_LABELS.get(kind, kind))
        progress("Installing" if kind != "exe" else "Setup window opened", -1)
        code = installer.install(path, kind, app.scope)
        if installer.succeeded(code):
            path.unlink(missing_ok=True)
            return True, ("", {})
        return False, ("Installer exited with code {code}", {"code": code})

    def _winget(self, upd: Update, progress) -> tuple[bool, Message]:
        progress("Preparing (winget)", -1)
        code, last = winget.update(
            upd.origin, upd.winget_mode,
            lambda pct: progress("Downloading (winget)", pct),
            lambda line: log.info("winget %s: %s", upd.origin, line),
        )
        if code == 0:
            return True, ("", {})
        if last:
            return False, ("{error}", {"error": last})
        return False, ("winget exited with {code}", {"code": f"0x{code:08X}"})
